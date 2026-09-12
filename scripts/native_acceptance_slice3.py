#!/usr/bin/env python3
"""Fail-closed Slice-3 packaged/native acceptance harness (W3-01..W3-10).

This harness is an external observer. In ``--target exe`` mode it may seed
operator-owned prerequisites with SQL, drive the packaged HTTP surface,
terminate exact target PIDs, and inspect durable SQLite state. It never imports
or executes ``jobscraper`` production behavior from the checkout to manufacture
proof. If the frozen package exposes no legitimate operation needed by a W3
requirement, the check is NOT_RUN with an explicit blocker.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.server
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from native_acceptance import FAIL, NOT_RUN, PASS, Harness  # noqa: E402

FROZEN_CANDIDATE = "c1cc7b07af9ce74f5629aa2de6332709e1739c1a"
FROZEN_BUILD_ID = "onewise-5068fbcdb158c649"
FROZEN_PACKAGE_SHA256 = (
    "5068fbcdb158c6495d629cbdc228bf3908a3fd1d131f502d3289d6ca487b461c"
)
W3_IDS = [f"W3-{number:02d}" for number in range(1, 11)]
CHECK_METHODS = {
    "W3-01": "_w3_01_lease_fence_epoch",
    "W3-02": "_w3_02_retry_after_persistence",
    "W3-03": "_w3_03_cursor_restart",
    "W3-04": "_w3_04_sitemap_bounded_crawl",
    "W3-05": "_w3_05_304_membership",
    "W3-06": "_w3_06_coverage_absence_safety",
    "W3-07": "_w3_07_fallback_truth",
    "W3-08": "_w3_08_cancellation_obligation",
    "W3-09": "_w3_09_temporal_availability",
    "W3-10": "_w3_10_post_workload_health",
}
RESOURCE_MEASUREMENTS = (
    "idle_service_memory_bytes",
    "browser_process_tree_peak_memory_bytes",
    "p95_local_search_latency_ms",
    "peak_temporary_disk_bytes",
    "wal_growth_bytes",
    "cancellation_responsiveness_ms",
    "total_http_bytes",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _numeric(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def _require_true(evidence: dict[str, Any], field: str, failures: list[str]) -> None:
    if evidence.get(field) is not True:
        failures.append(f"{field} not proved")


def evaluate_w3_01(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("prior_attempt_outcome") != "ABANDONED":
        failures.append("prior attempt is not ABANDONED")
    for field in (
        "fresh_epoch",
        "retry_under_fresh_epoch",
        "stale_heartbeat_rejected",
        "stale_commit_rejected",
    ):
        _require_true(evidence, field, failures)
    return failures


def evaluate_w3_02(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    _require_true(evidence, "cooldown_persisted", failures)
    if evidence.get("premature_redispatches") != 0:
        failures.append("premature redispatch occurred during cooldown")
    _require_true(evidence, "same_request_retried", failures)
    _require_true(evidence, "later_attempt_succeeded", failures)
    attempts = evidence.get("attempt_count")
    if not isinstance(attempts, int) or attempts < 2:
        failures.append("durable attempt history does not contain the retry")
    return failures


def evaluate_w3_03(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in (
        "checkpoint_persisted",
        "same_plan_identity_survived",
        "same_plan_resumed",
        "terminal_reached",
    ):
        _require_true(evidence, field, failures)
    if evidence.get("already_succeeded_refetches") != 0:
        failures.append("already-succeeded logical work was refetched")
    if sorted(evidence.get("union_expected") or []) != sorted(
        evidence.get("union_observed") or []
    ):
        failures.append("post-restart observation union is incorrect")
    if evidence.get("duplicates") != 0:
        failures.append("duplicate durable observations/work remain")
    return failures


def evaluate_w3_04(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in (
        "robots_seen",
        "sitemap_seen",
        "negative_ssrf_denied",
        "ordinary_provenance",
        "budget_within_limit",
    ):
        _require_true(evidence, field, failures)
    if evidence.get("observations") != evidence.get("expected_observations"):
        failures.append("generic crawl observation set is incomplete")
    if evidence.get("duplicate_fetches") != 0:
        failures.append("duplicate frontier URL was fetched")
    if evidence.get("out_of_scope_fetches") != 0:
        failures.append("out-of-scope URL was fetched")
    return failures


def evaluate_w3_05(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in (
        "conditional_304",
        "content_revisions_unchanged",
        "compatible_representation",
        "members_active",
    ):
        _require_true(evidence, field, failures)
    expected = sorted(evidence.get("expected_members") or [])
    before = sorted(evidence.get("before_members") or [])
    after = sorted(evidence.get("after_members") or [])
    reused = sorted(evidence.get("membership_reused") or [])
    reused_representation_id = evidence.get("reused_representation_id")
    membership_representation_ids = sorted(
        set(evidence.get("membership_representation_ids") or [])
    )
    if not expected or before != expected:
        failures.append("initial authoritative fixture membership is not the expected A/B set")
    if not before or before != after:
        failures.append("A/B membership was not retained across 304 restart")
    if reused != before:
        failures.append("durable cache membership reuse was not proved")
    if not isinstance(reused_representation_id, str) or not reused_representation_id:
        failures.append("304 fetch was not bound to an exact cache representation")
    elif membership_representation_ids != [reused_representation_id]:
        failures.append("retained membership was not bound to the exact 304 representation")
    if evidence.get("absence_transitions") != 0:
        failures.append("304 produced an absence transition")
    return failures


def evaluate_w3_06(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in ("same_generation_resumed", "later_complete", "same_scope_only"):
        _require_true(evidence, field, failures)
    if evidence.get("premature_absence") != 0:
        failures.append("interrupted generation applied premature absence")
    counts = evidence.get("absence_application_counts")
    if not isinstance(counts, list) or not counts or any(value != 1 for value in counts):
        failures.append("same-scope absence was not applied exactly once")
    if evidence.get("replay_duplicate_transitions") != 0:
        failures.append("coverage replay duplicated an absence transition")
    return failures


def evaluate_w3_07(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("pinned_ranks") != [0, 1, 2]:
        failures.append("fallback ranks 0/1/2 were not pinned in one logical group")
    if evidence.get("rank_outcomes") != [
        "FAILED",
        "SATISFIED",
        "SKIPPED_NOT_NEEDED",
    ]:
        failures.append("fallback rank outcomes are incorrect")
    if evidence.get("activated_ranks") != [0, 1]:
        failures.append("fallback activation did not advance exactly 0 -> 1")
    if evidence.get("final_group_outcome") != "SATISFIED":
        failures.append("logical fallback group is not SATISFIED")
    _require_true(evidence, "final_run_terminal", failures)
    if evidence.get("final_run_status") != "SUCCEEDED":
        failures.append("fallback-satisfied run did not terminalize as SUCCEEDED")
    return failures


def evaluate_w3_08(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("cancel_endpoint_status") != 200:
        failures.append("packaged cancellation endpoint did not accept cancellation")
    _require_true(evidence, "cancel_requested", failures)
    if not isinstance(evidence.get("accepted_observations"), int) or evidence[
        "accepted_observations"
    ] < 1:
        failures.append("no accepted observation existed before cancellation boundary")
    _require_true(evidence, "cancellation_before_local_completion", failures)
    if evidence.get("source_io_after_cancel") != 0:
        failures.append("source I/O occurred after durable cancellation")
    _require_true(evidence, "local_obligations_exactly_once", failures)
    accepted_count = evidence.get("accepted_observations")
    expected_counts = (
        {
            "RECONCILE": accepted_count,
            "ELIGIBILITY": accepted_count,
            "SCORE": accepted_count,
        }
        if isinstance(accepted_count, int) and accepted_count > 0
        else None
    )
    if evidence.get("obligation_counts_by_type") != expected_counts:
        failures.append("host-native obligation counts are not exactly one set per accepted observation")
    _require_true(evidence, "obligations_all_succeeded", failures)
    if not _numeric(evidence.get("cancellation_responsiveness_ms")):
        failures.append("cancellation responsiveness was not measured")
    if evidence.get("final_run_status") != "CANCELLED":
        failures.append("recovered cancelled run did not terminalize as CANCELLED")
    _require_true(evidence, "final_state_honest", failures)
    return failures


def evaluate_w3_09(evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in (
        "trusted_active",
        "aggregator_conflict",
        "reverse_completion_observed",
        "restart_between_evidence",
        "older_effective_evidence",
        "canonical_active",
        "aggregator_did_not_close",
    ):
        _require_true(evidence, field, failures)
    return failures


def _doctor_pragmas(doctor: dict[str, Any]) -> dict[str, Any] | None:
    checks = doctor.get("checks") if isinstance(doctor, dict) else None
    if not isinstance(checks, list):
        return None
    for item in checks:
        if isinstance(item, dict) and item.get("name") == "database":
            details = item.get("details") or {}
            pragmas = details.get("pragmas")
            return pragmas if isinstance(pragmas, dict) else None
    return None


def evaluate_w3_10(
    doctor: dict[str, Any],
    workload_databases: list[dict[str, Any]],
    processes: dict[str, Any],
    resources: dict[str, Any],
    approval: dict[str, Any] | None,
) -> list[str]:
    failures: list[str] = []
    if doctor.get("exit_code") != 0:
        failures.append(f"Doctor exit code is {doctor.get('exit_code')!r}")
    checks = doctor.get("checks")
    if not isinstance(checks, list) or not checks:
        failures.append("Doctor checks are missing")
    else:
        doctor_failures = [
            item.get("name", "unknown")
            for item in checks
            if isinstance(item, dict) and item.get("status") == "FAIL"
        ]
        if doctor_failures:
            failures.append(f"Doctor FAIL checks: {doctor_failures}")

    pragmas = _doctor_pragmas(doctor)
    if not pragmas:
        failures.append("package-reported database PRAGMAs are missing")
    else:
        if str(pragmas.get("journal_mode", "")).upper() != "WAL":
            failures.append("package journal_mode is not WAL")
        if pragmas.get("foreign_keys") != 1:
            failures.append("package foreign_keys is not enabled")
        if pragmas.get("synchronous") != 2:
            failures.append("package synchronous is not FULL")
        busy = pragmas.get("busy_timeout_ms", pragmas.get("busy_timeout"))
        if not isinstance(busy, int) or busy <= 0:
            failures.append("package busy_timeout is not positive")

    if not isinstance(workload_databases, list) or not workload_databases:
        failures.append("workload database health records are missing")
    else:
        total_fetch_attempts = 0
        for database in workload_databases:
            name = database.get("name", "unknown")
            if database.get("integrity_check") != ["ok"]:
                failures.append(f"{name} integrity_check failed")
            if database.get("foreign_key_check") != []:
                failures.append(f"{name} foreign_key_check failed")
            if database.get("application_consistency_ok") is not True:
                failures.append(f"{name} application consistency not proved")
            fetch_count = database.get("fetch_attempt_count")
            fetch_bytes = database.get("fetch_bytes_total")
            if not isinstance(fetch_count, int) or isinstance(fetch_count, bool) or fetch_count < 0:
                failures.append(f"{name} durable fetch-attempt count is invalid")
            else:
                total_fetch_attempts += fetch_count
            if not _numeric(fetch_bytes):
                failures.append(f"{name} durable fetch-byte total is invalid")
        if total_fetch_attempts <= 0:
            failures.append("workload corpus contains no durable fetch attempt")

    if processes.get("residual_target_pids"):
        failures.append(
            f"residual target processes remain: {processes['residual_target_pids']}"
        )
    if processes.get("browser_cleanup_ok") is not True:
        failures.append("browser cleanup/process-tree result is not PASS")

    for name in RESOURCE_MEASUREMENTS:
        if not _numeric(resources.get(name)):
            failures.append(f"required resource measurement missing or invalid: {name}")
    if resources.get("http_bytes_source") != "fixture_http_messages":
        failures.append(
            "HTTP byte measurement is not full deterministic fixture HTTP-message accounting"
        )
    if resources.get("browser_cleanup_ok") is not True:
        failures.append("resource record does not confirm browser cleanup")

    if not isinstance(approval, dict) or approval.get("approved") is not True:
        failures.append("resource ceiling approval is absent")
    else:
        ceilings = approval.get("ceilings")
        if not isinstance(ceilings, dict):
            failures.append("approved resource ceilings are missing")
        else:
            for name in RESOURCE_MEASUREMENTS:
                ceiling = ceilings.get(name)
                value = resources.get(name)
                if not _numeric(ceiling):
                    failures.append(f"approved ceiling missing or invalid: {name}")
                elif _numeric(value) and value > ceiling:
                    failures.append(
                        f"resource ceiling exceeded: {name}={value} > {ceiling}"
                    )
    return failures


def classify_w3_10_failures(failures: list[str]) -> str:
    """Classify W3-10 without letting missing VER-15 evidence mask real defects."""
    if not failures:
        return PASS
    not_run_prefixes = (
        "required resource measurement missing or invalid:",
        "resource ceiling approval is absent",
        "approved resource ceilings are missing",
        "approved ceiling missing or invalid:",
    )
    if all(any(failure.startswith(prefix) for prefix in not_run_prefixes) for failure in failures):
        return NOT_RUN
    return FAIL


def load_resource_approval(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("resource approval must be a JSON object")
    actual = (
        payload.get("candidate_commit"),
        payload.get("build_id"),
        payload.get("package_sha256"),
    )
    expected = (FROZEN_CANDIDATE, FROZEN_BUILD_ID, FROZEN_PACKAGE_SHA256)
    if actual != expected:
        raise ValueError(
            f"resource approval identity mismatch: expected {expected!r}, received {actual!r}"
        )
    return payload


def compute_package_tree_hash(package_dir: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file():
            continue
        count += 1
        relative = str(path.relative_to(package_dir)).replace("\\", "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                file_digest.update(chunk)
        digest.update(file_digest.digest())
    return digest.hexdigest(), count


def _module_is_checkout_production(module: object) -> bool:
    location = getattr(module, "__file__", None)
    if not location:
        return False
    try:
        candidate = Path(location).resolve()
        checkout = (REPO_ROOT / "src" / "jobscraper").resolve()
    except (OSError, RuntimeError):
        return False
    return candidate == checkout or checkout in candidate.parents


@dataclass
class FixtureReply:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)
    release: threading.Event | None = None


class FixtureState:
    """Deterministic fixture state plus repeatable HTTP-message byte accounting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._replies: dict[str, list[FixtureReply]] = {}
        self.requests: list[dict[str, Any]] = []
        self.total_http_bytes = 0

    def add(self, path: str, *replies: FixtureReply) -> None:
        self._replies.setdefault(path, []).extend(replies)

    @staticmethod
    def _request_bytes(path: str, headers: dict[str, str]) -> int:
        total = len(f"GET {path} HTTP/1.1\r\n".encode()) + 2
        for key, value in headers.items():
            total += len(f"{key}: {value}\r\n".encode())
        return total

    @staticmethod
    def _response_bytes(reply: FixtureReply) -> int:
        total = len(f"HTTP/1.1 {reply.status}\r\n".encode()) + 2 + len(reply.body)
        headers = {
            "Content-Type": reply.content_type,
            "Content-Length": str(len(reply.body)),
            **reply.headers,
        }
        for key, value in headers.items():
            total += len(f"{key}: {value}\r\n".encode())
        return total

    def take(self, path: str, headers: dict[str, str]) -> FixtureReply:
        with self._lock:
            choices = self._replies.get(path, [])
            reply = (
                choices.pop(0)
                if len(choices) > 1
                else choices[0]
                if choices
                else FixtureReply(404, b"not found", "text/plain")
            )
            request_index = len(self.requests)
            self.requests.append(
                {
                    "path": path,
                    "headers": headers,
                    "status": reply.status,
                    "at": _utc_now(),
                }
            )
            self.total_http_bytes += self._request_bytes(path, headers)
        if reply.release is not None and not reply.release.wait(timeout=120):
            reply = FixtureReply(504, b"fixture barrier timed out", "text/plain")
        with self._lock:
            self.requests[request_index]["status"] = reply.status
            self.total_http_bytes += self._response_bytes(reply)
        return reply

    def count(self, path: str | None = None) -> int:
        with self._lock:
            if path is None:
                return len(self.requests)
            return sum(item["path"] == path for item in self.requests)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self.requests))


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        state: FixtureState = self.server.fixture_state  # type: ignore[attr-defined]
        headers = {key.lower(): value for key, value in self.headers.items()}
        reply = state.take(self.path, headers)
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        for key, value in reply.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if reply.body:
            try:
                self.wfile.write(reply.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, *_args):
        pass


class FixtureServer:
    def __init__(self, state: FixtureState) -> None:
        self.state = state
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self.server.fixture_state = state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


def _json_reply(payload: dict[str, Any], **kwargs) -> FixtureReply:
    # Normal JSON fixture responses are HTTP 200 unless a scenario explicitly
    # requests another status (for example 429/503). Keep the default here,
    # rather than on FixtureReply itself, so non-JSON fixture replies remain
    # required to state their status explicitly.
    kwargs.setdefault("status", 200)
    return FixtureReply(body=json.dumps(payload).encode("utf-8"), **kwargs)


def _jobs(*ids: str) -> dict[str, Any]:
    return {
        "jobs": [
            {
                "id": item,
                "title": f"W3 Engineer {item}",
                "company": "W3 Fixture",
                "description": "<p>deterministic fixture</p>",
                "url": f"https://jobs.example.test/jobs/{item}",
                "apply_url": f"https://jobs.example.test/jobs/{item}/apply",
                "locations": ["Remote"],
                "created_at": "2026-09-12T12:00:00Z",
            }
            for item in ids
        ]
    }


class _ResourceSampler:
    """External sampler for one service lifetime."""

    def __init__(self, harness: "Slice3Harness", root: Path, pid: int) -> None:
        self.harness = harness
        self.root = root
        self.pid = pid
        self.stop_event = threading.Event()
        self.initial_temp = harness._directory_size(root)
        wal = Path(str(harness._database_path(root)) + "-wal")
        try:
            self.initial_wal = wal.stat().st_size if wal.exists() else 0
        except OSError:
            self.initial_wal = 0
        self.peak_temp = 0
        self.peak_wal = 0
        self.peak_descendant_memory = 0
        self.descendants_observed = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.sample()
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.wait(0.75):
            self.sample()

    def sample(self) -> None:
        current_temp = self.harness._directory_size(self.root)
        # VER-15 asks for peak temporary-disk usage, not merely growth from
        # an arbitrary service-lifetime baseline.
        self.peak_temp = max(self.peak_temp, current_temp)
        wal = Path(str(self.harness._database_path(self.root)) + "-wal")
        try:
            current_wal = wal.stat().st_size if wal.exists() else 0
            self.peak_wal = max(self.peak_wal, max(0, current_wal - self.initial_wal))
        except OSError:
            pass
        snapshot = self.harness._process_snapshot()
        descendants = self.harness._descendants_from_snapshot(self.pid, snapshot)
        if descendants:
            self.descendants_observed = True
        total = sum(int(snapshot[p].get("memory", 0) or 0) for p in descendants if p in snapshot)
        self.peak_descendant_memory = max(self.peak_descendant_memory, total)
        self.harness._track_owned_processes({self.pid, *descendants}, snapshot)

    def stop(self) -> None:
        self.sample()
        self.stop_event.set()
        self.thread.join(timeout=5)
        self.harness._peak_temp_bytes = max(self.harness._peak_temp_bytes, self.peak_temp)
        self.harness._peak_wal_bytes = max(self.harness._peak_wal_bytes, self.peak_wal)
        if self.descendants_observed:
            current_peak = self.harness._browser_tree_peak_bytes or 0
            self.harness._browser_tree_peak_bytes = max(
                current_peak, self.peak_descendant_memory
            )


class Slice3Harness(Harness):
    def __init__(
        self,
        target: str,
        exe: Path | None,
        evidence: Path,
        *,
        candidate_commit: str | None = None,
        build_id: str | None = None,
        package_sha256: str | None = None,
        resource_approval: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(target, exe, evidence)
        self.candidate_commit = candidate_commit
        self.build_id = build_id
        self.package_sha256 = package_sha256
        self.resource_approval = resource_approval
        self._package_verification: dict[str, Any] | None = None
        self._temporary_roots: list[tempfile.TemporaryDirectory] = []
        self._workload_roots: dict[str, list[Path]] = {}
        self._owned_process_identities: set[tuple[int, str]] = set()
        self._active_samplers: dict[int, _ResourceSampler] = {}
        self._fixture_bytes = 0
        self._idle_service_memory: list[int] = []
        self._peak_temp_bytes = 0
        self._peak_wal_bytes = 0
        self._browser_tree_peak_bytes: int | None = None
        self._metrics: dict[str, Any] = {}
        self._scenario_evidence: dict[str, Any] = {}

    @staticmethod
    def _checkout_modules() -> set[str]:
        return {
            name
            for name, module in sys.modules.items()
            if name == "jobscraper" or name.startswith("jobscraper.")
            if _module_is_checkout_production(module)
        }

    def _assert_exe_no_checkout_behavior(self) -> None:
        if self.target != "exe":
            return
        imported = self._checkout_modules()
        if imported:
            raise RuntimeError(
                "exe acceptance imported checkout production module(s): "
                + ", ".join(sorted(imported))
            )

    def _require_exe_binding(self) -> dict[str, Any]:
        if self.target != "exe":
            return {}
        actual = (self.candidate_commit, self.build_id, self.package_sha256)
        expected = (FROZEN_CANDIDATE, FROZEN_BUILD_ID, FROZEN_PACKAGE_SHA256)
        if actual != expected:
            raise ValueError(
                "exe target must use the exact frozen package identity "
                f"{expected!r}; received {actual!r}"
            )
        if self.exe is None or not self.exe.is_dir():
            raise ValueError("frozen package directory does not exist")
        executable_name = "JobScraper.exe" if os.name == "nt" else "JobScraper"
        if not (self.exe / executable_name).is_file():
            raise ValueError(f"frozen package executable is missing: {executable_name}")
        verification_path = self.exe.parent.parent / "package-verification.json"
        try:
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"frozen package verification record is unreadable: {exc}") from exc
        expected_record = {
            "result": "PASS",
            "build_id": FROZEN_BUILD_ID,
            "build_sha256": FROZEN_PACKAGE_SHA256,
        }
        mismatches = {
            key: {"expected": value, "actual": verification.get(key)}
            for key, value in expected_record.items()
            if verification.get(key) != value
        }
        if mismatches:
            raise ValueError(f"frozen package verification record mismatch: {mismatches}")
        observed_hash, observed_count = compute_package_tree_hash(self.exe)
        if observed_hash != FROZEN_PACKAGE_SHA256:
            raise ValueError(
                "frozen package tree hash mismatch: "
                f"expected {FROZEN_PACKAGE_SHA256}, observed {observed_hash}"
            )
        expected_count = verification.get("file_count")
        if not isinstance(expected_count, int) or observed_count != expected_count:
            raise ValueError(
                "frozen package file count mismatch: "
                f"record={expected_count!r}, observed={observed_count}"
            )
        self._package_verification = {
            "record_path": str(verification_path.resolve()),
            "tree_sha256": observed_hash,
            "file_count": observed_count,
            **verification,
        }
        return self._package_verification

    @staticmethod
    def _database_path(data_root: Path) -> Path:
        return data_root / "db" / "jobscraper.sqlite3"

    def _db_connect(self, data_root: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(self._database_path(data_root), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _w1_wait_db(self, data_root: Path, timeout: float = 60.0) -> sqlite3.Connection:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = self._db_connect(data_root)
                conn.execute("SELECT COUNT(*) FROM sources").fetchone()
                return conn
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"target database never became ready: {last_error}")

    @staticmethod
    def _wait_until(predicate: Callable[[], Any], description: str, timeout: float = 30) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = predicate()
                if last:
                    return last
            except (sqlite3.Error, OSError):
                pass
            time.sleep(0.1)
        raise RuntimeError(f"timed out waiting for {description}; last={last!r}")

    def _new_root(self, check_id: str, *, suffix: str = "") -> Path:
        tag = check_id if not suffix else f"{check_id}-{suffix}"
        temporary = tempfile.TemporaryDirectory(prefix=f"wjs-{tag.lower()}-")
        self._temporary_roots.append(temporary)
        root = Path(temporary.name) / tag
        (root / "auth").mkdir(parents=True, exist_ok=True)
        (root / "runtime").mkdir(parents=True, exist_ok=True)
        self._workload_roots.setdefault(check_id, []).append(root)
        return root

    @staticmethod
    def _runtime_pid(data_root: Path) -> int | None:
        path = data_root / "runtime" / "service_descriptor.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            pid = payload.get("pid")
            return int(pid) if isinstance(pid, int) or str(pid).isdigit() else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _directory_size(root: Path) -> int:
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                pass
        return total

    @staticmethod
    def _process_snapshot() -> dict[int, dict[str, Any]]:
        if os.name == "nt":
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,WorkingSetSize,Name,CreationDate | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return {}
            try:
                payload = json.loads(proc.stdout)
            except ValueError:
                return {}
            rows = payload if isinstance(payload, list) else [payload]
            out: dict[int, dict[str, Any]] = {}
            for row in rows:
                try:
                    pid = int(row["ProcessId"])
                except (KeyError, TypeError, ValueError):
                    continue
                out[pid] = {
                    "ppid": int(row.get("ParentProcessId") or 0),
                    "memory": int(row.get("WorkingSetSize") or 0),
                    "name": str(row.get("Name") or ""),
                    "creation": str(row.get("CreationDate") or ""),
                }
            return out
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,rss=,comm="],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = {}
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            try:
                pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            out[pid] = {
                "ppid": ppid,
                "memory": rss * 1024,
                "name": parts[3],
                "creation": "",
            }
        return out

    @staticmethod
    def _descendants_from_snapshot(
        root_pid: int, snapshot: dict[int, dict[str, Any]]
    ) -> set[int]:
        descendants: set[int] = set()
        frontier = {root_pid}
        while frontier:
            next_frontier = {
                pid
                for pid, row in snapshot.items()
                if int(row.get("ppid", 0)) in frontier and pid not in descendants
            }
            descendants.update(next_frontier)
            frontier = next_frontier
        descendants.discard(root_pid)
        return descendants

    def _track_owned_processes(
        self, pids: set[int], snapshot: dict[int, dict[str, Any]]
    ) -> None:
        for pid in pids:
            row = snapshot.get(pid)
            if row is None:
                continue
            self._owned_process_identities.add(
                (int(pid), str(row.get("creation") or ""))
            )

    def _residual_owned_pids(
        self, snapshot: dict[int, dict[str, Any]]
    ) -> list[int]:
        residual: set[int] = set()
        for pid, creation in self._owned_process_identities:
            row = snapshot.get(pid)
            if row is None:
                continue
            current_creation = str(row.get("creation") or "")
            if not creation or current_creation == creation:
                residual.add(pid)
        return sorted(residual)

    def _process_alive(self, pid: int) -> bool:
        return pid in self._process_snapshot()

    def _process_memory(self, pid: int) -> int | None:
        row = self._process_snapshot().get(pid)
        return int(row["memory"]) if row is not None else None

    def _launch(self, data_root: Path) -> tuple[subprocess.Popen, dict[str, Any], int]:
        launcher, url = self.launch_and_get_url(data_root)
        auth = self._w1_bootstrap(url)
        pid = int(
            self._wait_until(
                lambda: self._runtime_pid(data_root), "target service descriptor PID"
            )
        )
        snapshot = self._process_snapshot()
        self._track_owned_processes({pid, int(launcher.pid)}, snapshot)
        memory = self._process_memory(pid)
        if memory is not None:
            self._idle_service_memory.append(memory)
        sampler = _ResourceSampler(self, data_root, pid)
        self._active_samplers[pid] = sampler
        sampler.start()
        return launcher, auth, pid

    def _stop_sampler(self, pid: int) -> None:
        sampler = self._active_samplers.pop(pid, None)
        if sampler is not None:
            sampler.stop()

    def _stop_target(self, launcher: subprocess.Popen, pid: int) -> None:
        snapshot = self._process_snapshot()
        self._track_owned_processes(
            {int(launcher.pid), pid, *self._descendants_from_snapshot(pid, snapshot)},
            snapshot,
        )
        try:
            self.stop_launcher(launcher)
        finally:
            self._stop_sampler(pid)

    def _kill_and_stop(self, launcher: subprocess.Popen, pid: int) -> None:
        snapshot = self._process_snapshot()
        self._track_owned_processes(
            {int(launcher.pid), pid, *self._descendants_from_snapshot(pid, snapshot)},
            snapshot,
        )
        try:
            self.kill_tree(pid)
            try:
                self.stop_launcher(launcher)
            except Exception:
                pass
        finally:
            self._stop_sampler(pid)

    def _seed_feed_source(
        self,
        conn: sqlite3.Connection,
        port: int,
        *,
        prefix: str,
        source_family: str = "PUBLIC_FEED",
        entry_url: str | None = None,
        url_template: str | None = None,
    ) -> dict[str, str]:
        now = _utc_now()
        source_id = f"{prefix}-src"
        binding_id = f"{prefix}-bnd"
        revision_id = f"{prefix}-rev"
        permission_id = f"{prefix}-perm"
        entry = entry_url or f"http://127.0.0.1:{port}/jobs"
        template = url_template or f"http://127.0.0.1:{port}/jobs?page={{page}}"
        config = json.dumps(
            {
                "url_template": template,
                "items_path": "jobs",
                "fields": {
                    "source_job_id": {"path": "id", "required": True},
                    "title": {"path": "title", "required": True},
                    "company": {"path": "company"},
                    "description": {"path": "description"},
                    "job_url": {"path": "url"},
                    "apply_url": {"path": "apply_url"},
                    "locations": {"path": "locations", "many": True},
                    "posted_at": {"path": "created_at"},
                },
            },
            sort_keys=True,
        )
        conn.execute(
            "INSERT INTO sources (id,display_name,source_family,entry_url,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (source_id, prefix, source_family, entry, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO adapter_definitions (adapter_id,adapter_version,adapter_api_version,manifest_json,created_at) VALUES ('json_api_feed','1.0.0','1','{}',?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO adapter_permission_profiles (id,display_name,created_at) VALUES (?,?,?)",
            (permission_id, prefix, now),
        )
        conn.execute(
            "INSERT INTO adapter_permission_profile_revisions (id,permission_profile_id,revision,policy_json,created_at) VALUES (?,?,1,'{}',?)",
            (f"{permission_id}-rev", permission_id, now),
        )
        conn.execute(
            "INSERT INTO source_adapter_bindings (id,source_id,display_name,current_revision_id,created_at) VALUES (?,?,?,?,?)",
            (binding_id, source_id, prefix, revision_id, now),
        )
        conn.execute(
            "INSERT INTO source_adapter_binding_revisions (id,binding_id,revision,adapter_id,adapter_version,strategy,execution_class,permission_profile_id,permission_profile_revision,config_json,created_at) VALUES (?,?,1,'json_api_feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP',?,1,?,?)",
            (revision_id, binding_id, permission_id, config, now),
        )
        conn.commit()
        return {
            "source_id": source_id,
            "binding_id": binding_id,
            "revision_id": revision_id,
        }

    def _seed_generic_discovery_source(
        self, conn: sqlite3.Connection, port: int, *, prefix: str
    ) -> dict[str, str]:
        now = _utc_now()
        ids = {
            "source_id": f"{prefix}-src",
            "binding_id": f"{prefix}-bnd",
            "revision_id": f"{prefix}-rev",
        }
        permission = f"{prefix}-perm"
        conn.execute(
            "INSERT INTO sources (id,display_name,source_family,entry_url,robots_mode,created_at,updated_at) VALUES (?,?,?,?, 'RESPECT',?,?)",
            (ids["source_id"], prefix, "CAREERS", f"http://127.0.0.1:{port}/start", now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO adapter_definitions (adapter_id,adapter_version,adapter_api_version,manifest_json,created_at) VALUES ('generic_discovery','1.0.0','1','{}',?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO adapter_permission_profiles (id,display_name,created_at) VALUES (?,?,?)",
            (permission, prefix, now),
        )
        conn.execute(
            "INSERT INTO adapter_permission_profile_revisions (id,permission_profile_id,revision,policy_json,created_at) VALUES (?,?,1,'{}',?)",
            (f"{permission}-rev", permission, now),
        )
        conn.execute(
            "INSERT INTO source_adapter_bindings (id,source_id,display_name,current_revision_id,created_at) VALUES (?,?,?,?,?)",
            (ids["binding_id"], ids["source_id"], prefix, ids["revision_id"], now),
        )
        conn.execute(
            "INSERT INTO source_adapter_binding_revisions (id,binding_id,revision,adapter_id,adapter_version,strategy,execution_class,permission_profile_id,permission_profile_revision,config_json,created_at) VALUES (?,?,1,'generic_discovery','1.0.0','GENERIC_DISCOVERY','HTTP',?,1,'{}',?)",
            (ids["revision_id"], ids["binding_id"], permission, now),
        )
        conn.commit()
        return ids

    def _create_profile(self, auth: dict[str, Any], name: str) -> str:
        status, payload = self._w1_api(
            auth,
            "POST",
            "/api/profiles",
            json_body={
                "name": name,
                "keywords": ["engineer"],
                "eligible_countries": ["DE"],
                "remote_rules": {"remote_ok": True},
                "min_score_inbox": 0,
            },
        )
        if status != 200 or not payload or not payload.get("id"):
            raise RuntimeError(f"target profile creation failed: HTTP {status} {payload!r}")
        return str(payload["id"])

    def _run_async(
        self, auth: dict[str, Any], profile_id: str
    ) -> tuple[threading.Thread, dict[str, Any]]:
        result: dict[str, Any] = {}

        def invoke() -> None:
            try:
                result["response"] = self._w1_api(
                    auth, "POST", "/api/runs", json_body={"profile_id": profile_id}
                )
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        return thread, result

    def _run_sync(self, auth: dict[str, Any], profile_id: str):
        return self._w1_api(
            auth, "POST", "/api/runs", json_body={"profile_id": profile_id}
        )

    def _query(self, root: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        conn = self._db_connect(root)
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _write_result(
        self,
        check_id: str,
        status: str,
        summary: str,
        evidence: dict[str, Any],
    ) -> None:
        evidence = {"captured_at_utc": _utc_now(), **evidence}
        self._scenario_evidence[check_id] = evidence
        self.write_evidence(
            f"{check_id}-evidence.json",
            json.dumps(evidence, indent=2, sort_keys=True, default=str),
        )
        self.record(check_id, status, summary, **evidence)

    def _blocked(self, check_id: str, summary: str, **evidence: Any) -> None:
        self._write_result(check_id, NOT_RUN, summary, {"blocker": summary, **evidence})

    def _failed(self, check_id: str, summary: str, **evidence: Any) -> None:
        self._write_result(check_id, FAIL, summary, {"failure": summary, **evidence})

    def _passed(self, check_id: str, summary: str, pid: int, **evidence: Any) -> None:
        self._write_result(
            check_id,
            PASS,
            summary,
            {"producer": "target-process", "target_pid": pid, **evidence},
        )

    def _w3_01_lease_fence_epoch(self) -> None:
        check = "W3-01"
        state = FixtureState()
        release = threading.Event()
        state.add("/jobs?page=1", _json_reply(_jobs("lease-a"), release=release))
        with FixtureServer(state) as fixture:
            root = self._new_root(check)
            launcher, auth, pid = self._launch(root)
            conn = self._w1_wait_db(root)
            ids = self._seed_feed_source(conn, fixture.port, prefix="w301")
            conn.close()
            profile = self._create_profile(auth, check)
            thread, api_result = self._run_async(auth, profile)
            self._wait_until(lambda: state.count("/jobs?page=1"), "package fixture dispatch")
            running = self._wait_until(
                lambda: (
                    rows := self._query(
                        root,
                        "SELECT req.id AS request_id,req.run_id,req.status,ra.attempt_id,ra.service_epoch_id FROM scrape_requests req JOIN request_attempts ra ON ra.request_id=req.id WHERE req.source_id=? AND req.status='RUNNING' ORDER BY ra.started_at DESC LIMIT 1",
                        (ids["source_id"],),
                    )
                )
                and rows[0],
                "package-owned RUNNING request",
            )
            self._kill_and_stop(launcher, pid)
            release.set()
            thread.join(timeout=10)
            launcher2, _auth2, pid2 = self._launch(root)
            recovered = self._wait_until(
                lambda: (
                    rows := self._query(
                        root,
                        "SELECT req.status,req.current_attempt_id,ra.outcome,ra.abandoned_reason FROM scrape_requests req JOIN request_attempts ra ON ra.request_id=req.id WHERE ra.attempt_id=?",
                        (running["attempt_id"],),
                    )
                )
                and rows[0].get("outcome")
                and rows[0],
                "startup ownership recovery",
            )
            epochs = self._query(
                root,
                "SELECT id,started_at,ended_at,end_reason FROM service_clock_epochs ORDER BY started_at",
            )
            attempts = self._query(
                root,
                "SELECT attempt_id,outcome,service_epoch_id FROM request_attempts WHERE request_id=? ORDER BY started_at",
                (running["request_id"],),
            )
            self._stop_target(launcher2, pid2)
            self._fixture_bytes += state.total_http_bytes
            evidence = {
                "prior_attempt_outcome": recovered.get("outcome"),
                "fresh_epoch": bool(
                    len(epochs) >= 2 and epochs[-1]["id"] != running["service_epoch_id"]
                ),
                "retry_under_fresh_epoch": any(
                    item["attempt_id"] != running["attempt_id"]
                    and item["service_epoch_id"] != running["service_epoch_id"]
                    for item in attempts
                ),
                "stale_heartbeat_rejected": None,
                "stale_commit_rejected": None,
                "running": running,
                "recovered": recovered,
                "attempts": attempts,
                "epochs": epochs,
                "fixture_requests": state.snapshot(),
                "api_result_after_kill": api_result,
                "target_pid_before": pid,
                "target_pid_after": pid2,
            }
            failures = evaluate_w3_01(evidence)
            hard = [
                item
                for item in failures
                if item.startswith("prior attempt") or item.startswith("fresh_epoch")
            ]
            if hard:
                self._failed(check, "startup lease/service-epoch recovery failed", failures=failures, **evidence)
            else:
                self._blocked(
                    check,
                    "frozen package exposes startup recovery but no external operation that can exercise stale heartbeat/commit authority and resume the same interrupted run",
                    failures=failures,
                    **evidence,
                )

    def _w3_02_retry_after_persistence(self) -> None:
        check = "W3-02"
        state = FixtureState()
        state.add(
            "/jobs?page=1",
            _json_reply(
                {"error": "rate_limited"}, status=429, headers={"Retry-After": "3"}
            ),
            _json_reply(_jobs("retry-a")),
        )
        state.add("/jobs?page=2", _json_reply(_jobs()))
        with FixtureServer(state) as fixture:
            root = self._new_root(check)
            launcher, auth, pid = self._launch(root)
            conn = self._w1_wait_db(root)
            ids = self._seed_feed_source(conn, fixture.port, prefix="w302")
            conn.close()
            profile = self._create_profile(auth, check)
            first_status, first_run = self._run_sync(auth, profile)
            requests = self._query(
                root,
                "SELECT id,status,next_retry_at,attempt_count FROM scrape_requests WHERE source_id=? ORDER BY created_at",
                (ids["source_id"],),
            )
            rate_rows = self._query(
                root,
                "SELECT * FROM binding_host_rate_state WHERE binding_id=?",
                (ids["binding_id"],),
            )
            calls_before = state.count()
            self._stop_target(launcher, pid)
            launcher2, _auth2, pid2 = self._launch(root)
            time.sleep(0.5)
            premature = state.count() - calls_before
            attempts = []
            if requests:
                attempts = self._query(
                    root,
                    "SELECT attempt_id,outcome,failure_kind,service_epoch_id FROM request_attempts WHERE request_id=? ORDER BY started_at",
                    (requests[0]["id"],),
                )
            self._stop_target(launcher2, pid2)
            self._fixture_bytes += state.total_http_bytes
            cooldown_persisted = bool(
                rate_rows
                and rate_rows[0].get("cooldown_until")
                and requests
                and requests[0].get("next_retry_at")
            )
            evidence = {
                "cooldown_persisted": cooldown_persisted,
                "premature_redispatches": premature,
                "same_request_retried": len(attempts) >= 2,
                "later_attempt_succeeded": bool(
                    attempts and attempts[-1].get("outcome") == "SUCCEEDED"
                ),
                "attempt_count": len(attempts),
                "first_http_status": first_status,
                "first_run": first_run,
                "rate_state": rate_rows,
                "request_state": requests,
                "attempt_history": attempts,
                "fixture_requests": state.snapshot(),
                "target_pid_before": pid,
                "target_pid_after": pid2,
            }
            failures = evaluate_w3_02(evidence)
            if not cooldown_persisted or premature != 0:
                self._failed(check, "Retry-After persistence/cooldown behavior failed", failures=failures, **evidence)
            elif failures:
                self._blocked(
                    check,
                    "frozen package persists Retry-After across restart but exposes no packaged operation to resume the same durable request after cooldown",
                    failures=failures,
                    **evidence,
                )
            else:
                self._passed(check, "Retry-After restart behavior proved", pid2, **evidence)

    def _interrupted_feed_probe(self, check: str, *, prefix: str) -> dict[str, Any]:
        state = FixtureState()
        release = threading.Event()
        state.add("/jobs?page=1", _json_reply(_jobs(f"{prefix}-a", f"{prefix}-b")))
        state.add("/jobs?page=2", _json_reply(_jobs(f"{prefix}-c"), release=release))
        state.add("/jobs?page=3", _json_reply(_jobs()))
        with FixtureServer(state) as fixture:
            root = self._new_root(check)
            launcher, auth, pid = self._launch(root)
            conn = self._w1_wait_db(root)
            ids = self._seed_feed_source(conn, fixture.port, prefix=prefix)
            conn.close()
            profile = self._create_profile(auth, check)
            thread, api_result = self._run_async(auth, profile)
            self._wait_until(lambda: state.count("/jobs?page=2"), "second-page target dispatch")
            before = {
                "requests": self._query(
                    root,
                    "SELECT id,run_source_plan_id,request_type,status,payload_json,current_attempt_id FROM scrape_requests WHERE source_id=? ORDER BY created_at",
                    (ids["source_id"],),
                ),
                "cursors": self._query(root, "SELECT * FROM crawl_cursors"),
                "coverages": self._query(root, "SELECT * FROM enumeration_coverage"),
                "observations": self._query(
                    root,
                    "SELECT id,request_id,source_job_id FROM job_observations WHERE source_id=?",
                    (ids["source_id"],),
                ),
            }
            self._kill_and_stop(launcher, pid)
            release.set()
            thread.join(timeout=10)
            launcher2, auth2, pid2 = self._launch(root)
            time.sleep(0.5)
            after = {
                "requests": self._query(
                    root,
                    "SELECT id,run_source_plan_id,request_type,status,payload_json,current_attempt_id FROM scrape_requests WHERE source_id=? ORDER BY created_at",
                    (ids["source_id"],),
                ),
                "cursors": self._query(root, "SELECT * FROM crawl_cursors"),
                "coverages": self._query(root, "SELECT * FROM enumeration_coverage"),
                "observations": self._query(
                    root,
                    "SELECT id,request_id,source_job_id FROM job_observations WHERE source_id=?",
                    (ids["source_id"],),
                ),
            }
            self._stop_target(launcher2, pid2)
            self._fixture_bytes += state.total_http_bytes
            return {
                "target_pid_before": pid,
                "target_pid_after": pid2,
                "root": str(root),
                "source": ids,
                "profile_id": profile,
                "auth_after_restart": auth2,
                "before_kill": before,
                "after_restart": after,
                "fixture_requests": state.snapshot(),
                "api_result_after_kill": api_result,
            }

    def _w3_03_cursor_restart(self) -> None:
        check = "W3-03"
        proof = self._interrupted_feed_probe(check, prefix="w303")
        before = proof["before_kill"]
        after = proof["after_restart"]
        before_obs = sorted(row["source_job_id"] for row in before["observations"])
        after_obs = sorted(row["source_job_id"] for row in after["observations"])
        before_plan_ids = {
            row["run_source_plan_id"] for row in before["requests"] if row["run_source_plan_id"]
        }
        after_plan_ids = {
            row["run_source_plan_id"] for row in after["requests"] if row["run_source_plan_id"]
        }
        before_continuations = [
            row
            for row in before["requests"]
            if row.get("status") in {"PENDING", "RUNNING", "RETRY_WAIT"}
            and row.get("run_source_plan_id") in before_plan_ids
        ]
        checkpoint_persisted = bool(
            before["cursors"]
            and before_continuations
            and all(
                cursor.get("checkpoint_run_source_plan_id") in before_plan_ids
                for cursor in before["cursors"]
            )
        )
        evidence = {
            "checkpoint_persisted": checkpoint_persisted,
            "same_plan_resumed": False,
            "already_succeeded_refetches": max(
                0,
                sum(1 for item in proof["fixture_requests"] if item["path"] == "/jobs?page=1")
                - 1,
            ),
            "union_expected": ["w303-a", "w303-b", "w303-c"],
            "union_observed": after_obs,
            "duplicates": len(after_obs) - len(set(after_obs)),
            "terminal_reached": any(row.get("finalized_at") for row in after["coverages"]),
            "same_plan_identity_survived": before_plan_ids == after_plan_ids,
            **proof,
        }
        failures = evaluate_w3_03(evidence)
        self._blocked(
            check,
            "frozen package recovers durable cursor/frontier state but exposes no packaged operation to resume the same interrupted RunSourcePlan",
            failures=failures,
            **evidence,
        )

    def _w3_04_sitemap_bounded_crawl(self) -> None:
        check = "W3-04"
        # The current frozen package has generic_discovery (DISCOVER-only), but
        # no built-in adapter capable of planning/parsing SOURCE_CRAWL. Exercise
        # the public packaged run surface to prove the capability gap rather
        # than importing the test-only fixture crawler used by automated tests.
        state = FixtureState()
        state.add("/start", FixtureReply(200, b"<html><body>careers</body></html>", "text/html"))
        with FixtureServer(state) as fixture:
            root = self._new_root(check)
            launcher, auth, pid = self._launch(root)
            conn = self._w1_wait_db(root)
            ids = self._seed_generic_discovery_source(conn, fixture.port, prefix="w304")
            conn.close()
            profile = self._create_profile(auth, check)
            status, run = self._run_sync(auth, profile)
            requests = self._query(
                root,
                "SELECT id,request_type,status,last_failure_kind,request_unique_key,payload_json FROM scrape_requests WHERE source_id=? ORDER BY created_at",
                (ids["source_id"],),
            )
            attempts = self._query(
                root,
                "SELECT req.request_type,ra.outcome,ra.failure_kind FROM scrape_requests req LEFT JOIN request_attempts ra ON ra.request_id=req.id WHERE req.source_id=? ORDER BY req.created_at,ra.started_at",
                (ids["source_id"],),
            )
            observations = self._query(
                root,
                "SELECT id,request_id,source_job_id FROM job_observations WHERE source_id=?",
                (ids["source_id"],),
            )
            self._stop_target(launcher, pid)
            self._fixture_bytes += state.total_http_bytes
            evidence = {
                "robots_seen": state.count("/robots.txt") > 0,
                "sitemap_seen": state.count("/sitemap.xml") > 0,
                "expected_observations": 2,
                "observations": len(observations),
                "duplicate_fetches": 0,
                "out_of_scope_fetches": 0,
                "negative_ssrf_denied": False,
                "ordinary_provenance": bool(observations),
                "budget_within_limit": len(requests) <= 8,
                "http_status": status,
                "run": run,
                "requests": requests,
                "attempts": attempts,
                "fixture_requests": state.snapshot(),
                "target_pid": pid,
            }
            failures = evaluate_w3_04(evidence)
            self._blocked(
                check,
                "frozen package exposes generic discovery but no built-in packaged SOURCE_CRAWL adapter/orchestration surface; W3-04 cannot be proved without checkout/test-only crawler code",
                failures=failures,
                **evidence,
            )

    def _w3_05_304_membership(self) -> None:
        check = "W3-05"
        state = FixtureState()
        state.add(
            "/jobs?page=1",
            _json_reply(
                _jobs("cache-a", "cache-b"), headers={"ETag": '"w3-list-v1"'}
            ),
            FixtureReply(304, b"", "application/json", headers={"ETag": '"w3-list-v1"'}),
        )
        state.add("/jobs?page=2", _json_reply(_jobs()))
        with FixtureServer(state) as fixture:
            root = self._new_root(check)
            launcher, auth, pid = self._launch(root)
            conn = self._w1_wait_db(root)
            ids = self._seed_feed_source(conn, fixture.port, prefix="w305")
            conn.close()
            profile = self._create_profile(auth, check)
            first_status, first_run = self._run_sync(auth, profile)
            before = self._query(
                root,
                "SELECT id,source_job_id,presence_state,content_revision FROM job_sources WHERE source_id=? ORDER BY source_job_id",
                (ids["source_id"],),
            )
            self._stop_target(launcher, pid)
            launcher2, auth2, pid2 = self._launch(root)
            second_status, second_run = self._run_sync(auth2, profile)
            after = self._query(
                root,
                "SELECT id,source_job_id,presence_state,content_revision FROM job_sources WHERE source_id=? ORDER BY source_job_id",
                (ids["source_id"],),
            )
            representations = self._query(
                root,
                "SELECT id,body_hash,etag,membership_complete FROM cache_representation WHERE binding_revision_id=? AND pruned_at IS NULL ORDER BY stored_at",
                (ids["revision_id"],),
            )
            reuse_evidence = self._query(
                root,
                "SELECT e.request_id,e.detail_json,f.was_304 FROM acquisition_evidence e "
                "JOIN fetch_attempts f ON f.id=e.fetch_attempt_id "
                "JOIN scrape_requests req ON req.id=e.request_id "
                "WHERE req.source_id=? AND e.kind='REVIEW' AND e.ref='cache://304_REUSE' "
                "AND f.was_304=1 ORDER BY e.observed_at,e.id",
                (ids["source_id"],),
            )
            reused_representation_id = None
            if reuse_evidence:
                try:
                    reuse_detail = json.loads(reuse_evidence[-1].get("detail_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    reuse_detail = {}
                candidate_representation_id = reuse_detail.get("cache_representation_id")
                if isinstance(candidate_representation_id, str) and candidate_representation_id:
                    reused_representation_id = candidate_representation_id
            membership = self._query(
                root,
                "SELECT m.representation_id,m.stable_source_identity FROM cache_representation_membership m "
                "WHERE m.representation_id=? ORDER BY m.stable_source_identity",
                (reused_representation_id,),
            ) if reused_representation_id else []
            absence = self._query(
                root,
                "SELECT cpa.* FROM coverage_presence_application cpa JOIN enumeration_coverage c ON c.id=cpa.coverage_id WHERE c.binding_revision_id=? AND cpa.decision IN ('UNCERTAIN','EXPIRED')",
                (ids["revision_id"],),
            )
            self._stop_target(launcher2, pid2)
            self._fixture_bytes += state.total_http_bytes
            first_page_calls = [
                item for item in state.snapshot() if item["path"] == "/jobs?page=1"
            ]
            conditional = len(first_page_calls) >= 2 and first_page_calls[1][
                "headers"
            ].get("if-none-match") == '"w3-list-v1"'
            before_members = [row["source_job_id"] for row in before]
            after_members = [row["source_job_id"] for row in after]
            reused_members = [row["stable_source_identity"] for row in membership]
            membership_representation_ids = [row["representation_id"] for row in membership]
            before_revision = {row["id"]: row["content_revision"] for row in before}
            after_revision = {row["id"]: row["content_revision"] for row in after}
            evidence = {
                "expected_members": ["cache-a", "cache-b"],
                "conditional_304": conditional
                and any(item["status"] == 304 for item in first_page_calls),
                "before_members": before_members,
                "after_members": after_members,
                "membership_reused": reused_members,
                "reused_representation_id": reused_representation_id,
                "membership_representation_ids": membership_representation_ids,
                "content_revisions_unchanged": before_revision == after_revision,
                "members_active": bool(after) and all(
                    row.get("presence_state") == "ACTIVE" for row in after
                ),
                "absence_transitions": len(absence),
                "compatible_representation": any(
                    row.get("id") == reused_representation_id
                    and row.get("membership_complete") == 1
                    and row.get("etag") == '"w3-list-v1"'
                    for row in representations
                ),
                "first_http_status": first_status,
                "first_run": first_run,
                "second_http_status": second_status,
                "second_run": second_run,
                "presence_before": before,
                "presence_after": after,
                "representations": representations,
                "reuse_evidence": reuse_evidence,
                "membership_rows": membership,
                "absence_rows": absence,
                "fixture_requests": state.snapshot(),
                "target_pid_before": pid,
                "target_pid_after": pid2,
            }
            failures = evaluate_w3_05(evidence)
            if failures:
                self._failed(check, "packaged 304 retained-membership proof failed", failures=failures, **evidence)
            else:
                self._passed(check, "packaged 304 retained A/B membership without fabricated revision/absence", pid2, **evidence)

    def _w3_06_coverage_absence_safety(self) -> None:
        check = "W3-06"
        proof = self._interrupted_feed_probe(check, prefix="w306")
        root = Path(proof["root"])
        after = proof["after_restart"]
        premature = self._query(
            root,
            "SELECT id,presence_state,last_absence_coverage_id FROM job_sources WHERE presence_state <> 'ACTIVE'",
        )
        open_before = [row for row in proof["before_kill"]["coverages"] if not row.get("finalized_at")]
        open_after = [row for row in after["coverages"] if not row.get("finalized_at")]
        evidence = {
            "same_generation_resumed": False,
            "premature_absence": len(premature),
            "later_complete": False,
            "absence_application_counts": [],
            "same_scope_only": False,
            "replay_duplicate_transitions": 0,
            "open_coverages_before": open_before,
            "open_coverages_after": open_after,
            "premature_absence_rows": premature,
            **proof,
        }
        failures = evaluate_w3_06(evidence)
        if premature:
            self._failed(check, "interrupted packaged coverage applied premature absence", failures=failures, **evidence)
        else:
            self._blocked(
                check,
                "frozen package preserves interrupted coverage safely but exposes no packaged operation to continue/finalize the same durable generation and prove exactly-once same-scope absence",
                failures=failures,
                **evidence,
            )

    def _w3_07_fallback_truth(self) -> None:
        check = "W3-07"
        state = FixtureState()
        state.add("/jobs?page=1", _json_reply({"error": "source"}, status=503))
        with FixtureServer(state) as fixture:
            root = self._new_root(check)
            launcher, auth, pid = self._launch(root)
            conn = self._w1_wait_db(root)
            for rank in range(3):
                self._seed_feed_source(conn, fixture.port, prefix=f"w307r{rank}")
            conn.close()
            profile = self._create_profile(auth, check)
            status, run = self._run_sync(auth, profile)
            plans = self._query(
                root,
                "SELECT source_plan_group_id,fallback_rank,group_outcome,binding_id FROM run_source_plans ORDER BY source_plan_group_id,fallback_rank",
            )
            groups = self._query(root, "SELECT * FROM source_plan_group_state")
            final_runs = self._query(root, "SELECT id,status FROM scrape_runs ORDER BY created_at DESC LIMIT 1")
            self._stop_target(launcher, pid)
            self._fixture_bytes += state.total_http_bytes
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in plans:
                grouped.setdefault(str(row["source_plan_group_id"]), []).append(row)
            candidate = next(
                (rows for rows in grouped.values() if [r["fallback_rank"] for r in rows] == [0, 1, 2]),
                [],
            )
            evidence = {
                "pinned_ranks": [row["fallback_rank"] for row in candidate],
                "rank_outcomes": [row["group_outcome"] for row in candidate],
                "activated_ranks": [],
                "final_group_outcome": None,
                "final_run_terminal": bool(
                    final_runs
                    and final_runs[0]["status"] in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
                ),
                "final_run_status": final_runs[0]["status"] if final_runs else None,
                "http_status": status,
                "run": run,
                "plans": plans,
                "groups": groups,
                "fixture_requests": state.snapshot(),
                "target_pid": pid,
            }
            failures = evaluate_w3_07(evidence)
            self._blocked(
                check,
                "packaged /api/runs creates independent rank-0 source groups and exposes no public/operator surface for one pinned fallback group with ranks 0/1/2",
                failures=failures,
                **evidence,
            )

    def _w3_08_cancellation_obligation(self) -> None:
        check = "W3-08"
        state = FixtureState()
        release = threading.Event()
        state.add("/jobs?page=1", _json_reply(_jobs("cancel-a")))
        state.add("/jobs?page=2", _json_reply(_jobs("cancel-b"), release=release))
        with FixtureServer(state) as fixture:
            root = self._new_root(check)
            launcher, auth, pid = self._launch(root)
            conn = self._w1_wait_db(root)
            ids = self._seed_feed_source(conn, fixture.port, prefix="w308")
            conn.close()
            profile = self._create_profile(auth, check)
            run_thread, run_result = self._run_async(auth, profile)
            self._wait_until(lambda: state.count("/jobs?page=2"), "blocked second page")
            run_row = self._wait_until(
                lambda: (
                    rows := self._query(
                        root,
                        "SELECT id,status,cancel_requested_at FROM scrape_runs WHERE status='RUNNING' ORDER BY created_at DESC LIMIT 1",
                    )
                )
                and rows[0],
                "RUNNING scrape run",
            )
            accepted = self._query(
                root,
                "SELECT id,request_id,source_job_id FROM job_observations WHERE run_id=?",
                (run_row["id"],),
            )
            obligations_before = self._query(
                root,
                "SELECT id,request_type,status FROM scrape_requests WHERE run_id=? AND request_type IN ('RECONCILE','ELIGIBILITY','SCORE') ORDER BY created_at,id",
                (run_row["id"],),
            )

            cancel_result: dict[str, Any] = {}
            started = time.perf_counter()

            def cancel_call() -> None:
                try:
                    cancel_result["response"] = self._w1_api(
                        auth, "POST", f"/api/runs/{run_row['id']}/cancel"
                    )
                    cancel_result["elapsed_ms"] = (time.perf_counter() - started) * 1000
                except Exception as exc:  # noqa: BLE001
                    cancel_result["error"] = f"{type(exc).__name__}: {exc}"
                    cancel_result["elapsed_ms"] = (time.perf_counter() - started) * 1000

            cancel_thread = threading.Thread(target=cancel_call, daemon=True)
            cancel_thread.start()
            cancel_thread.join(timeout=2.0)
            concurrent_cancel_completed = not cancel_thread.is_alive()

            # Do not release page 2 before establishing whether the packaged
            # service can accept cancellation independently of the synchronous
            # run request. Kill the exact service to preserve this evidence.
            self._kill_and_stop(launcher, pid)
            release.set()
            run_thread.join(timeout=10)
            cancel_thread.join(timeout=10)
            calls_at_kill = state.count()

            launcher2, auth2, pid2 = self._launch(root)
            # After restart the public endpoint is callable, but this occurs
            # after startup recovery may already have drained local obligations.
            restart_cancel_started = time.perf_counter()
            restart_cancel_status, restart_cancel_payload = self._w1_api(
                auth2, "POST", f"/api/runs/{run_row['id']}/cancel"
            )
            restart_cancel_ms = (time.perf_counter() - restart_cancel_started) * 1000
            calls_after_cancel = state.count() - calls_at_kill
            run_after = self._query(
                root,
                "SELECT id,status,cancel_requested_at FROM scrape_runs WHERE id=?",
                (run_row["id"],),
            )
            obligations_after = self._query(
                root,
                "SELECT request_type,status,COUNT(*) AS n FROM scrape_requests WHERE run_id=? AND request_type IN ('RECONCILE','ELIGIBILITY','SCORE') GROUP BY request_type,status ORDER BY request_type,status",
                (run_row["id"],),
            )
            self._stop_target(launcher2, pid2)
            self._fixture_bytes += state.total_http_bytes
            obligation_counts_by_type: dict[str, int] = {}
            for row in obligations_after:
                obligation_counts_by_type[row["request_type"]] = (
                    obligation_counts_by_type.get(row["request_type"], 0) + int(row["n"])
                )
            expected_obligation_counts = {
                "RECONCILE": len(accepted),
                "ELIGIBILITY": len(accepted),
                "SCORE": len(accepted),
            }
            obligations_all_succeeded = bool(obligations_after) and all(
                row["status"] == "SUCCEEDED" for row in obligations_after
            )
            exactly_once = (
                bool(accepted)
                and obligation_counts_by_type == expected_obligation_counts
                and obligations_all_succeeded
            )
            measured = (
                cancel_result.get("elapsed_ms")
                if concurrent_cancel_completed and "response" in cancel_result
                else None
            )
            if _numeric(measured):
                self._metrics["cancellation_responsiveness_ms"] = measured
            evidence = {
                "cancel_endpoint_status": (
                    cancel_result.get("response", [None])[0]
                    if concurrent_cancel_completed and "response" in cancel_result
                    else restart_cancel_status
                ),
                "cancel_requested": bool(
                    run_after and run_after[0].get("cancel_requested_at")
                ),
                "accepted_observations": len(accepted),
                "cancellation_before_local_completion": bool(
                    concurrent_cancel_completed
                    and any(row["status"] != "SUCCEEDED" for row in obligations_before)
                ),
                "source_io_after_cancel": calls_after_cancel,
                "local_obligations_exactly_once": exactly_once,
                "obligation_counts_by_type": obligation_counts_by_type,
                "obligations_all_succeeded": obligations_all_succeeded,
                "cancellation_responsiveness_ms": measured,
                "final_run_status": run_after[0]["status"] if run_after else None,
                "final_state_honest": bool(
                    run_after and run_after[0]["status"] == "CANCELLED"
                ),
                "concurrent_cancel_completed": concurrent_cancel_completed,
                "concurrent_cancel_result": cancel_result,
                "restart_cancel_status": restart_cancel_status,
                "restart_cancel_payload": restart_cancel_payload,
                "restart_cancel_ms": restart_cancel_ms,
                "run_before": run_row,
                "run_after": run_after,
                "accepted": accepted,
                "obligations_before": obligations_before,
                "obligations_after": obligations_after,
                "fixture_requests": state.snapshot(),
                "run_result_after_kill": run_result,
                "target_pid_before": pid,
                "target_pid_after": pid2,
            }
            failures = evaluate_w3_08(evidence)
            if concurrent_cancel_completed and not failures:
                self._passed(check, "packaged cancellation/local-obligation recovery proved", pid2, **evidence)
            else:
                self._blocked(
                    check,
                    "packaged synchronous /api/runs execution does not expose a concurrent cancellation boundary before local processing; post-restart cancellation is too late to satisfy W3-08/VER-15 responsiveness",
                    failures=failures,
                    **evidence,
                )

    def _w3_09_temporal_availability(self) -> None:
        check = "W3-09"
        # Real packaged fetch timestamps define availability effective time.
        # The package exposes no native fixture/test seam for delivering an
        # older effective event after a newer event across restart; doing this
        # with checkout helpers or SQL projection writes would invalidate W3.
        root = self._new_root(check)
        launcher, _auth, pid = self._launch(root)
        conn = self._w1_wait_db(root)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(job_sources)").fetchall()]
        conn.close()
        self._stop_target(launcher, pid)
        evidence = {
            "trusted_active": False,
            "aggregator_conflict": False,
            "reverse_completion_observed": False,
            "restart_between_evidence": False,
            "older_effective_evidence": False,
            "canonical_active": False,
            "aggregator_did_not_close": False,
            "availability_columns_present": all(
                name in columns
                for name in (
                    "availability_effective_at",
                    "availability_received_at",
                    "availability_evidence_kind",
                )
            ),
            "target_pid": pid,
        }
        failures = evaluate_w3_09(evidence)
        self._blocked(
            check,
            "frozen package has no packaged/native seam to inject older effective availability evidence after newer evidence across a restart boundary; SQL projection fabrication and checkout helpers are forbidden",
            failures=failures,
            **evidence,
        )

    def _doctor_health(self, root: Path) -> dict[str, Any]:
        proc = subprocess.run(
            self.app_command("--doctor", "--data-root", str(root), "--json"),
            capture_output=True,
            text=True,
            timeout=600,
            env=self._spawn_env(),
        )
        try:
            report = json.loads(proc.stdout)
        except (ValueError, TypeError):
            report = {"checks": [], "parse_error": "Doctor did not emit JSON"}
        if not isinstance(report, dict):
            report = {"checks": [], "parse_error": "Doctor JSON was not an object"}
        report["reported_exit_code"] = report.get("exit_code")
        report["exit_code"] = proc.returncode
        if proc.stderr:
            report["stderr"] = proc.stderr[-4000:]
        return report

    def _database_health(self, name: str, root: Path) -> dict[str, Any]:
        # Use an independent read connection only for integrity/FK/application
        # consistency. Effective PRAGMAs are taken from packaged Doctor, not
        # from this observer connection.
        conn = sqlite3.connect(self._database_path(root), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            fk = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
            problems: list[str] = []
            for label, sql in (
                ("events with empty kind", "SELECT COUNT(*) FROM events WHERE kind = ''"),
                ("events with empty message", "SELECT COUNT(*) FROM events WHERE message = ''"),
            ):
                try:
                    count = int(conn.execute(sql).fetchone()[0])
                except sqlite3.OperationalError:
                    continue
                if count:
                    problems.append(f"{label}: {count}")
            fetch_row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(bytes_downloaded,0)),0) AS bytes "
                "FROM fetch_attempts"
            ).fetchone()
            return {
                "name": name,
                "root": str(root),
                "integrity_check": integrity,
                "foreign_key_check": fk,
                "application_consistency_ok": not problems,
                "application_consistency_problems": problems,
                "fetch_attempt_count": int(fetch_row["n"]),
                "fetch_bytes_total": int(fetch_row["bytes"]),
            }
        finally:
            conn.close()

    def _w3_10_post_workload_health(self) -> None:
        check = "W3-10"
        # Use a populated workload root when available. W3-05 is preferred
        # because it contains real observations/cache/revalidation state.
        candidate_roots = [
            root
            for check_id in W3_IDS[:-1]
            for root in self._workload_roots.get(check_id, [])
            if self._database_path(root).exists()
        ]
        if not candidate_roots:
            self._blocked(check, "no prior W3 workload database exists for health/resource evaluation")
            return
        primary = next(
            (
                root
                for root in self._workload_roots.get("W3-05", [])
                if self._database_path(root).exists()
            ),
            candidate_roots[0],
        )
        launcher, auth, pid = self._launch(primary)
        latencies: list[float] = []
        for _ in range(20):
            started = time.perf_counter()
            status, _payload = self._w1_api(auth, "GET", "/api/search?q=engineer")
            elapsed = (time.perf_counter() - started) * 1000
            if status != 200:
                latencies = []
                break
            latencies.append(elapsed)
        doctor = self._doctor_health(primary)
        during_inventory = self.list_processes()
        self.write_evidence("process-inventory-during.txt", "\n".join(during_inventory))
        self._stop_target(launcher, pid)
        time.sleep(1.0)
        snapshot = self._process_snapshot()
        residual = self._residual_owned_pids(snapshot)
        after_inventory = self.list_processes()
        self.write_evidence("process-inventory-after.txt", "\n".join(after_inventory))

        workload_databases = []
        for check_id in W3_IDS[:-1]:
            for index, root in enumerate(self._workload_roots.get(check_id, []), start=1):
                if self._database_path(root).exists():
                    workload_databases.append(
                        self._database_health(f"{check_id}:{index}", root)
                    )

        p95 = None
        if latencies:
            ordered = sorted(latencies)
            p95 = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]
        idle = max(self._idle_service_memory) if self._idle_service_memory else None
        cancellation = self._metrics.get("cancellation_responsiveness_ms")
        durable_response_body_bytes = sum(
            int(database.get("fetch_bytes_total") or 0)
            for database in workload_databases
        )
        resources = {
            "recorded_at_utc": _utc_now(),
            "procedure": {
                "process_memory": "external OS process snapshot; service idle working set and descendant-tree working sets",
                "temporary_disk": "0.75s sampled absolute peak W3 data-root usage during service workloads",
                "wal": "0.75s sampled peak WAL growth above each service-lifetime baseline during W3 workloads",
                "http_bytes": "deterministic loopback-fixture HTTP message bytes for all observed W3 acquisition requests/responses, including repeated retry/subrequest/redirect hops that reach the fixture; durable fetch_attempt response-body bytes are retained as corroboration",
                "search_latency": "20 authenticated packaged /api/search requests against a populated W3 workload root",
                "cancellation": "measured packaged cancellation response before local completion; absent if no such boundary exists",
            },
            "idle_service_memory_bytes": idle,
            "browser_process_tree_peak_memory_bytes": self._browser_tree_peak_bytes,
            "p95_local_search_latency_ms": p95,
            "peak_temporary_disk_bytes": self._peak_temp_bytes,
            "wal_growth_bytes": self._peak_wal_bytes,
            "cancellation_responsiveness_ms": cancellation,
            "total_http_bytes": self._fixture_bytes,
            "http_bytes_source": "fixture_http_messages",
            "durable_response_body_bytes": durable_response_body_bytes,
            "browser_cleanup_ok": not residual,
        }
        processes = {
            "tracked_target_pids": sorted(
                {pid for pid, _creation in self._owned_process_identities}
            ),
            "tracked_target_identities": [
                {"pid": pid, "creation": creation}
                for pid, creation in sorted(self._owned_process_identities)
            ],
            "residual_target_pids": residual,
            "browser_cleanup_ok": not residual,
        }
        approval = self.resource_approval
        failures = evaluate_w3_10(
            doctor, workload_databases, processes, resources, approval
        )
        self.write_evidence("slice3-doctor.json", json.dumps(doctor, indent=2, sort_keys=True))
        self.write_evidence(
            "resource-measurements.json", json.dumps(resources, indent=2, sort_keys=True)
        )
        self.write_evidence(
            "workload-database-health.json",
            json.dumps(workload_databases, indent=2, sort_keys=True),
        )
        evidence = {
            "producer": "target-process-and-external-observer",
            "target_pid": pid,
            "primary_workload_root": str(primary),
            "doctor": doctor,
            "workload_databases": workload_databases,
            "processes": processes,
            "resources": resources,
            "resource_approval": approval,
            "failures": failures,
        }
        outcome = classify_w3_10_failures(failures)
        if outcome == PASS:
            self._passed(check, "post-workload health/resource gate passed", pid, **evidence)
        elif outcome == NOT_RUN:
            self._blocked(
                check,
                "VER-15 resource record is incomplete: measured evidence and explicit package-bound ceiling approval are both required",
                **evidence,
            )
        else:
            self._failed(check, "post-workload health/resource gate failed", **evidence)

    def run_slice3(self) -> int:
        self._require_exe_binding()
        self._assert_exe_no_checkout_behavior()
        started = _utc_now()
        self.write_evidence("process-inventory-before.txt", "\n".join(self.list_processes()))
        try:
            for check_id, method_name in CHECK_METHODS.items():
                try:
                    getattr(self, method_name)()
                    self._assert_exe_no_checkout_behavior()
                except Exception as exc:  # noqa: BLE001
                    self._failed(
                        check_id,
                        f"{check_id} harness execution failed: {type(exc).__name__}: {exc}",
                    )
                if check_id not in self.results:
                    self._blocked(
                        check_id, "harness did not produce the required independent result"
                    )
        finally:
            for pid in list(self._active_samplers):
                self._stop_sampler(pid)
            for temporary in reversed(self._temporary_roots):
                try:
                    temporary.cleanup()
                except OSError:
                    pass

        tests = {check_id: self.results[check_id]["status"] for check_id in W3_IDS}
        incomplete = [check_id for check_id, status in tests.items() if status != PASS]
        promotion_state = (
            "BLOCKED_NATIVE_ACCEPTANCE"
            if incomplete
            else "PENDING_NATIVE_RUN"
            if self.target != "exe"
            else "PENDING_PROMOTION_REVIEW"
        )
        record = {
            "schema_version": 1,
            "slice": "3",
            "spec_version": "0.3.1.3",
            "target": self.target,
            "candidate_commit": self.candidate_commit,
            "build_id": self.build_id,
            "package_sha256": self.package_sha256,
            "package_verification": self._package_verification,
            "resource_approval_present": bool(
                isinstance(self.resource_approval, dict)
                and self.resource_approval.get("approved") is True
            ),
            "started_at_utc": started,
            "finished_at_utc": _utc_now(),
            "tests": tests,
            "details": {check_id: self.results[check_id] for check_id in W3_IDS},
            "promotion": promotion_state,
            "unresolved_critical_findings": len(incomplete),
        }
        self.write_evidence(
            "native-acceptance.json",
            json.dumps(record, indent=2, sort_keys=True, default=str),
        )
        print(f"[slice3-native] W3 results: {json.dumps(tests, sort_keys=True)}", flush=True)
        if incomplete:
            print(f"[slice3-native] incomplete checks: {incomplete}", flush=True)
            return 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Slice-3 packaged/native acceptance companion (W3-01..W3-10)"
    )
    parser.add_argument("--target", choices=("exe", "dev"), default="exe")
    parser.add_argument("--exe", type=Path, default=None, help="dist/JobScraper package directory")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--candidate-commit")
    parser.add_argument("--build-id")
    parser.add_argument("--package-sha256")
    parser.add_argument(
        "--resource-approval",
        type=Path,
        default=None,
        help=(
            "JSON approval bound to the exact candidate/build/package with "
            "approved=true and ceilings for every VER-15 numeric measurement"
        ),
    )
    args = parser.parse_args()
    if args.target == "exe" and args.exe is None:
        parser.error("--target exe requires --exe <package-dir>")
    approval = None
    if args.resource_approval is not None:
        try:
            approval = load_resource_approval(args.resource_approval)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(f"invalid --resource-approval: {exc}")
    harness = Slice3Harness(
        args.target,
        args.exe,
        args.evidence_dir,
        candidate_commit=args.candidate_commit,
        build_id=args.build_id,
        package_sha256=args.package_sha256,
        resource_approval=approval,
    )
    return harness.run_slice3()


if __name__ == "__main__":
    raise SystemExit(main())
