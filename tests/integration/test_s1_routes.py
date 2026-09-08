"""S1.10 integration tests: Slice-1 service API behind the security shell.

Proves:

* every new route requires an authenticated session (401) and mutations
  require CSRF (403) — the Slice-0 shell covers the new surface;
* profiles CRUD with immutable revisions;
* run creation + synchronous bounded execution against the fixture feed,
  run listing, cancellation;
* inbox listing with score/breakdown/eligibility; disposition round-trip
  with optimistic-concurrency 409;
* job detail exposes all source provenance links and only safelink-
  approved URLs (a javascript: application URL never surfaces);
* applications CRUD.
"""

from __future__ import annotations

import http.client
import http.server
import json
import threading

import pytest

from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.paths import build_app_paths
from jobscraper.security.install_secret import load_or_create_install_secret
from jobscraper.service.app import create_service_app

NOW = "2026-09-08T09:00:00.000000Z"


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if "page=1" in self.path or self.path == "/jobs":
            body = json.dumps(
                {"jobs": [
                    {"id": "fx-1", "title": "Backend Engineer", "company": "Fixture Corp",
                     "description": "<p>Python backend</p>",
                     "url": "https://jobs.example.test/jobs/fx-1",
                     "apply_url": "https://jobs.example.test/jobs/fx-1/apply",
                     "locations": ["Berlin"], "created_at": NOW},
                ]}
            ).encode()
        else:
            body = json.dumps({"jobs": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def feed_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def service(tmp_path, feed_server):
    root = tmp_path / "root"
    paths = build_app_paths(root)
    from jobscraper.paths import ensure_app_directories
    ensure_app_directories(paths)
    secret = load_or_create_install_secret(paths)
    db = Database(paths.database_file)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    port = feed_server.server_address[1]
    feed_config = json.dumps(
        {
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
        }
    )
    db.conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-1','Fixture Feed','PUBLIC_FEED','http://127.0.0.1:{port}/jobs','{NOW}','{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}','{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, current_revision_id, created_at)
        VALUES ('bnd-1','src-1','api','bndrev-1','{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, config_json, created_at)
        VALUES ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,
            '{feed_config}','{NOW}');
        """
    )
    db.conn.commit()
    app, state = create_service_app(
        AppConfig(data_root=root), db, port=8765, secret=secret
    )
    yield {"app": app, "state": state, "db": db, "port": 8765, "root": root}
    db.close()


def _client(service_fixture):
    from fastapi.testclient import TestClient

    client = TestClient(service_fixture["app"], base_url="http://127.0.0.1:8765")
    # bootstrap a session directly through the state's session registry
    state = service_fixture["state"]
    session_id, csrf = state.sessions.create(state.instance_id)
    client.cookies.set("wjs_session", session_id)
    client.cookies.set("wjs_csrf", csrf)
    client.headers.update(
        {
            "X-CSRF-Token": csrf,
            "host": "127.0.0.1:8765",
            "origin": "http://127.0.0.1:8765",
        }
    )
    return client


def _unauth_client(service_fixture):
    from fastapi.testclient import TestClient

    client = TestClient(service_fixture["app"], base_url="http://127.0.0.1:8765")
    client.headers.update(
        {"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}
    )
    return client


# ------------------------------------------------------- security shell


def test_new_routes_require_session_and_csrf(service):
    client = _unauth_client(service)
    for method, path in (
        ("GET", "/api/profiles"),
        ("GET", "/api/runs"),
        ("GET", "/api/inbox?profile_id=x"),
        ("GET", "/api/jobs/j1"),
        ("GET", "/api/applications?profile_id=x"),
    ):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/profiles", json={"name": "x"}).status_code == 401
    # session but no CSRF header
    state = service["state"]
    from fastapi.testclient import TestClient

    client2 = TestClient(service["app"], base_url="http://127.0.0.1:8765")
    session_id, csrf = state.sessions.create(state.instance_id)
    client2.cookies.set("wjs_session", session_id)
    client2.cookies.set("wjs_csrf", csrf)
    client2.headers.update(
        {"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}
    )
    assert client2.post("/api/profiles", json={"name": "x"}).status_code == 403


# ------------------------------------------------------------- profiles


def test_profile_crud_with_revisions(service):
    client = _client(service)
    created = client.post(
        "/api/profiles",
        json={"name": "Backend EU", "keywords": ["backend"],
              "eligible_countries": ["DE"], "remote_rules": {"remote_ok": True},
              "min_score_inbox": 0},
    )
    assert created.status_code == 200
    profile_id = created.json()["id"]
    listed = client.get("/api/profiles").json()["profiles"]
    assert [p["name"] for p in listed] == ["Backend EU"]
    edited = client.patch(
        f"/api/profiles/{profile_id}", json={"name": "Backend EU strict"}
    )
    assert edited.status_code == 200
    revisions = service["db"].conn.execute(
        "SELECT revision FROM profile_revisions WHERE profile_id = ? ORDER BY revision",
        (profile_id,),
    ).fetchall()
    assert [r["revision"] for r in revisions] == [1, 2]
    bad = client.post("/api/profiles", json={})
    assert bad.status_code == 400


# ------------------------------------------------------------------ runs


def test_run_create_executes_and_lists(service):
    client = _client(service)
    profile = client.post(
        "/api/profiles",
        json={"name": "P", "keywords": ["backend"], "eligible_countries": ["DE"],
              "remote_rules": {"remote_ok": True}, "min_score_inbox": 0},
    ).json()
    result = client.post("/api/runs", json={"profile_id": profile["id"]})
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "SUCCEEDED"
    assert body["jobs_saved"] == 1
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "SUCCEEDED"


def test_run_cancel(service):
    client = _client(service)
    profile = client.post("/api/profiles", json={"name": "P", "min_score_inbox": 0}).json()
    run = client.post("/api/runs", json={"profile_id": profile["id"]}).json()
    cancelled = client.post(f"/api/runs/{run['run_id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


# ---------------------------------------------------------- inbox + jobs


def test_inbox_disposition_and_job_detail(service):
    client = _client(service)
    profile = client.post(
        "/api/profiles",
        json={"name": "P", "keywords": ["backend"], "eligible_countries": ["DE"],
              "remote_rules": {"remote_ok": True}, "min_score_inbox": 0},
    ).json()
    client.post("/api/runs", json={"profile_id": profile["id"]})
    inbox = client.get(f"/api/inbox?profile_id={profile['id']}").json()["inbox"]
    assert len(inbox) == 1
    assert inbox[0]["title"] == "Backend Engineer"
    assert inbox[0]["score"] is not None and inbox[0]["breakdown"]
    job_id = inbox[0]["job_id"]

    # disposition round-trip with optimistic concurrency
    set1 = client.post(
        f"/api/profiles/{profile['id']}/jobs/{job_id}/disposition",
        json={"disposition": "SHORTLISTED"},
    )
    assert set1.status_code == 200
    revision = set1.json()["row_revision"]
    conflict = client.post(
        f"/api/profiles/{profile['id']}/jobs/{job_id}/disposition",
        json={"disposition": "DISMISSED", "expected_row_revision": revision - 1},
    )
    assert conflict.status_code == 409
    ok = client.post(
        f"/api/profiles/{profile['id']}/jobs/{job_id}/disposition",
        json={"disposition": "DISMISSED", "expected_row_revision": revision},
    )
    assert ok.status_code == 200

    # job detail: provenance visible, apply link safe
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["job"]["title"] == "Backend Engineer"
    assert len(detail["sources"]) == 1
    assert detail["sources"][0]["application_url"] == "https://jobs.example.test/jobs/fx-1/apply"
    assert detail["apply_url"] == "https://jobs.example.test/jobs/fx-1/apply"

    # a hostile application URL in provenance never surfaces
    service["db"].conn.execute(
        "UPDATE job_sources SET application_url = 'javascript:alert(1)' WHERE job_id = ?",
        (job_id,),
    )
    service["db"].conn.commit()
    detail2 = client.get(f"/api/jobs/{job_id}").json()
    assert detail2["apply_url"] is None
    assert detail2["sources"][0]["application_url"] is None


def test_job_detail_404(service):
    client = _client(service)
    assert client.get("/api/jobs/missing").status_code == 404


# --------------------------------------------------------- applications


def test_application_crud_via_api(service):
    client = _client(service)
    profile = client.post("/api/profiles", json={"name": "P", "min_score_inbox": 0}).json()
    client.post("/api/runs", json={"profile_id": profile["id"]})
    job_id = client.get(f"/api/inbox?profile_id={profile['id']}").json()["inbox"][0]["job_id"]

    created = client.post(
        "/api/applications", json={"job_id": job_id, "profile_id": profile["id"]}
    )
    assert created.status_code == 200
    app_id = created.json()["id"]
    assert created.json()["status"] == "PREPARING"

    updated = client.patch(
        f"/api/applications/{app_id}", json={"status": "APPLIED"}
    )
    assert updated.status_code == 200 and updated.json()["status"] == "APPLIED"

    bad = client.patch(f"/api/applications/{app_id}", json={"status": "BOGUS"})
    assert bad.status_code == 400
    listed = client.get(f"/api/applications?profile_id={profile['id']}").json()["applications"]
    assert len(listed) == 1
    assert client.post(
        "/api/applications", json={"job_id": "missing", "profile_id": profile["id"]}
    ).status_code == 404
