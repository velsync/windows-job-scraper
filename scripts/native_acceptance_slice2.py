#!/usr/bin/env python3
"""Slice-2 packaged/native acceptance companion harness (W2-01..W2-06).

This deliberately does not modify the accepted W0/W1 harness. Final Slice-2
promotion runs the existing ``native_acceptance.py --slice all`` foundation
matrix and this W2 matrix against the same verified package candidate.

W2 uses deterministic loopback provider fixtures. Source/binding provisioning
is performed by the harness because source-management UI/API is not part of
Slice 2; the *target application* still owns run planning, acquisition,
adapter execution, canonical persistence, search, restart and Doctor behavior.
"""

from __future__ import annotations

import argparse
import http.server
import json
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from native_acceptance import FAIL, NOT_RUN, PASS, Harness, REPO_ROOT


_FIXTURES = REPO_ROOT / "tests" / "fixtures"
_EXPECTED_TITLES = {"Backend Engineer", "Platform Engineer", "Senior Data Engineer"}


@dataclass(frozen=True)
class Provider:
    check_id: str
    adapter_id: str
    canonical_host: str


_PROVIDERS = (
    Provider("W2-01", "greenhouse", "boards.greenhouse.io"),
    Provider("W2-02", "lever", "jobs.lever.co"),
    Provider("W2-03", "ashby", "jobs.ashbyhq.com"),
)

_LEVER_DETAILS = {
    "1a2b3c4d-0000-4000-8000-000000004001": "posting_detail.json",
    "1a2b3c4d-0000-4000-8000-000000004002": "posting_detail_multi_location.json",
    "1a2b3c4d-0000-4000-8000-000000004003": "posting_detail_remote_hostile_links.json",
}
_GREENHOUSE_DETAILS = {
    "4001": "job_detail.json",
    "4002": "job_detail_multi_location.json",
    "4003": "job_detail_remote_hostile_links.json",
}


class _ProviderFixtureHandler(http.server.BaseHTTPRequestHandler):
    """Deterministic local stand-in for the three graduated provider APIs."""

    def _send_file(self, path: Path, status: int = 200) -> None:
        body = path.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        body = b'{"error":"not found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server interface
        path = urlsplit(self.path).path
        if path == "/v1/boards/acme/jobs":
            self._send_file(_FIXTURES / "greenhouse" / "board_list.json")
            return
        gh = re.fullmatch(r"/v1/boards/acme/jobs/(\d+)", path)
        if gh:
            fixture = _GREENHOUSE_DETAILS.get(gh.group(1))
            if fixture:
                self._send_file(_FIXTURES / "greenhouse" / fixture)
            else:
                self._not_found()
            return

        if path == "/v0/postings/acme":
            self._send_file(_FIXTURES / "lever" / "postings_list.json")
            return
        lever = re.fullmatch(r"/v0/postings/acme/([a-z0-9-]+)", path)
        if lever:
            fixture = _LEVER_DETAILS.get(lever.group(1))
            if fixture:
                self._send_file(_FIXTURES / "lever" / fixture)
            else:
                self._not_found()
            return

        if path == "/posting-api/job-board/acme":
            self._send_file(_FIXTURES / "ashby" / "board_jobs.json")
            return

        self._not_found()

    def log_message(self, *args):
        pass


class Slice2Harness(Harness):
    def __init__(
        self,
        target: str,
        exe: Path | None,
        evidence: Path,
        *,
        candidate_commit: str | None = None,
        build_id: str | None = None,
    ) -> None:
        super().__init__(target, exe, evidence)
        self.candidate_commit = candidate_commit
        self.build_id = build_id
        self._greenhouse_root: Path | None = None

    def _seed_provider(self, db, provider: Provider, port: int) -> None:
        """Create one host-owned provider source/binding for the target run."""
        from jobscraper.runtime.provisioning import (
            ensure_builtin_adapter_definition,
            provision_source_and_binding,
        )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        db.execute(
            "INSERT OR IGNORE INTO adapter_permission_profiles"
            " (id, display_name, created_at) VALUES ('w2-perm','W2 fixture policy',?)",
            (now,),
        )
        db.execute(
            "INSERT OR IGNORE INTO adapter_permission_profile_revisions"
            " (id, permission_profile_id, revision, policy_json, created_at)"
            " VALUES ('w2-permrev','w2-perm',1,'{}',?)",
            (now,),
        )
        definition = ensure_builtin_adapter_definition(
            db, provider.adapter_id, now=now, commit=False
        )
        if not definition.verified:
            raise RuntimeError(
                f"built-in {provider.adapter_id} definition conflict: "
                f"{definition.conflict_reason}"
            )
        config: dict[str, object] = {
            "board": "acme",
            "api_base_url": f"http://127.0.0.1:{port}",
            "company_name": "Acme Fixtures",
            "careers_url": "https://acme.example/careers",
        }
        if provider.adapter_id in {"greenhouse", "lever"}:
            config["detail_fetch"] = True
        provision_source_and_binding(
            db,
            display_name=f"W2 {provider.adapter_id} fixture",
            source_family="ATS_BOARD",
            entry_url=f"http://127.0.0.1:{port}/careers/{provider.adapter_id}",
            canonical_host=provider.canonical_host,
            adapter_id=provider.adapter_id,
            adapter_version=definition.adapter_version,
            strategy="PROVIDER_NATIVE",
            execution_class="HTTP",
            config=config,
            now=now,
            commit=False,
        )
        db.commit()

    def _provider_check(self, provider: Provider, data_root: Path, port: int) -> None:
        launcher = None
        try:
            launcher, url = self.launch_and_get_url(data_root)
            db = self._w1_wait_db(data_root)
            try:
                self._seed_provider(db, provider, port)
            finally:
                db.close()

            auth = self._w1_bootstrap(url)
            status, profile = self._w1_api(
                auth,
                "POST",
                "/api/profiles",
                json_body={
                    "name": f"W2 {provider.adapter_id}",
                    "keywords": ["engineer"],
                    "eligible_countries": ["DE", "FR"],
                    "remote_rules": {"remote_ok": True},
                    "min_score_inbox": 0,
                },
            )
            if status != 200 or not profile or not profile.get("id"):
                raise RuntimeError(f"profile creation failed: HTTP {status} {profile!r}")
            status, run = self._w1_api(
                auth, "POST", "/api/runs", json_body={"profile_id": profile["id"]}
            )
            if status != 200 or not run:
                raise RuntimeError(f"run failed: HTTP {status} {run!r}")

            db = self._db_connect(data_root)
            try:
                titles = {
                    row["title"]
                    for row in db.execute("SELECT title FROM jobs").fetchall()
                }
                observations = db.execute(
                    "SELECT COUNT(*) FROM job_observations WHERE adapter_id = ?",
                    (provider.adapter_id,),
                ).fetchone()[0]
                presences = db.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0]
                with_company = db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE company_id IS NOT NULL"
                ).fetchone()[0]
                locations = db.execute("SELECT COUNT(*) FROM job_locations").fetchone()[0]
                cleaned = db.execute(
                    "SELECT COUNT(*) FROM jobs"
                    " WHERE description_md IS NOT NULL AND TRIM(description_md) <> ''"
                ).fetchone()[0]
            finally:
                db.close()

            ok = (
                run.get("status") == "SUCCEEDED"
                and run.get("jobs_saved") == 3
                and titles == _EXPECTED_TITLES
                and observations >= 3
                and presences == 3
                and with_company == 3
                and locations >= 3
                and cleaned == 3
            )
            self.record(
                provider.check_id,
                PASS if ok else FAIL,
                f"packaged {provider.adapter_id} provider path produces canonical Slice-2 state",
                run_status=run.get("status"),
                jobs_saved=run.get("jobs_saved"),
                titles=sorted(titles),
                provider_observations=observations,
                presences=presences,
                jobs_with_company=with_company,
                locations=locations,
                cleaned_jobs=cleaned,
            )
        except Exception as exc:  # noqa: BLE001
            self.record(provider.check_id, FAIL, f"{provider.adapter_id} packaged path failed: {exc}")
        finally:
            if launcher is not None:
                self.stop_launcher(launcher)

    def _search_and_restart_checks(self, data_root: Path) -> None:
        launcher = None
        try:
            launcher, url = self.launch_and_get_url(data_root)
            auth = self._w1_bootstrap(url)
            status, result = self._w1_api(auth, "GET", "/api/search?q=engineer")
            result = result or {}
            mode = result.get("mode")
            warning = result.get("warning")
            bm25 = result.get("bm25")
            titles = {hit.get("title") for hit in result.get("hits", [])}
            honest_mode = (
                (mode == "FTS5_ACTIVE" and warning is None and bm25 is True)
                or (
                    mode == "SUBSTRING_FALLBACK"
                    and isinstance(warning, str)
                    and bool(warning.strip())
                    and bm25 is False
                )
            )
            search_ok = (
                status == 200
                and result.get("total") == 3
                and titles == _EXPECTED_TITLES
                and honest_mode
            )
            self.record(
                "W2-04",
                PASS if search_ok else FAIL,
                "packaged Slice-2 search route returns provider jobs and reports capability honestly",
                http_status=status,
                mode=mode,
                warning=warning,
                bm25=bm25,
                total=result.get("total"),
                titles=sorted(t for t in titles if t),
            )

            db = self._db_connect(data_root)
            try:
                jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                companies = db.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
                locations = db.execute("SELECT COUNT(*) FROM job_locations").fetchone()[0]
                bindings = db.execute(
                    "SELECT COUNT(*) FROM source_adapter_bindings"
                    " WHERE current_revision_id IS NOT NULL"
                ).fetchone()[0]
            finally:
                db.close()
            restart_ok = jobs == 3 and companies >= 1 and locations >= 3 and bindings == 1
            self.record(
                "W2-05",
                PASS if restart_ok else FAIL,
                "Slice-2 canonical/company/location/binding state survives packaged restart",
                jobs=jobs,
                companies=companies,
                locations=locations,
                current_bindings=bindings,
            )
        except Exception as exc:  # noqa: BLE001
            if "W2-04" not in self.results:
                self.record("W2-04", FAIL, f"search check failed after restart: {exc}")
            if "W2-05" not in self.results:
                self.record("W2-05", FAIL, f"restart persistence check failed: {exc}")
        finally:
            if launcher is not None:
                self.stop_launcher(launcher)

    def _doctor_check(self, data_root: Path) -> None:
        try:
            report = self.doctor_json(data_root)
            failed = [
                c.get("name") for c in report.get("checks", []) if c.get("status") == "FAIL"
            ]
            ok = report.get("exit_code") == 0 and not failed
            self.record(
                "W2-06",
                PASS if ok else FAIL,
                "Doctor healthy after packaged Slice-2 provider/search workload",
                exit_code=report.get("exit_code"),
                failed_checks=failed,
            )
            self.write_evidence(
                "slice2-doctor.json", json.dumps(report, indent=2, sort_keys=True)
            )
        except Exception as exc:  # noqa: BLE001
            self.record("W2-06", FAIL, f"Doctor check failed: {exc}")

    def run_slice2(self) -> int:
        started = datetime.now(timezone.utc).isoformat()
        self.write_evidence(
            "process-inventory-before.txt", "\n".join(self.list_processes())
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProviderFixtureHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            with tempfile.TemporaryDirectory(prefix="wjs-w2-") as root_name:
                root = Path(root_name)
                for provider in _PROVIDERS:
                    data_root = root / provider.adapter_id
                    (data_root / "auth").mkdir(parents=True, exist_ok=True)
                    (data_root / "runtime").mkdir(parents=True, exist_ok=True)
                    self._provider_check(provider, data_root, port)
                    if provider.adapter_id == "greenhouse":
                        self._greenhouse_root = data_root

                if self.results.get("W2-01", {}).get("status") == PASS and self._greenhouse_root:
                    self._search_and_restart_checks(self._greenhouse_root)
                    self._doctor_check(self._greenhouse_root)
                else:
                    self.record(
                        "W2-04", NOT_RUN, "Greenhouse prerequisite failed; search check not executable"
                    )
                    self.record(
                        "W2-05", NOT_RUN, "Greenhouse prerequisite failed; restart check not executable"
                    )
                    self.record(
                        "W2-06", NOT_RUN, "Greenhouse prerequisite failed; Doctor check not executable"
                    )
        finally:
            server.shutdown()
            server.server_close()

        self.write_evidence(
            "process-inventory-after.txt", "\n".join(self.list_processes())
        )
        statuses = {k: v["status"] for k, v in sorted(self.results.items())}
        record = {
            "schema_version": 1,
            "slice": "2",
            "spec_version": "0.3.1.3",
            "target": self.target,
            "candidate_commit": self.candidate_commit,
            "build_id": self.build_id,
            "started_at_utc": started,
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "tests": statuses,
            "details": self.results,
            "promotion": (
                "PENDING_NATIVE_RUN"
                if self.target != "exe"
                else "PENDING_PROMOTION_REVIEW"
            ),
            "unresolved_critical_findings": sum(
                1 for status in statuses.values() if status != PASS
            ),
        }
        self.write_evidence(
            "native-acceptance.json", json.dumps(record, indent=2, sort_keys=True)
        )
        incomplete = [check_id for check_id, status in statuses.items() if status != PASS]
        if incomplete:
            print(f"[slice2-native] incomplete checks: {incomplete}", flush=True)
            return 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Slice-2 native acceptance companion harness (W2-01..W2-06)"
    )
    parser.add_argument("--target", choices=["exe", "dev"], default="exe")
    parser.add_argument("--exe", type=Path, default=None, help="package dir (dist/JobScraper)")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "slice2" / "run" / "w2",
    )
    parser.add_argument("--candidate-commit", default=None)
    parser.add_argument("--build-id", default=None)
    args = parser.parse_args()
    if args.target == "exe":
        if args.exe is None:
            parser.error("--target exe requires --exe <package-dir>")
        if not args.candidate_commit or not args.build_id:
            parser.error(
                "--target exe requires --candidate-commit and --build-id to bind evidence"
            )
    harness = Slice2Harness(
        args.target,
        args.exe,
        args.evidence_dir,
        candidate_commit=args.candidate_commit,
        build_id=args.build_id,
    )
    return harness.run_slice2()


if __name__ == "__main__":
    raise SystemExit(main())
