"""Slice 0 automated acceptance (S0.12).

One end-to-end run against the *real* launcher and service subprocesses on an
isolated data root, proving the aggregate acceptance items that unit tests
cannot show together:

    launch → OS-assigned port → signed descriptor → launcher proof
    → one-time ticket in URL fragment → same-origin bootstrap exchange
    → HttpOnly session cookie + CSRF → authenticated private read
    → CSRF-protected mutation (logout) → private read denied afterwards
    → clean shutdown → descriptor removed → no orphan service
    → Doctor healthy on the initialized root
    → secret scan across every file in the data root (secret/ticket/
      session/CSRF values absent from DB bytes, events, logs, descriptor)
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jobscraper.config import AppConfig
from jobscraper.diagnostics.events import list_recent_events
from jobscraper.launcher.runtime_descriptor import (
    DescriptorError,
    load_runtime_descriptor,
)
from jobscraper.paths import build_app_paths, ensure_app_directories
from jobscraper.security.install_secret import load_or_create_install_secret

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("WJS_DATA_ROOT", None)
    return env


@pytest.fixture()
def launched_app(tmp_path):
    """Run the real launcher; yield (config, secret, dashboard_url, launcher)."""
    root = tmp_path / "acceptance-root"
    ensure_app_directories(build_app_paths(root))
    launcher = subprocess.Popen(
        [sys.executable, "-m", "jobscraper", "--data-root", str(root), "--print-url"],
        cwd=str(REPO_ROOT),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    config = AppConfig(data_root=root)
    secret = load_or_create_install_secret(config.paths)
    dashboard_url = None
    try:
        deadline = time.time() + 90
        lines = []
        while time.time() < deadline:
            line = launcher.stdout.readline()
            if not line:
                break
            lines.append(line)
            if line.startswith("Dashboard:"):
                dashboard_url = line.strip().split("Dashboard: ", 1)[1]
                break
        assert dashboard_url, "launcher never produced a dashboard URL: " + "".join(lines)
        yield config, secret, dashboard_url, launcher
    finally:
        if launcher.poll() is None:
            launcher.send_signal(signal.SIGTERM)
            try:
                launcher.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                launcher.kill()
                launcher.wait()


def _request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None):
    """urllib request returning (status, headers, body)."""
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            # Return the raw header object: duplicate Set-Cookie headers must
            # survive (a dict would collapse them).
            return response.status, response.headers, response.read()
        # urllib follows redirects; none are expected in this flow.
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


class TestFullBootstrapChain:
    def test_end_to_end_flow(self, launched_app):
        config, secret, dashboard_url, launcher = launched_app

        # 1. Ticket is in the fragment; nothing sensitive in query/path.
        assert "#bootstrap=" in dashboard_url
        base, fragment = dashboard_url.split("#", 1)
        ticket = fragment.split("bootstrap=", 1)[1]
        assert "ticket=" not in base and ticket not in base

        host_port = base.split("//", 1)[1].rstrip("/")
        host, port_s = host_port.rsplit(":", 1)
        port = int(port_s)
        assert host == "127.0.0.1"
        assert 0 < port < 65536  # OS-assigned, not a fixed default

        # 2. Runtime descriptor is valid on disk while running.
        desc = load_runtime_descriptor(config.paths.runtime)
        assert desc is not None and desc.port == port

        # 3. Liveness is public and minimal.
        status, headers, body = _request("GET", f"http://{host}:{port}/health/live")
        assert status == 200
        health = json.loads(body)
        assert set(health) == {"status", "service_instance_id"}
        assert health["service_instance_id"] == desc.service_instance_id

        # 4. Public bootstrap shell: local assets only.
        status, headers, shell = _request("GET", f"http://{host}:{port}/")
        assert status == 200
        shell_text = shell.decode()
        assert "/static/app.js" in shell_text
        assert not any(m in shell_text for m in ("unpkg.com", "cdn.jsdelivr", "googleapis"))
        assert "service_instance_id" not in shell_text
        assert "X-Content-Type-Options" in {k for k in headers}
        csp = headers.get("Content-Security-Policy", "")
        assert csp.startswith("default-src 'self'")
        assert "Access-Control-Allow-Origin" not in headers

        # 5. Hostile Host/Origin denied.
        status, _, _ = _request(
            "GET", f"http://{host}:{port}/health/live", headers={"Host": "evil.example"}
        )
        assert status == 400
        status, _, _ = _request(
            "POST",
            f"http://{host}:{port}/api/bootstrap",
            headers={"Host": host_port, "Origin": "http://evil.example"},
            body=b"{}",
        )
        assert status == 403

        # 6. Private read denied without a session.
        status, _, _ = _request(
            "GET", f"http://{host}:{port}/app", headers={"Host": host_port}
        )
        assert status == 401

        # 7. Bootstrap exchange: one-time ticket + exact Origin -> session.
        exchange_body = json.dumps({"ticket": ticket}).encode()
        status, headers, body = _request(
            "POST",
            f"http://{host}:{port}/api/bootstrap",
            headers={
                "Host": host_port,
                "Origin": f"http://{host}:{port}",
                "Content-Type": "application/json",
            },
            body=exchange_body,
        )
        assert status == 200, body
        exchanged = json.loads(body)
        csrf_token = exchanged["csrf_token"]
        set_cookies = ", ".join(headers.get_all("Set-Cookie", []))
        session_id = None
        for part in set_cookies.split(","):
            if part.strip().startswith("wjs_session="):
                session_id = part.strip().split("=", 1)[1].split(";", 1)[0]
        assert session_id, set_cookies
        assert "HttpOnly" in set_cookies and "samesite=strict" in set_cookies.lower()
        assert session_id != ticket and csrf_token != ticket

        # 8. Ticket is single-use.
        status, _, _ = _request(
            "POST",
            f"http://{host}:{port}/api/bootstrap",
            headers={
                "Host": host_port,
                "Origin": f"http://{host}:{port}",
                "Content-Type": "application/json",
            },
            body=exchange_body,
        )
        assert status == 403

        # 9. Authenticated private read works; unauthenticated still denied.
        auth_headers = {
            "Host": host_port,
            "Origin": f"http://{host}:{port}",
            "Cookie": f"wjs_session={session_id}; wjs_csrf={csrf_token}",
        }
        status, _, app_html = _request("GET", f"http://{host}:{port}/app", headers=auth_headers)
        assert status == 200
        assert "Windows Job Scraper" in app_html.decode()

        status, _, _ = _request("GET", f"http://{host}:{port}/api/events", headers={"Host": host_port})
        assert status == 401

        # 10. Mutation without CSRF denied; with CSRF succeeds.
        status, _, _ = _request(
            "POST",
            f"http://{host}:{port}/api/session/logout",
            headers={**auth_headers, "Content-Type": "application/json"},
            body=b"{}",
        )
        assert status == 403
        status, _, _ = _request(
            "POST",
            f"http://{host}:{port}/api/session/logout",
            headers={**auth_headers, "Content-Type": "application/json", "X-CSRF-Token": csrf_token},
            body=b"{}",
        )
        assert status == 200

        # 11. Session revoked -> private read denied.
        status, _, _ = _request("GET", f"http://{host}:{port}/app", headers=auth_headers)
        assert status == 401

    def test_clean_shutdown_and_recovery(self, launched_app):
        config, secret, dashboard_url, launcher = launched_app
        desc = load_runtime_descriptor(config.paths.runtime)
        assert desc is not None
        service_pid = desc.pid

        # Clean launcher shutdown stops the service and clears the descriptor.
        launcher.send_signal(signal.SIGTERM)
        assert launcher.wait(timeout=30) == 0
        deadline = time.time() + 15
        while time.time() < deadline:
            if load_runtime_descriptor(config.paths.runtime) is None:
                break
            time.sleep(0.2)
        assert load_runtime_descriptor(config.paths.runtime) is None

        from jobscraper.launcher.runtime_descriptor import pid_alive

        deadline = time.time() + 10
        while time.time() < deadline and pid_alive(service_pid):
            time.sleep(0.2)
        assert not pid_alive(service_pid), "service child survived launcher shutdown"


class TestPostRunEvidence:
    def test_events_persisted_and_secret_scan_across_data_root(self, launched_app):
        config, secret, dashboard_url, launcher = launched_app
        # Exercise one full bootstrap to have session/ticket material in play.
        ticket = dashboard_url.split("#bootstrap=", 1)[1]
        host_port = dashboard_url.split("//", 1)[1].split("#", 1)[0].rstrip("/")

        status, headers, body = _request(
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
        set_cookies = ", ".join(headers.get_all("Set-Cookie", []))
        session_id = None
        for part in set_cookies.split(","):
            if part.strip().startswith("wjs_session="):
                session_id = part.strip().split("=", 1)[1].split(";", 1)[0]
        assert session_id
        csrf_token = json.loads(body)["csrf_token"]

        # Shutdown cleanly so DB/WAL are checkpointed.
        launcher.send_signal(signal.SIGTERM)
        launcher.wait(timeout=30)

        # Durable lifecycle events exist.
        from jobscraper.db.connection import connect_db

        conn = connect_db(config.paths.database_file)
        try:
            events = list_recent_events(conn, limit=200)
            kinds = {e.kind for e in events}
            assert "SERVICE_STARTED" in kinds
            assert "LAUNCHER_SERVICE_READY" in kinds or "LAUNCHER_DASHBOARD_OPENED" in kinds
        finally:
            conn.close()

        # SECRET SCAN: none of the sensitive values may appear in ANY file
        # under the data root (DB bytes, WAL, events, runtime descriptor...).
        sensitive = {
            "secret_hex": secret.hex(),
            "secret_b64": __import__("base64").b64encode(secret).decode(),
            "ticket": ticket,
            "session_id": session_id,
            "csrf_token": csrf_token,
        }
        scanned = 0
        for path in sorted(config.paths.root.rglob("*")):
            if path.is_file():
                scanned += 1
                blob = path.read_bytes()
                for name, value in sensitive.items():
                    assert value.encode() not in blob, (
                        f"sensitive value {name} leaked into {path}"
                    )
                    if name in ("ticket", "session_id", "csrf_token", "secret_hex", "secret_b64"):
                        continue
                # Raw secret bytes (non-ASCII) must not appear either.
                assert secret not in blob, f"raw install secret leaked into {path}"
        assert scanned >= 3, "expected database/events/descriptor files to scan"

    def test_doctor_healthy_after_initialized_run(self, launched_app):
        config, secret, dashboard_url, launcher = launched_app
        launcher.send_signal(signal.SIGTERM)
        launcher.wait(timeout=30)

        from jobscraper.launcher.doctor import run_doctor

        results = run_doctor(config)
        failures = [(r.name, r.summary) for r in results if r.status == "FAIL"]
        assert not failures, failures
        by_name = {r.name: r for r in results}
        assert by_name["database"].status == "PASS"
        assert by_name["auth_storage"].status == "PASS"
        assert by_name["timezone"].status == "PASS"
