"""Regression: live cancel remains responsive while run source I/O is blocked."""

from __future__ import annotations

import http.server
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from jobscraper.config import AppConfig
from jobscraper.db.connection import Database, connect_db
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.paths import build_app_paths, ensure_app_directories
from jobscraper.runtime.clock import begin_service_epoch
from jobscraper.security.install_secret import load_or_create_install_secret
from jobscraper.service.app import create_service_app

NOW = "2026-10-02T18:00:00.000000Z"
_PAGE2_ENTERED = threading.Event()
_PAGE2_RELEASE = threading.Event()


class _BlockingFeedHandler(http.server.BaseHTTPRequestHandler):
    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/jobs?page=1"):
            self._send({"jobs": [{
                "id": "cancel-a",
                "title": "W3 Cancel Engineer",
                "company": "W3 Fixture",
                "description": "accepted before cancellation",
                "url": "https://jobs.example.test/jobs/cancel-a",
                "apply_url": "https://jobs.example.test/jobs/cancel-a/apply",
                "locations": ["Remote"],
                "created_at": NOW,
            }]})
            return
        if self.path.startswith("/jobs?page=2"):
            _PAGE2_ENTERED.set()
            if not _PAGE2_RELEASE.wait(timeout=15):
                self.send_response(504)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send({"jobs": [{
                "id": "cancel-b",
                "title": "Must Not Commit After Cancellation",
                "company": "W3 Fixture",
                "description": "in flight at cancellation",
                "url": "https://jobs.example.test/jobs/cancel-b",
                "apply_url": "https://jobs.example.test/jobs/cancel-b/apply",
                "locations": ["Remote"],
                "created_at": NOW,
            }]})
            return
        if self.path.startswith("/jobs?page=3"):
            self._send({"jobs": []})
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def feed_server():
    _PAGE2_ENTERED.clear()
    _PAGE2_RELEASE.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BlockingFeedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        _PAGE2_RELEASE.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def service(tmp_path, feed_server):
    root = tmp_path / "root"
    paths = build_app_paths(root)
    ensure_app_directories(paths)
    secret = load_or_create_install_secret(paths)
    db = Database(paths.database_file)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    begin_service_epoch(db.conn)

    port = feed_server.server_address[1]
    config = json.dumps({
        "url_template": f"http://127.0.0.1:{port}/jobs?page={{page}}",
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
    }, sort_keys=True)

    db.conn.execute(
        "INSERT INTO sources (id,display_name,source_family,entry_url,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("w308-src", "W3-08", "PUBLIC_FEED", f"http://127.0.0.1:{port}/jobs", NOW, NOW),
    )
    db.conn.execute(
        "INSERT INTO adapter_definitions (adapter_id,adapter_version,adapter_api_version,manifest_json,created_at) VALUES ('json_api_feed','1.0.0','1','{}',?)",
        (NOW,),
    )
    db.conn.execute(
        "INSERT INTO adapter_permission_profiles(id,display_name,created_at) VALUES ('w308-perm','W3-08',?)",
        (NOW,),
    )
    db.conn.execute(
        "INSERT INTO adapter_permission_profile_revisions (id,permission_profile_id,revision,policy_json,created_at) VALUES ('w308-perm-rev','w308-perm',1,'{}',?)",
        (NOW,),
    )
    db.conn.execute(
        "INSERT INTO source_adapter_bindings (id,source_id,display_name,current_revision_id,created_at) VALUES ('w308-bnd','w308-src','W3-08','w308-rev',?)",
        (NOW,),
    )
    db.conn.execute(
        """INSERT INTO source_adapter_binding_revisions
        (id,binding_id,revision,adapter_id,adapter_version,strategy,execution_class,
         permission_profile_id,permission_profile_revision,config_json,created_at)
        VALUES ('w308-rev','w308-bnd',1,'json_api_feed','1.0.0',
        'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','w308-perm',1,?,?)""",
        (config, NOW),
    )
    db.conn.commit()

    app, state = create_service_app(AppConfig(data_root=root), db, port=8765, secret=secret)
    try:
        yield {"app": app, "state": state, "db": db}
    finally:
        _PAGE2_RELEASE.set()
        db.close()


def _client(service):
    client = TestClient(service["app"], base_url="http://127.0.0.1:8765")
    state = service["state"]
    session_id, csrf = state.sessions.create(state.instance_id)
    client.cookies.set("wjs_session", session_id)
    client.cookies.set("wjs_csrf", csrf)
    client.headers.update({
        "X-CSRF-Token": csrf,
        "host": "127.0.0.1:8765",
        "origin": "http://127.0.0.1:8765",
    })
    return client


def test_cancel_endpoint_remains_responsive_while_run_waits_on_source_io(service):
    run_client = _client(service)
    cancel_client = _client(service)
    profile = run_client.post("/api/profiles", json={
        "name": "W3-08",
        "keywords": ["engineer"],
        "eligible_countries": ["DE"],
        "remote_rules": {"remote_ok": True},
        "min_score_inbox": 0,
    })
    assert profile.status_code == 200

    result = {}
    def _start_run():
        try:
            result["response"] = run_client.post(
                "/api/runs", json={"profile_id": profile.json()["id"]}
            )
        except BaseException as exc:  # pragma: no cover
            result["error"] = exc

    run_thread = threading.Thread(target=_start_run, daemon=True)
    run_thread.start()
    observer = connect_db(service["db"].path)
    try:
        assert _PAGE2_ENTERED.wait(timeout=10), "run never reached blocked page 2"
        deadline = time.time() + 5
        run = None
        while time.time() < deadline:
            run = observer.execute(
                "SELECT id,status,cancel_requested_at FROM scrape_runs WHERE status='RUNNING' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if run is not None:
                break
            time.sleep(0.05)
        assert run is not None, "no durable RUNNING run at cancellation boundary"
        accepted = observer.execute(
            "SELECT COUNT(*) FROM job_observations WHERE run_id=?", (run["id"],)
        ).fetchone()[0]
        assert accepted == 1

        started = time.perf_counter()
        cancelled = cancel_client.post(f"/api/runs/{run['id']}/cancel")
        elapsed = time.perf_counter() - started
        assert cancelled.status_code == 200
        assert elapsed < 2.0, f"cancel endpoint blocked for {elapsed:.3f}s"
        durable = observer.execute(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id=?", (run["id"],)
        ).fetchone()
        assert durable["cancel_requested_at"] is not None

        _PAGE2_RELEASE.set()
        run_thread.join(timeout=20)
        assert not run_thread.is_alive(), "run did not settle after cancellation"
        assert "error" not in result
        response = result.get("response")
        assert response is not None
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        final = observer.execute(
            "SELECT status FROM scrape_runs WHERE id=?", (run["id"],)
        ).fetchone()
        assert final["status"] == "CANCELLED"
        observations = observer.execute(
            "SELECT source_job_id FROM job_observations WHERE run_id=? ORDER BY source_job_id",
            (run["id"],),
        ).fetchall()
        assert [row["source_job_id"] for row in observations] == ["cancel-a"]
    finally:
        _PAGE2_RELEASE.set()
        run_thread.join(timeout=5)
        observer.close()
