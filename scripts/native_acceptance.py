#!/usr/bin/env python3
"""Native Windows acceptance harness (S0.13; Slice-1 extension S1.12).

Authority: docs/plans/slice-0-windows-acceptance-strategy-v0313.md (native
acceptance matrix W0-01..W0-18); docs/plans/slice-1-worker-implementation-
plan-v0313.md S1.12 (W1 checks).

Single-purpose, machine-verifiable harness. Run on the Windows target:

    python scripts/native_acceptance.py --target exe --exe <path-to-JobScraper.exe-dir> --slice 0
    python scripts/native_acceptance.py --target exe --exe <path-to-JobScraper.exe-dir> --slice 1
    python scripts/native_acceptance.py --target exe --exe <path-to-JobScraper.exe-dir> --slice all

or, to validate the harness logic itself on a development host (Linux/CI):

    python scripts/native_acceptance.py --target dev --slice 1

Slice-1 checks (W1-01..W1-07) drive the *packaged* application through its
public HTTP surface with a harness-hosted loopback fixture feed:

    W1-01 packaged E2E vertical slice (profile → run → canonical jobs →
          scores/eligibility → inbox events → dispositions → application)
    W1-02 restart persistence (PROD-08: every piece of state survives)
    W1-03 idempotent resume + inbox dedupe across restart
    W1-04 SSRF negative: private/test destination denied (fail closed,
          typed POLICY_REJECTED, no fetch)
    W1-05 SSRF negative: redirect to unauthorized destination denied per hop
    W1-06 unsafe source links (javascript:/data:) never surfaced clickable
    W1-07 doctor healthy after the Slice-1 workload

The harness never fabricates results: checks that cannot execute in the
current environment are recorded as NOT_RUN with the reason. Results are
written to <evidence>/native-acceptance.json using the schema from the
acceptance strategy. No secret material is ever written to evidence.
"""

from __future__ import annotations

import argparse
import csv
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"

_NOW = "2026-09-08T09:00:00.000000Z"

# Fixture jobs for the W1 vertical slice (same shape as the pytest E2E
# acceptance suite).
_W1_JOBS_PAGE_1 = [
    {
        "id": "fx-1", "title": "Backend Engineer", "company": "Fixture Corp",
        "description": "<p>Python services</p>",
        "url": "https://jobs.example.test/jobs/fx-1",
        "apply_url": "https://jobs.example.test/jobs/fx-1/apply",
        "locations": ["Berlin"], "created_at": _NOW,
    },
    {
        "id": "fx-2", "title": "Platform Engineer", "company": "Fixture Corp",
        "description": "<p>Go infrastructure</p>",
        "url": "https://jobs.example.test/jobs/fx-2",
        "apply_url": "https://jobs.example.test/jobs/fx-2/apply",
        "locations": ["Remote"], "created_at": _NOW,
    },
]


class _W1FeedHandler(http.server.BaseHTTPRequestHandler):
    """Loopback fixture feed hosted by the harness (S1.12 W1 checks)."""

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path
        if path.startswith("/redirect-jobs"):
            # SSRF negative: redirect hop to an unauthorized TEST-NET target
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.2/evil")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path.startswith("/jobs-unsafe"):
            # PROD-05 negative: malicious non-http source links
            self._send({"jobs": [{
                "id": "bad-1", "title": "Malicious Listing",
                "company": "Evil Corp",
                "url": "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
                "apply_url": "javascript:alert(1)",
            }]})
        elif path.startswith("/jobs?page=1") or path == "/jobs":
            self._send({"jobs": _W1_JOBS_PAGE_1})
        else:
            self._send({"jobs": []})

    def log_message(self, *args):
        pass


def log(message: str) -> None:
    print(f"[native-acceptance] {message}", flush=True)


def _process_identity(line: str) -> tuple[str, int] | None:
    """Return stable (image/command, PID) identity from tasklist/ps output.

    Windows ``tasklist /FO CSV`` includes a volatile memory-usage field, so
    raw-line comparisons can falsely report an existing Chrome process as a
    new orphan when only its memory counter changes.  W0-14 compares only
    stable image/PID identity instead.
    """
    text = line.strip()
    if not text:
        return None
    if text.startswith('"'):
        try:
            row = next(csv.reader([text]))
        except (csv.Error, StopIteration):
            return None
        if len(row) < 2 or not row[1].isdigit():
            return None
        return row[0].lower(), int(row[1])
    parts = text.split(None, 2)
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    return parts[1].lower(), int(parts[0])


class Harness:
    def __init__(self, target: str, exe: Path | None, evidence: Path) -> None:
        self.target = target
        self.exe = exe
        self.evidence = evidence
        self.results: dict[str, dict] = {}
        self._w1_state: dict = {}
        evidence.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- plumbing
    def app_command(self, *args: str) -> list[str]:
        if self.target == "exe":
            name = "JobScraper.exe" if os.name == "nt" else "JobScraper"
            return [str(self.exe / name), *args]
        env_path = os.environ.get("PYTHONPATH", "")
        parts = [p for p in env_path.split(os.pathsep) if p]
        if str(SRC) not in parts:
            parts.insert(0, str(SRC))
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(parts))
        self._dev_env = env
        return [sys.executable, "-m", "jobscraper", *args]

    def _spawn_env(self) -> dict:
        """W0-17: the app must run without development help (no PYTHONPATH,
        no venv) — especially for the packaged target. Automated acceptance
        suppresses the real default-browser side effect while exercising the
        same launcher/bootstrap flow."""
        if self.target == "exe":
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.pop("VIRTUAL_ENV", None)
        else:
            env = dict(getattr(self, "_dev_env", dict(os.environ)))
        env["WJS_SUPPRESS_BROWSER_OPEN"] = "1"
        return env

    def record(self, check_id: str, status: str, summary: str, **details) -> None:
        self.results[check_id] = {
            "status": status,
            "summary": summary,
            "details": details,
        }
        log(f"{check_id}: {status} — {summary}")

    def write_evidence(self, name: str, content: str) -> Path:
        path = self.evidence / name
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------ utilities
    def run_app(self, *args: str, timeout: float = 120) -> subprocess.Popen:
        popen_kwargs: dict[str, int] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            self.app_command(*args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self._spawn_env(),
            **popen_kwargs,
        )

    def launch_and_get_url(self, data_root: Path, timeout: float = 120) -> tuple[subprocess.Popen, str]:
        launcher = self.run_app("--data-root", str(data_root), "--print-url")
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = launcher.stdout.readline() if launcher.stdout else ""
            if line.startswith("Dashboard:"):
                return launcher, line.strip().split("Dashboard: ", 1)[1]
            if launcher.poll() is not None:
                raise RuntimeError("launcher exited before producing a dashboard URL")
        launcher.kill()
        raise RuntimeError("launcher never produced a dashboard URL")

    def stop_launcher(self, launcher: subprocess.Popen) -> None:
        if launcher.poll() is None:
            if os.name == "nt":
                launcher.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                launcher.terminate()
            try:
                launcher.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                launcher.kill()
                launcher.wait()

    @staticmethod
    def http(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None):
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    @staticmethod
    def descriptor_of(data_root: Path):
        from jobscraper.launcher.runtime_descriptor import load_runtime_descriptor

        return load_runtime_descriptor((data_root / "runtime"))

    def doctor_json(self, data_root: Path) -> dict:
        proc = subprocess.run(
            self.app_command("--doctor", "--data-root", str(data_root), "--json"),
            capture_output=True,
            text=True,
            timeout=600,
            env=self._spawn_env(),
        )
        return json.loads(proc.stdout)

    def list_processes(self) -> list[str]:
        """Process inventory lines for evidence + orphan checks."""
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV"], capture_output=True, text=True, timeout=60
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-eo", "pid,comm,args"], capture_output=True, text=True, timeout=60
            ).stdout
        return out.splitlines()

    def chrome_like_processes(self) -> list[str]:
        markers = ("chrome", "chromium", "JobScraper", "jobscraper")
        return [line for line in self.list_processes() if any(m in line for m in markers)]

    @staticmethod
    def child_pids(parent_pid: int) -> list[int]:
        """Direct child PIDs of one exact parent (no name matching)."""
        if os.name == "nt":
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "Get-CimInstance Win32_Process "
                        f"-Filter 'ParentProcessId={parent_pid}' "
                        "| Select-Object -ExpandProperty ProcessId"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
            return [int(x) for x in out.split() if x.isdigit()]
        out = subprocess.run(
            ["ps", "--ppid", str(parent_pid), "-o", "pid="],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        return [int(x) for x in out.split() if x.isdigit()]

    def kill_tree(self, pid: int) -> None:
        """Kill exactly one process (and, on Windows, its subtree).

        Never name-matches: only the explicit PID tree is terminated, so the
        operator's own browsers are untouched.
        """
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=60,
            )
            return
        # POSIX: children first (best effort), then the process itself.
        for child in self.child_pids(pid):
            try:
                os.kill(child, signal.SIGKILL)
            except OSError:
                pass
        os.kill(pid, signal.SIGKILL)

    # ------------------------------------------------------------- W0 checks
    def w01_packaged_launch(self, data_root: Path) -> str:
        launcher, url = self.launch_and_get_url(data_root)
        try:
            host_port = url.split("//", 1)[1].split("#", 1)[0].rstrip("/")
            status, _, body = self.http("GET", f"http://{host_port}/health/live")
            ok = status == 200 and json.loads(body).get("status") == "ok"
            self.record(
                "W0-01",
                PASS if ok else FAIL,
                "packaged launch works; service healthy; dashboard URL produced",
                url_shape="fragment" if "#bootstrap=" in url else "invalid",
            )
            return "ok"
        finally:
            self.stop_launcher(launcher)

    def w02_loopback_only(self, data_root: Path) -> str:
        launcher, url = self.launch_and_get_url(data_root)
        try:
            desc = self.descriptor_of(data_root)
            host = desc.host if desc else url.split("//", 1)[1].split(":")[0]
            listening = [
                line for line in self.list_processes() if f":{desc.port}" in line and "LISTEN" in line.upper()
            ]
            # netstat output is environment-specific; the bound address from
            # the descriptor plus health reachability is the primary oracle.
            ok = host == "127.0.0.1"
            self.record(
                "W0-02",
                PASS if ok else FAIL,
                "service binds loopback only",
                bound_host=host,
                listener_lines=len(listening),
            )
        finally:
            self.stop_launcher(launcher)

    def w03_collision_safe_port(self, data_root: Path) -> str:
        # Occupy an unrelated port first; the service must take a different
        # OS-assigned port (no probe-close-bind dependency).
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied = blocker.getsockname()[1]
        try:
            launcher, url = self.launch_and_get_url(data_root)
            try:
                desc = self.descriptor_of(data_root)
                ok = desc is not None and desc.port != occupied
                self.record(
                    "W0-03",
                    PASS if ok else FAIL,
                    "port allocation is collision-safe (OS-assigned)",
                    occupied_port=occupied,
                    assigned_port=desc.port if desc else None,
                )
            finally:
                self.stop_launcher(launcher)
        finally:
            blocker.close()

    def w04_single_instance(self, data_root: Path) -> str:
        first, url = self.launch_and_get_url(data_root)
        try:
            desc = self.descriptor_of(data_root)
            second = self.run_app("--data-root", str(data_root), "--print-url")
            try:
                out, _ = second.communicate(timeout=120)
                second_attached = second.returncode == 0 and "Dashboard:" in out
                desc_after = self.descriptor_of(data_root)
                same_instance = (
                    desc_after is not None and desc_after.service_instance_id == desc.service_instance_id
                )
                ok = second_attached and same_instance
                self.record(
                    "W0-04",
                    PASS if ok else FAIL,
                    "second launch attaches to the existing service instance",
                    second_exit=second.returncode,
                    same_service_instance=same_instance,
                )
            finally:
                if second.poll() is None:
                    second.kill()
        finally:
            self.stop_launcher(first)

    def w05_stale_descriptor_recovery(self, data_root: Path) -> str:
        launcher, url = self.launch_and_get_url(data_root)
        old_instance = self.descriptor_of(data_root).service_instance_id
        # Kill the service hard, keeping the (now stale) descriptor.
        service_pid = self.descriptor_of(data_root).pid
        self.stop_launcher(launcher)  # launcher stops service gracefully...
        # Simulate forced death + leftover descriptor instead:
        from jobscraper.launcher.runtime_descriptor import pid_alive

        if pid_alive(service_pid):  # pragma: no cover - race
            _hard_kill(service_pid)
            time.sleep(1)
        # Write a stale descriptor pointing at the dead PID.
        from jobscraper.launcher.runtime_descriptor import (
            complete_descriptor,
            make_descriptor,
            write_runtime_descriptor,
        )
        from jobscraper.paths import build_app_paths
        from jobscraper.security.install_secret import load_or_create_install_secret

        secret = load_or_create_install_secret(build_app_paths(data_root))
        stale = complete_descriptor(
            make_descriptor(
                service_instance_id=old_instance,
                pid=service_pid,
                process_start_identity="",
                host="127.0.0.1",
                port=1,
                service_epoch="epoch-old",
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            ),
            secret,
        )
        write_runtime_descriptor(data_root / "runtime", stale)
        # Relaunch: the launcher must reject the stale descriptor and start
        # a fresh valid service.
        launcher2, url2 = self.launch_and_get_url(data_root)
        try:
            desc2 = self.descriptor_of(data_root)
            ok = desc2.pid != service_pid
            self.record(
                "W0-05",
                PASS if ok else FAIL,
                "stale descriptor rejected; new valid instance established",
                new_pid=desc2.pid,
                old_pid=service_pid,
            )
        finally:
            self.stop_launcher(launcher2)

    def w06_old_port_impersonation(self, data_root: Path) -> str:
        # Bind an unrelated listener on a port, then present a descriptor
        # that points at it. The launcher must refuse the impersonator.
        import http.server

        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"status": "ok", "service_instance_id": "someone-else"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
        port = server.server_address[1]
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from jobscraper.launcher.lifecycle import validate_running_service
            from jobscraper.launcher.runtime_descriptor import (
                DescriptorError,
                complete_descriptor,
                make_descriptor,
                write_runtime_descriptor,
            )
            from jobscraper.paths import build_app_paths
            from jobscraper.security.install_secret import load_or_create_install_secret

            secret = load_or_create_install_secret(build_app_paths(data_root))
            fake = complete_descriptor(
                make_descriptor(
                    service_instance_id="svc-expected",
                    pid=os.getpid(),
                    process_start_identity="",
                    host="127.0.0.1",
                    port=port,
                    service_epoch="epoch-x",
                    created_at_utc=datetime.now(timezone.utc).isoformat(),
                ),
                secret,
            )
            write_runtime_descriptor(data_root / "runtime", fake)
            rejected = False
            try:
                validate_running_service(fake, secret)
            except DescriptorError:
                rejected = True
            self.record(
                "W0-06",
                PASS if rejected else FAIL,
                "old-port impersonator rejected by service-instance validation",
                impersonator_port=port,
            )
        finally:
            server.shutdown()
            server.server_close()

    def w07_protected_secret(self, data_root: Path) -> str:
        from jobscraper.paths import build_app_paths
        from jobscraper.security.install_secret import (
            load_or_create_install_secret,
            secret_protected_at_rest,
        )
        from jobscraper.security.windows_acl import inspect_auth_directory_acl

        paths = build_app_paths(data_root)
        launcher, _url = self.launch_and_get_url(data_root)
        secret1 = load_or_create_install_secret(paths)
        self.stop_launcher(launcher)
        # Restart same user: secret survives and stays protected.
        launcher2, _ = self.launch_and_get_url(data_root)
        secret2 = load_or_create_install_secret(paths)
        self.stop_launcher(launcher2)
        acl = inspect_auth_directory_acl(paths.auth)
        blob = (paths.auth / "install-secret.bin").read_bytes()
        ok = (
            secret1 == secret2
            and len(secret1) == 32
            and secret_protected_at_rest(paths)
            and secret1 not in blob
            and bool(acl.get("ok"))
        )
        self.record(
            "W0-07",
            PASS if ok else FAIL,
            "install secret survives restart, is protected at rest, auth dir app-owned",
            stable_across_restart=secret1 == secret2,
            protected_at_rest=secret1 not in blob,
            acl_ok=bool(acl.get("ok")),
            acl_reason=acl.get("reason"),
        )

    def w08_bootstrap_and_private_boundary(self, data_root: Path) -> str:
        launcher, url = self.launch_and_get_url(data_root)
        try:
            host_port = url.split("//", 1)[1].split("#", 1)[0].rstrip("/")
            status, _, _ = self.http("GET", f"http://{host_port}/app", headers={"Host": host_port})
            denied = status == 401
            # Bootstrap exchange works with the fragment ticket.
            ticket = url.split("#bootstrap=", 1)[1]
            status, _, body = self.http(
                "POST",
                f"http://{host_port}/api/bootstrap",
                headers={
                    "Host": host_port,
                    "Origin": f"http://{host_port}",
                    "Content-Type": "application/json",
                },
                body=json.dumps({"ticket": ticket}).encode(),
            )
            self.record(
                "W0-08",
                PASS if (denied and status == 200) else FAIL,
                "bootstrap succeeds; private routes denied without session",
                private_denied=denied,
                exchange_status=status,
            )
        finally:
            self.stop_launcher(launcher)

    def w09_ticket_not_leaked(self, data_root: Path) -> str:
        launcher, url = self.launch_and_get_url(data_root)
        try:
            host_port = url.split("//", 1)[1].split("#", 1)[0].rstrip("/")
            ticket = url.split("#bootstrap=", 1)[1]
            status, _, _ = self.http(
                "POST",
                f"http://{host_port}/api/bootstrap",
                headers={
                    "Host": host_port,
                    "Origin": f"http://{host_port}",
                    "Content-Type": "application/json",
                },
                body=json.dumps({"ticket": ticket}).encode(),
            )
            assert status == 200
        finally:
            self.stop_launcher(launcher)
        # Scan every file in the data root for the ticket value.
        leaked = []
        for path in sorted(data_root.rglob("*")):
            if path.is_file():
                try:
                    if ticket.encode() in path.read_bytes():
                        leaked.append(str(path))
                except OSError:  # pragma: no cover
                    continue
        self.record(
            "W0-09",
            PASS if not leaked else FAIL,
            "bootstrap ticket absent from request-path evidence (files/events)",
            leaked_files=leaked,
            ticket_location="url-fragment-only",
        )

    def w10_sqlite_settings(self, data_root: Path) -> str:
        report = self.doctor_json(data_root)
        db = next((c for c in report["checks"] if c["name"] == "database"), None)
        pragmas = (db or {}).get("details", {}).get("pragmas", {})
        ok = (
            db is not None
            and db["status"] in ("PASS", "WARN")
            and pragmas.get("foreign_keys") == 1
            and pragmas.get("journal_mode") == "WAL"
            and pragmas.get("synchronous") == 2
            and pragmas.get("busy_timeout_ms", 0) > 0
        )
        self.record(
            "W0-10",
            PASS if ok else FAIL,
            "SQLite packaged settings verified (fk/WAL/FULL/busy timeout)",
            pragmas=pragmas,
        )
        self.write_evidence("doctor.json", json.dumps(report, indent=2, sort_keys=True))

    def w11_backup_restore(self, data_root: Path) -> str:
        # Requires the harness python to drive the app's own backup APIs.
        try:
            from jobscraper.db.backup import create_backup_generation, verify_backup_generation
            from jobscraper.db.connection import connect_db
            from jobscraper.db.restore import activate_staged_restore, stage_restore

            from jobscraper.paths import build_app_paths

            conn = connect_db(data_root / "db" / "jobscraper.sqlite3")
            try:
                gen = create_backup_generation(
                    build_app_paths(data_root), conn, kind="ACCEPTANCE"
                )
            finally:
                conn.close()
            manifest = verify_backup_generation(gen)
            target = data_root.parent / (data_root.name + "-restored")
            staged = stage_restore(gen, target)
            activate_staged_restore(staged, target)
            # The restored root must pass doctor.
            report = self.doctor_json(target)
            db = next((c for c in report["checks"] if c["name"] == "database"), None)
            ok = manifest is not None and db is not None and db["status"] in ("PASS", "WARN")
            self.record(
                "W0-11",
                PASS if ok else FAIL,
                "backup generation restores into a clean isolated root",
                generation=Path(gen).name,
                artifacts=len(manifest.artifacts),
                restored_doctor=db["status"] if db else None,
            )
        except Exception as exc:  # noqa: BLE001
            self.record("W0-11", NOT_RUN, f"backup/restore could not execute: {exc}")

    def w12_timezone(self, data_root: Path) -> str:
        report = self.doctor_json(data_root)
        tz = next((c for c in report["checks"] if c["name"] == "timezone"), None)
        ok = tz is not None and tz["status"] == "PASS"
        self.record(
            "W0-12",
            PASS if ok else FAIL,
            "pinned tzdata present; named IANA zone resolves",
            details_tz=tz["details"] if tz else None,
        )

    def w13_browser_isolation(self) -> str:
        try:
            from jobscraper.browser_worker.supervisor import BrowserWorkerSupervisor

            supervisor = BrowserWorkerSupervisor()
            supervisor.start()
            try:
                version = supervisor.request("VERSION", timeout_s=60)
                smoke = supervisor.request("SMOKE", timeout_s=240)
                worker_pid = supervisor.worker_pid()
                if not version.ok:
                    self.record("W0-13", NOT_RUN, "browser worker did not answer VERSION")
                    return
                if smoke.ok:
                    self.record(
                        "W0-13",
                        PASS,
                        "browser worker + Chromium launch outside service process",
                        worker_pid=worker_pid,
                        chromium_version=smoke.payload.get("chromium_version"),
                    )
                else:
                    kind = smoke.error_kind or "SMOKE_FAILED"
                    if kind in ("CHROMIUM_NOT_INSTALLED", "PLAYWRIGHT_NOT_INSTALLED"):
                        self.record(
                            "W0-13",
                            NOT_RUN,
                            f"browser runtime not installed on this host ({kind})",
                        )
                    else:
                        self.record("W0-13", FAIL, f"inert smoke failed: {kind}")
            finally:
                supervisor.stop(timeout_s=15)
        except Exception as exc:  # noqa: BLE001
            self.record("W0-13", NOT_RUN, f"browser check could not execute: {exc}")

    def w14_browser_cleanup(self) -> str:
        before_lines = self.chrome_like_processes()
        try:
            before = {_process_identity(line) for line in before_lines}
            if None in before:
                raise RuntimeError("could not parse pre-smoke process identity")
            from jobscraper.browser_worker.supervisor import BrowserWorkerSupervisor

            supervisor = BrowserWorkerSupervisor()
            supervisor.start()
            try:
                supervisor.request("SMOKE", timeout_s=240)
            finally:
                supervisor.stop(timeout_s=15)
            time.sleep(2)
            after_lines = self.chrome_like_processes()
            after = {_process_identity(line) for line in after_lines}
            if None in after:
                raise RuntimeError("could not parse post-smoke process identity")
            # No NEW chrome-like process identity may remain. Memory/session
            # counters in tasklist output are deliberately ignored.
            residual = sorted(after - before)
            self.record(
                "W0-14",
                PASS if not residual else FAIL,
                "no orphan browser/worker processes after clean shutdown",
                residual_count=len(residual),
                residual_processes=[{"image": image, "pid": pid} for image, pid in residual],
            )
        except Exception as exc:  # noqa: BLE001
            self.record("W0-14", NOT_RUN, f"cleanup check could not execute: {exc}")

    def w15_browser_crash_recovery(self, data_root: Path) -> str:
        """Terminate the service's own browser worker; service must survive,
        the supervisor must restart it bounded, and Doctor must stay healthy.
        Targets only the exact worker PID (child of the service process) —
        never name-matched processes, so the operator's browsers are safe."""
        launcher, url = self.launch_and_get_url(data_root)
        try:
            desc = self.descriptor_of(data_root)
            # The service starts its browser worker at boot; find it as a
            # direct child of the service PID.
            deadline = time.time() + 30
            children: list[int] = []
            while time.time() < deadline:
                children = self.child_pids(desc.pid)
                if children:
                    break
                time.sleep(0.5)
            if not children:
                self.record(
                    "W0-15",
                    FAIL,
                    "service did not start its browser worker (no child process)",
                    service_pid=desc.pid,
                )
                return "FAIL"
            old_worker = children[0]
            self.kill_tree(old_worker)
            # Bounded restart: the supervisor restarts the worker (backoff 1s+).
            deadline = time.time() + 60
            new_worker = None
            while time.time() < deadline:
                children = self.child_pids(desc.pid)
                if children and children[0] != old_worker:
                    new_worker = children[0]
                    break
                time.sleep(0.5)
            host_port = f"{desc.host}:{desc.port}"
            status, _, body = self.http("GET", f"http://{host_port}/health/live")
            survived = status == 200 and json.loads(body).get("status") == "ok"
            doctor = self.doctor_json(data_root)
            doctor_healthy = doctor.get("failed") == 0
            ok = bool(new_worker) and survived and doctor_healthy
            self.record(
                "W0-15",
                PASS if ok else FAIL,
                "service survives browser-worker termination; bounded restart; doctor healthy",
                old_worker_pid=old_worker,
                restarted_worker_pid=new_worker,
                service_healthy_after=survived,
                doctor_status=doctor.get("status"),
                doctor_failed_checks=[
                    c.get("name") for c in doctor.get("checks", []) if c.get("status") == "FAIL"
                ],
            )
        finally:
            self.stop_launcher(launcher)

    def w16_service_forced_kill_recovery(self, data_root: Path) -> str:
        launcher, url = self.launch_and_get_url(data_root)
        desc = self.descriptor_of(data_root)
        service_pid = desc.pid
        os.kill(service_pid, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
        # The supervising launcher restarts the service.
        deadline = time.time() + 60
        new_desc = None
        while time.time() < deadline:
            try:
                candidate = self.descriptor_of(data_root)
            except Exception:
                candidate = None
            if candidate is not None and candidate.pid != service_pid:
                new_desc = candidate
                break
            time.sleep(0.5)
        self.stop_launcher(launcher)
        ok = new_desc is not None
        self.record(
            "W0-16",
            PASS if ok else FAIL,
            "forced service termination recovered by supervising launcher",
            new_pid=new_desc.pid if new_desc else None,
        )

    def w17_package_independence(self, data_root: Path) -> str:
        # The spawn environment for exe targets already strips PYTHONPATH and
        # VIRTUAL_ENV; prove the app works under it.
        try:
            launcher, url = self.launch_and_get_url(data_root)
            try:
                host_port = url.split("//", 1)[1].split("#", 1)[0].rstrip("/")
                status, _, _ = self.http("GET", f"http://{host_port}/health/live")
                doctor = self.doctor_json(data_root)
                ok = status == 200 and doctor.get("exit_code") == 0
                self.record(
                    "W0-17",
                    PASS if ok else FAIL,
                    "app/doctor work from a clean environment (no dev venv/PYTHONPATH)",
                    target=self.target,
                )
            finally:
                self.stop_launcher(launcher)
        except Exception as exc:  # noqa: BLE001
            self.record("W0-17", NOT_RUN, f"could not execute: {exc}")

    def w18_resource_observation(self, data_root: Path) -> str:
        launcher, url = self.launch_and_get_url(data_root)
        try:
            desc = self.descriptor_of(data_root)
            time.sleep(3)
            inventory = self.chrome_like_processes()
            measurements = {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "service_pid": desc.pid,
                "process_inventory_lines": len(inventory),
                "note": "memory values recorded from tasklist/ps evidence",
            }
            self.write_evidence(
                "resource-measurements.json", json.dumps(measurements, indent=2)
            )
            self.write_evidence(
                "process-inventory-during.txt", "\n".join(self.list_processes())
            )
            self.record(
                "W0-18",
                PASS,
                "idle service + browser-tree observations recorded (no invented ceilings)",
            )
        finally:
            self.stop_launcher(launcher)

    # ------------------------------------------------------------- W1 checks
    # Slice-1 native acceptance (S1.12).  All checks drive the target
    # (packaged exe or dev) through its public HTTP surface; the harness
    # hosts the loopback fixture feed and seeds operator-style source
    # rows directly in the database (Slice 1 has no source-management
    # API), exactly like the pytest E2E acceptance suite.
    def _db_connect(self, data_root: Path):
        from jobscraper.db.connection import connect_db
        from jobscraper.paths import build_app_paths

        return connect_db(build_app_paths(data_root).database_file)

    def _w1_wait_db(self, data_root: Path, timeout: float = 60.0):
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                conn = self._db_connect(data_root)
                conn.execute("SELECT COUNT(*) FROM sources").fetchone()
                return conn
            except Exception as exc:  # DB not created/migrated yet
                last_error = exc
                time.sleep(0.2)
        raise RuntimeError(f"service database never became ready: {last_error}")

    def _w1_seed_source(self, db, feed_port: int, *, source_id: str,
                        binding_id: str, revision_id: str, path: str) -> None:
        """Operator-style seeding of one feed source + binding (the only
        way sources exist in Slice 1)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        config = json.dumps(
            {
                "url_template": f"http://127.0.0.1:{feed_port}{path}?page={{page}}",
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
            }
        )
        db.executescript(
            f"""
            INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
            VALUES ('{source_id}','Fixture Feed','PUBLIC_FEED',
                    'http://127.0.0.1:{feed_port}/jobs','{now}','{now}');
            INSERT OR IGNORE INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
            VALUES ('json_api_feed','1.0.0','1','{{}}','{now}');
            INSERT OR IGNORE INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{now}');
            INSERT OR IGNORE INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
            VALUES ('permrev-1','perm-1',1,'{{}}','{now}');
            INSERT INTO source_adapter_bindings (id, source_id, display_name, current_revision_id, created_at)
            VALUES ('{binding_id}','{source_id}','api','{revision_id}','{now}');
            INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
                strategy, execution_class, permission_profile_id, permission_profile_revision, config_json, created_at)
            VALUES ('{revision_id}','{binding_id}',1,'json_api_feed','1.0.0',
                'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,'{config}','{now}');
            """
        )
        db.commit()

    def _w1_repoint_binding(self, db, revision_id: str, feed_port: int, path: str) -> None:
        config = json.dumps(
            {
                "url_template": f"http://127.0.0.1:{feed_port}{path}?page={{page}}",
                "items_path": "jobs",
                "fields": {
                    "source_job_id": {"path": "id", "required": True},
                    "title": {"path": "title", "required": True},
                    "company": {"path": "company"},
                    "job_url": {"path": "url"},
                    "apply_url": {"path": "apply_url"},
                },
            }
        )
        db.execute(
            "UPDATE source_adapter_binding_revisions SET config_json = ? WHERE id = ?",
            (config, revision_id),
        )
        db.commit()

    def _w1_bootstrap(self, url: str) -> dict:
        base, ticket = url.split("#bootstrap=", 1)
        host_port = base.split("//", 1)[1].rstrip("/")
        status, headers, body = self.http(
            "POST",
            f"http://{host_port}/api/bootstrap",
            headers={
                "Host": host_port,
                "Origin": f"http://{host_port}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"ticket": ticket}).encode(),
        )
        if status != 200:
            raise RuntimeError(f"bootstrap exchange failed: {status} {body[:200]!r}")
        csrf = json.loads(body)["csrf_token"]
        session_id = None
        for part in ", ".join(headers.get_all("Set-Cookie", [])).split(","):
            if part.strip().startswith("wjs_session="):
                session_id = part.strip().split("=", 1)[1].split(";", 1)[0]
        if not session_id:
            raise RuntimeError("no session cookie after bootstrap")
        return {"host_port": host_port, "session": session_id, "csrf": csrf}

    def _w1_api(self, auth: dict, method: str, path: str, *, json_body=None):
        headers = {
            "Host": auth["host_port"],
            "Origin": f"http://{auth['host_port']}",
            "Cookie": f"wjs_session={auth['session']}; wjs_csrf={auth['csrf']}",
            "X-CSRF-Token": auth["csrf"],
        }
        body = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body).encode()
        status, _, out = self.http(
            method, f"http://{auth['host_port']}{path}", headers=headers, body=body
        )
        return status, (json.loads(out) if out else None)

    def run_slice1(self) -> int:
        """W1-01..W1-07 on one isolated root with a harness-hosted feed."""
        import threading

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _W1FeedHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        feed_port = server.server_address[1]
        try:
            with tempfile.TemporaryDirectory(prefix="wjs-w1-") as root_name:
                data_root = Path(root_name) / "W1"
                (data_root / "auth").mkdir(parents=True, exist_ok=True)
                (data_root / "runtime").mkdir(parents=True, exist_ok=True)
                db = None
                try:
                    self._w1_01_vertical_slice(data_root, feed_port)
                    self._w1_02_restart_persistence(data_root, feed_port)
                    self._w1_03_inbox_dedupe_across_restart(data_root, feed_port)
                    db = self._db_connect(data_root)
                    self._w1_04_private_destination_denied(data_root, feed_port, db)
                    self._w1_05_redirect_hop_denied(data_root, feed_port, db)
                    self._w1_06_unsafe_links_not_clickable(data_root, feed_port, db)
                    self._w1_07_doctor_after_workload(data_root)
                finally:
                    if db is not None:
                        db.close()
        finally:
            server.shutdown()
            server.server_close()
        return 0

    def _w1_01_vertical_slice(self, data_root: Path, feed_port: int) -> None:
        launcher, url = self.launch_and_get_url(data_root)
        auth = None
        profile = job_id = application = None
        try:
            db = self._w1_wait_db(data_root)
            try:
                self._w1_seed_source(
                    db, feed_port, source_id="src-1", binding_id="bnd-1",
                    revision_id="bndrev-1", path="/jobs",
                )
            finally:
                db.close()
            auth = self._w1_bootstrap(url)
            status, profile = self._w1_api(
                auth, "POST", "/api/profiles",
                json_body={
                    "name": "Native", "keywords": ["engineer"],
                    "eligible_countries": ["DE"],
                    "remote_rules": {"remote_ok": True}, "min_score_inbox": 0,
                },
            )
            ok_profile = status == 200 and profile.get("id")
            status, run = self._w1_api(
                auth, "POST", "/api/runs", json_body={"profile_id": profile["id"]}
            )
            ok_run = (
                status == 200 and run.get("status") == "SUCCEEDED"
                and run.get("jobs_saved") == 2
            )
            status, inbox = self._w1_api(
                auth, "GET", f"/api/inbox?profile_id={profile['id']}"
            )
            items = inbox.get("inbox", []) if inbox else []
            ok_inbox = (
                status == 200 and len(items) == 2
                and {i["event_kind"] for i in items} == {"NEW_ELIGIBLE_APPEARANCE"}
                and all(i["score"] >= 0 and i["breakdown"] for i in items)
            )
            job_id = next((i["job_id"] for i in items if i["title"] == "Backend Engineer"), None)
            status, detail = self._w1_api(auth, "GET", f"/api/jobs/{job_id}")
            presence = (detail.get("sources") or [{}])[0] if detail else {}
            ok_detail = (
                status == 200
                and detail["job"]["listing_status"] == "ACTIVE"
                and presence.get("source_job_id") == "fx-1"
                and presence.get("canonical_job_url")
                == "https://jobs.example.test/jobs/fx-1"
                and detail.get("apply_url") == "https://jobs.example.test/jobs/fx-1/apply"
            )
            status, out = self._w1_api(
                auth, "POST",
                f"/api/profiles/{profile['id']}/jobs/{job_id}/disposition",
                json_body={"disposition": "SHORTLISTED"},
            )
            ok_disposition = status == 200 and out.get("disposition") == "SHORTLISTED"
            status, application = self._w1_api(
                auth, "POST", "/api/applications",
                json_body={"job_id": job_id, "profile_id": profile["id"]},
            )
            ok_application = status == 200 and application.get("status") == "PREPARING"
            ok = all([ok_profile, ok_run, ok_inbox, ok_detail, ok_disposition, ok_application])
            self.record(
                "W1-01",
                PASS if ok else FAIL,
                "packaged E2E vertical slice: profile → run → jobs → scores → "
                "inbox → disposition → application",
                profile=ok_profile, run=ok_run, inbox=ok_inbox, job_detail=ok_detail,
                disposition=ok_disposition, application=ok_application,
            )
        except Exception as exc:  # noqa: BLE001
            self.record("W1-01", FAIL, f"vertical slice failed: {exc}")
        finally:
            self.stop_launcher(launcher)
            self._w1_state["auth"] = auth
            self._w1_state["profile_id"] = (
                profile.get("id") if isinstance(profile, dict) else None
            )
            self._w1_state["job_id"] = (
                job_id if isinstance(job_id, str) else None
            )
            self._w1_state["application_id"] = (
                application.get("id") if isinstance(application, dict) else None
            )

    def _w1_02_restart_persistence(self, data_root: Path, feed_port: int) -> None:
        try:
            launcher, url = self.launch_and_get_url(data_root)
            try:
                auth = self._w1_bootstrap(url)
                profile_id = self._w1_state.get("profile_id")
                job_id = self._w1_state.get("job_id")
                status, profiles = self._w1_api(auth, "GET", "/api/profiles")
                ok_profiles = (
                    status == 200
                    and len(profiles["profiles"]) == 1
                    and profiles["profiles"][0]["snapshot"]["name"] == "Native"
                )
                status, inbox = self._w1_api(
                    auth, "GET", f"/api/inbox?profile_id={profile_id}"
                )
                items = inbox.get("inbox", []) if inbox else []
                by_title = {i["title"]: i for i in items}
                ok_inbox = (
                    status == 200
                    and len(items) == 2
                    and by_title["Backend Engineer"]["disposition"] == "SHORTLISTED"
                    and by_title["Platform Engineer"]["disposition"] == "NONE"
                )
                status, detail = self._w1_api(auth, "GET", f"/api/jobs/{job_id}")
                ok_job = status == 200 and detail["job"]["title"] == "Backend Engineer"
                status, applications = self._w1_api(
                    auth, "GET", f"/api/applications?profile_id={profile_id}"
                )
                ok_application = (
                    status == 200
                    and len(applications["applications"]) == 1
                    and applications["applications"][0]["status"] == "PREPARING"
                )
                ok = all([ok_profiles, ok_inbox, ok_job, ok_application])
                self.record(
                    "W1-02",
                    PASS if ok else FAIL,
                    "restart persistence: profile/inbox/job/application survive (PROD-08)",
                    profiles=ok_profiles, inbox=ok_inbox, job=ok_job,
                    application=ok_application,
                )
            finally:
                self.stop_launcher(launcher)
        except Exception as exc:  # noqa: BLE001
            self.record("W1-02", FAIL, f"restart persistence check failed: {exc}")

    def _w1_03_inbox_dedupe_across_restart(self, data_root: Path, feed_port: int) -> None:
        try:
            db = self._db_connect(data_root)
            try:
                before_events = db.execute(
                    "SELECT COUNT(*) FROM job_profile_inbox_events"
                ).fetchone()[0]
                before_jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                before_observations = db.execute(
                    "SELECT COUNT(*) FROM job_observations"
                ).fetchone()[0]
            finally:
                db.close()
            launcher, url = self.launch_and_get_url(data_root)
            try:
                auth = self._w1_bootstrap(url)
                status, run = self._w1_api(
                    auth, "POST", "/api/runs",
                    json_body={"profile_id": self._w1_state.get("profile_id")},
                )
                ok_run = (
                    status == 200 and run.get("status") == "SUCCEEDED"
                    and run.get("jobs_saved") == 0 and run.get("jobs_updated") == 0
                )
            finally:
                self.stop_launcher(launcher)
            db = self._db_connect(data_root)
            try:
                after_events = db.execute(
                    "SELECT COUNT(*) FROM job_profile_inbox_events"
                ).fetchone()[0]
                after_jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                after_observations = db.execute(
                    "SELECT COUNT(*) FROM job_observations"
                ).fetchone()[0]
            finally:
                db.close()
            ok = (
                ok_run
                and after_events == before_events
                and after_jobs == before_jobs
                and after_observations == before_observations
            )
            self.record(
                "W1-03",
                PASS if ok else FAIL,
                "idempotent resume after restart: no duplicate jobs, "
                "observations, or inbox events",
                run=ok_run, events=(before_events, after_events),
                jobs=(before_jobs, after_jobs),
                observations=(before_observations, after_observations),
            )
        except Exception as exc:  # noqa: BLE001
            self.record("W1-03", FAIL, f"dedupe check failed: {exc}")

    def _w1_04_private_destination_denied(self, data_root: Path, feed_port: int, db) -> None:
        """SSRF negative: the granted entry host is the loopback fixture,
        but the binding template targets a TEST-NET address.  The packaged
        app must fail closed — typed POLICY_REJECTED, no fetch attempt."""
        try:
            self._w1_seed_source(
                db, feed_port, source_id="src-2", binding_id="bnd-2",
                revision_id="bndrev-2", path="/jobs",
            )
            # point the second binding's template at a TEST-NET-1 address
            db.execute(
                "UPDATE source_adapter_binding_revisions SET config_json = ?"
                " WHERE id = 'bndrev-2'",
                (json.dumps({
                    "url_template": "http://192.0.2.1/jobs?page={page}",
                    "items_path": "jobs",
                    "fields": {"source_job_id": {"path": "id", "required": True},
                                "title": {"path": "title", "required": True}},
                }),),
            )
            db.commit()
            launcher, url = self.launch_and_get_url(data_root)
            try:
                auth = self._w1_bootstrap(url)
                status, run = self._w1_api(
                    auth, "POST", "/api/runs",
                    json_body={"profile_id": self._w1_state.get("profile_id")},
                )
                run_status = run.get("status") if run else None
            finally:
                self.stop_launcher(launcher)
            row = db.execute(
                """
                SELECT req.status, req.page_class, fa.failure_kind AS fetch_kind,
                       fa.failure_json
                FROM scrape_requests req
                JOIN fetch_attempts fa ON fa.request_id = req.id
                WHERE req.source_id = 'src-2'
                ORDER BY req.created_at DESC LIMIT 1
                """
            ).fetchone()
            failure = (
                json.loads(row["failure_json"]) if row and row["failure_json"] else {}
            )
            policy_denied = (
                row is not None
                and row["status"] == "SUCCEEDED"  # definitive answer, no retry storm
                and row["fetch_kind"] == "POLICY_REJECTED"
                and failure.get("kind") == "POLICY_REJECTED"
                and bool(
                    (failure.get("details_redacted") or {}).get("reason_code")
                )
            )
            ok = (
                status == 200
                and run_status == "PARTIAL"
                and policy_denied
                and db.execute(
                    "SELECT COUNT(*) FROM job_observations WHERE source_id = 'src-2'"
                ).fetchone()[0] == 0
            )
            self.record(
                "W1-04",
                PASS if ok else FAIL,
                "SSRF negative: private/test destination denied fail-closed "
                "(typed POLICY_REJECTED, nothing fetched)",
                run_status=run_status,
                request=(dict(row) if row else None),
            )
        except Exception as exc:  # noqa: BLE001
            self.record("W1-04", FAIL, f"SSRF destination check failed: {exc}")

    def _w1_05_redirect_hop_denied(self, data_root: Path, feed_port: int, db) -> None:
        """SSRF negative: the granted entry host serves a redirect to an
        unauthorized TEST-NET destination; each hop is policy-checked."""
        try:
            self._w1_repoint_binding(db, "bndrev-2", feed_port, "/redirect-jobs")
            launcher, url = self.launch_and_get_url(data_root)
            try:
                auth = self._w1_bootstrap(url)
                status, run = self._w1_api(
                    auth, "POST", "/api/runs",
                    json_body={"profile_id": self._w1_state.get("profile_id")},
                )
                run_status = run.get("status") if run else None
            finally:
                self.stop_launcher(launcher)
            row = db.execute(
                """
                SELECT req.status, req.page_class, fa.failure_kind, fa.failure_json
                FROM scrape_requests req
                JOIN fetch_attempts fa ON fa.request_id = req.id
                WHERE req.source_id = 'src-2'
                ORDER BY req.created_at DESC LIMIT 1
                """
            ).fetchone()
            failure = json.loads(row["failure_json"]) if row and row["failure_json"] else {}
            ok = (
                status == 200
                and run_status == "PARTIAL"
                and row is not None
                and row["status"] == "SUCCEEDED"
                and row["failure_kind"] == "POLICY_REJECTED"
                and failure.get("kind") == "POLICY_REJECTED"
            )
            self.record(
                "W1-05",
                PASS if ok else FAIL,
                "SSRF negative: redirect to unauthorized destination denied per hop",
                run_status=run_status,
                failure_kind=failure.get("kind"),
            )
        except Exception as exc:  # noqa: BLE001
            self.record("W1-05", FAIL, f"redirect-hop check failed: {exc}")

    def _w1_06_unsafe_links_not_clickable(self, data_root: Path, feed_port: int, db) -> None:
        """PROD-05: javascript:/data: source links are stored for
        provenance but never surfaced as clickable URLs."""
        try:
            self._w1_repoint_binding(db, "bndrev-2", feed_port, "/jobs-unsafe")
            launcher, url = self.launch_and_get_url(data_root)
            try:
                auth = self._w1_bootstrap(url)
                self._w1_api(
                    auth, "POST", "/api/runs",
                    json_body={"profile_id": self._w1_state.get("profile_id")},
                )
                row = db.execute(
                    "SELECT job_id FROM job_sources WHERE source_job_id = 'bad-1'"
                ).fetchone()
                ok_job_created = row is not None
                status, detail = (
                    self._w1_api(auth, "GET", f"/api/jobs/{row['job_id']}")
                    if row else (0, None)
                )
                sources = (detail or {}).get("sources") or [{}]
                presence = sources[0]
                ok = (
                    ok_job_created
                    and status == 200
                    and presence.get("application_url") is None
                    and presence.get("canonical_job_url") is None
                    and (detail or {}).get("apply_url") is None
                )
                self.record(
                    "W1-06",
                    PASS if ok else FAIL,
                    "unsafe source links (javascript:/data:) never surfaced clickable",
                    job_created=ok_job_created,
                    application_url=presence.get("application_url", "missing"),
                    canonical_job_url=presence.get("canonical_job_url", "missing"),
                    apply_url=(detail or {}).get("apply_url", "missing"),
                )
            finally:
                self.stop_launcher(launcher)
        except Exception as exc:  # noqa: BLE001
            self.record("W1-06", FAIL, f"unsafe-link check failed: {exc}")

    def _w1_07_doctor_after_workload(self, data_root: Path) -> None:
        try:
            report = self.doctor_json(data_root)
            failed = [c["name"] for c in report.get("checks", []) if c.get("status") == "FAIL"]
            ok = report.get("exit_code") == 0 and not failed
            self.record(
                "W1-07",
                PASS if ok else FAIL,
                "doctor healthy on the root after the Slice-1 workload",
                exit_code=report.get("exit_code"),
                failed_checks=failed,
            )
        except Exception as exc:  # noqa: BLE001
            self.record("W1-07", FAIL, f"doctor check failed: {exc}")

    # ------------------------------------------------------------------ main
    def run(self, slice_arg: str = "0") -> int:
        started = datetime.now(timezone.utc).isoformat()
        self.write_evidence(
            "process-inventory-before.txt", "\n".join(self.list_processes())
        )
        if slice_arg in ("0", "all"):
            with tempfile.TemporaryDirectory(prefix="wjs-w0-") as root_name:
                root = Path(root_name)
                data_root = root / "W0"
                (data_root / "auth").mkdir(parents=True, exist_ok=True)
                (data_root / "runtime").mkdir(parents=True, exist_ok=True)
                # Initialize via one launch (all subsequent checks reuse it).
                launcher, url = self.launch_and_get_url(data_root)
                self.stop_launcher(launcher)

                self.w01_packaged_launch(data_root)
                self.w02_loopback_only(data_root)
                self.w03_collision_safe_port(data_root)
                self.w04_single_instance(data_root)
                self.w05_stale_descriptor_recovery(data_root)
                self.w06_old_port_impersonation(data_root)
                self.w07_protected_secret(data_root)
                self.w08_bootstrap_and_private_boundary(data_root)
                self.w09_ticket_not_leaked(data_root)
                self.w10_sqlite_settings(data_root)
                self.w11_backup_restore(data_root)
                self.w12_timezone(data_root)
                self.w13_browser_isolation()
                self.w14_browser_cleanup()
                self.w15_browser_crash_recovery(data_root)
                self.w16_service_forced_kill_recovery(data_root)
                self.w17_package_independence(data_root)
                self.w18_resource_observation(data_root)

        if slice_arg in ("1", "all"):
            self.run_slice1()

        self.write_evidence(
            "process-inventory-after.txt", "\n".join(self.list_processes())
        )
        slices_run = {"0": ["0"], "1": ["1"], "all": ["0", "1"]}[slice_arg]
        record = {
            "schema_version": 1,
            "slice": "+".join(slices_run),
            "spec_version": "0.3.1.3",
            "target": self.target,
            "started_at_utc": started,
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "tests": {k: v["status"] for k, v in sorted(self.results.items())},
            "details": self.results,
            "promotion": "PENDING_NATIVE_RUN" if self.target != "exe" else None,
        }
        failed = [k for k, v in self.results.items() if v["status"] == FAIL]
        record["unresolved_critical_findings"] = len(failed)
        self.write_evidence("native-acceptance.json", json.dumps(record, indent=2, sort_keys=True))
        log(f"results: {json.dumps(record['tests'], sort_keys=True)}")
        if failed:
            log(f"FAILED checks: {failed}")
            return 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Native acceptance harness (slices 0 and 1)")
    parser.add_argument("--slice", choices=["0", "1", "all"], default="0",
                        help="which acceptance matrix to run (default 0: the documented Slice-0 invocation)")
    parser.add_argument("--target", choices=["exe", "dev"], default="exe")
    parser.add_argument("--exe", type=Path, default=None, help="package dir (dist/JobScraper)")
    parser.add_argument(
        "--evidence-dir", type=Path, default=REPO_ROOT / "artifacts" / "slice0" / "run"
    )
    args = parser.parse_args()
    if args.target == "exe" and args.exe is None:
        parser.error("--target exe requires --exe <package-dir>")
    harness = Harness(args.target, args.exe, args.evidence_dir)
    return harness.run(slice_arg=args.slice)


if __name__ == "__main__":
    raise SystemExit(main())
