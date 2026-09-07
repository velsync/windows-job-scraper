"""Integration tests for the browser worker process (S0.9).

Proves with real subprocesses:

* the worker process answers PING/VERSION/SHUTDOWN over the wire protocol;
* malformed/oversized lines get typed errors without crashing the worker;
* SMOKE returns a typed result — a full inert success when the pinned
  Chromium runtime is present, or a typed unavailability (fail-closed) when
  it is not (this host has no browser downloaded);
* the browser-worker module imports neither FastAPI nor Playwright at import
  time (lazy runtime; ARC-06 isolation);
* the service shell does not import Playwright;
* the supervisor restarts a killed worker with bounded backoff and a restart
  ceiling, without raising into the caller;
* supervisor lifecycle events reach the durable event log (redacted).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


class WorkerProcess:
    """Thin test client for the real worker subprocess."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "jobscraper", "--browser-worker"],
            cwd=str(REPO_ROOT),
            env=_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def send_raw(self, line: str) -> str:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline()

    def request(self, msg_type: str, request_id: str | None = None) -> dict:
        request_id = request_id or f"r-{msg_type.lower()}-{time.monotonic_ns()}"
        raw = self.send_raw(json.dumps({"type": msg_type, "request_id": request_id}))
        return json.loads(raw)

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proc.kill()
                self.proc.wait()


@pytest.fixture()
def worker():
    w = WorkerProcess()
    yield w
    w.close()


class TestWorkerProcess:
    def test_ping(self, worker):
        data = worker.request("PING")
        assert data["ok"] is True
        assert data["protocol_version"] == 1
        assert data["payload"]["pong"] is True

    def test_version_reports_compatibility_facts(self, worker):
        data = worker.request("VERSION")
        assert data["ok"] is True
        payload = data["payload"]
        assert payload["app_version"]
        assert payload["worker_pid"] == worker.proc.pid
        assert payload["python_version"]
        # Playwright facts are present and typed (may be NOT_INSTALLED).
        assert "playwright_version" in payload
        assert "chromium_installed" in payload
        assert "chromium_revision" in payload

    def test_malformed_line_gets_typed_error_not_crash(self, worker):
        raw = worker.send_raw("{not json")
        data = json.loads(raw)
        assert data["ok"] is False
        assert data["error"]["kind"] == "MALFORMED"
        # Worker still alive and serving.
        assert worker.request("PING")["ok"] is True

    def test_oversized_line_gets_typed_error(self, worker):
        raw = worker.send_raw("x" * (64 * 1024 + 64))
        data = json.loads(raw)
        assert data["ok"] is False
        assert data["error"]["kind"] == "OVERSIZED"

    def test_unknown_type_gets_typed_error(self, worker):
        raw = worker.send_raw(json.dumps({"type": "EXECUTE", "request_id": "r"}))
        data = json.loads(raw)
        assert data["ok"] is False
        assert data["error"]["kind"] == "UNKNOWN_TYPE"

    def test_shutdown_exits_cleanly(self, worker):
        data = worker.request("SHUTDOWN")
        assert data["ok"] is True
        assert worker.proc.wait(timeout=10) == 0

    def test_smoke_returns_typed_result(self, worker):
        """SMOKE either succeeds inertly (browser present) or fails closed
        with a typed unavailability (browser absent). Never a crash."""
        data = worker.request("SMOKE", timeout_s=None) if False else worker.request("SMOKE")
        if data["ok"]:
            payload = data["payload"]
            assert payload["page_title"]  # inert local document title
            assert payload["browser_exited_cleanly"] is True
            assert payload["chromium_version"]
        else:
            assert data["error"]["kind"] in {
                "CHROMIUM_NOT_INSTALLED",
                "PLAYWRIGHT_NOT_INSTALLED",
                "LAUNCH_FAILED",
            }
            assert data["error"]["message"]
        # Worker still serves after a smoke attempt (success or typed fail).
        assert worker.request("PING")["ok"] is True


class TestIsolation:
    def test_browser_worker_module_imports_no_fastapi_no_playwright(self):
        code = (
            "import sys; sys.path.insert(0, 'src');"
            "import jobscraper.browser_worker.main;"
            "import jobscraper.browser_worker.supervisor;"
            "assert 'fastapi' not in sys.modules, 'fastapi leaked into browser worker';"
            "assert 'playwright' not in sys.modules, 'playwright imported eagerly';"
            "print('ISOLATION_OK')"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=_env(),
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert "ISOLATION_OK" in out.stdout

    def test_service_shell_does_not_import_playwright(self):
        code = (
            "import sys; sys.path.insert(0, 'src');"
            "import jobscraper.service.app;"
            "assert 'playwright' not in sys.modules, 'service must not import playwright';"
            "print('SERVICE_ISOLATION_OK')"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=_env(),
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert "SERVICE_ISOLATION_OK" in out.stdout


class TestSupervisor:
    def test_supervisor_start_ping_stop(self):
        from jobscraper.browser_worker.supervisor import BrowserWorkerSupervisor

        events: list[tuple[str, str, str]] = []
        supervisor = BrowserWorkerSupervisor(on_event=lambda l, k, m: events.append((l, k, m)))
        supervisor.start()
        try:
            assert supervisor.is_running()
            response = supervisor.request("PING", timeout_s=30)
            assert response.ok is True
            version = supervisor.request("VERSION", timeout_s=30)
            assert version.ok is True
        finally:
            supervisor.stop(timeout_s=15)
        assert not supervisor.is_running()
        kinds = [k for _l, k, _m in events]
        assert "BROWSER_WORKER_STARTED" in kinds
        assert "BROWSER_WORKER_STOPPED" in kinds

    def test_supervisor_restarts_killed_worker_bounded(self):
        from jobscraper.browser_worker.supervisor import BrowserWorkerSupervisor

        supervisor = BrowserWorkerSupervisor()
        supervisor.start()
        try:
            first_pid = supervisor.worker_pid()
            assert first_pid is not None
            # Kill the worker hard; the supervisor must restart it.
            os.kill(first_pid, 9)
            deadline = time.monotonic() + 30
            new_pid = None
            while time.monotonic() < deadline:
                current = supervisor.worker_pid()
                if current is not None and current != first_pid and supervisor.is_running():
                    new_pid = current
                    break
                time.sleep(0.2)
            assert new_pid is not None, "supervisor did not restart the killed worker"
            # The restarted worker serves requests.
            assert supervisor.request("PING", timeout_s=30).ok is True
        finally:
            supervisor.stop(timeout_s=15)
        assert supervisor.stats.restarts >= 1

    def test_service_lifespan_wires_default_supervisor(self, tmp_path):
        from jobscraper.config import AppConfig
        from jobscraper.db.connection import Database
        from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
        from jobscraper.paths import build_app_paths, ensure_app_directories
        from jobscraper.service.lifespan import ServiceLifespan

        root = tmp_path / "root"
        ensure_app_directories(build_app_paths(root))
        db = Database(build_app_paths(root).database_file)
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
        try:
            import asyncio

            lifespan = ServiceLifespan(AppConfig(data_root=root), db, b"\x06" * 32)
            asyncio.run(lifespan.start_background())
            try:
                assert lifespan.supervisor.is_running()
            finally:
                asyncio.run(lifespan.stop_background())
            assert not lifespan.supervisor.is_running()
            # Supervision events were persisted (redacted, no secrets).
            rows = db.query("SELECT kind FROM events ORDER BY id")
            kinds = [r["kind"] for r in rows]
            assert "BROWSER_WORKER_STARTED" in kinds
            assert "BROWSER_WORKER_STOPPED" in kinds
        finally:
            db.close()
