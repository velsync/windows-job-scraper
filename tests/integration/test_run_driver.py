"""S1.10 integration tests: the service-owned run driver against a live feed.

Proves the full collect path: claim → envelope → fetch (no transaction
held) → validity gate → parse → fenced ingest + coverage → cursor
continuation → obligation drain → run aggregation (RUN-01), including
the zero-job success case and the invalid-content partial case.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.driver import execute_run
from jobscraper.profiles.core import create_profile
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T09:00:00.000000Z"


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/jobs?page=1") or self.path == "/jobs":
            body = json.dumps(
                {"jobs": [
                    {"id": "fx-1", "title": "Backend Engineer",
                     "company": "Fixture Corp", "description": "<p>Python</p>",
                     "url": "https://jobs.example.test/jobs/fx-1",
                     "apply_url": "https://jobs.example.test/jobs/fx-1/apply",
                     "locations": ["Berlin"], "created_at": NOW},
                    {"id": "fx-2", "title": "Platform Engineer",
                     "company": "Fixture Corp", "description": "<p>Go</p>",
                     "url": "https://jobs.example.test/jobs/fx-2",
                     "apply_url": "https://jobs.example.test/jobs/fx-2/apply",
                     "locations": ["Remote"], "created_at": NOW},
                ]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/jobs?page=2") or self.path.startswith("/empty"):
            body = json.dumps({"jobs": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/loginfeed"):
            body = b"<html><title>Sign in</title><input type='password'></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def db(tmp_path, server):
    port = server.server_address[1]
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
    database = Database(tmp_path / "driver.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-1','Fixture Feed','PUBLIC_FEED','http://127.0.0.1:{port}/jobs', '{NOW}', '{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}','{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-1','src-1','api','{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, config_json, created_at)
        VALUES ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,
            '{feed_config}','{NOW}');
        """
    )
    yield database
    database.close()


def _start_run(db):
    plan = dict(
        source_id="src-1", source_plan_group_id="grp-1", fallback_rank=0,
        binding_id="bnd-1", binding_revision_id="bndrev-1",
        adapter_id="json_api_feed", adapter_version="1.0.0", adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", execution_class="HTTP",
        permission_profile_id="perm-1", permission_profile_revision=1,
    )
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=NOW)
    enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="http://127.0.0.1/jobs?page=1", logical_key='{"page": 1}',
    )
    return run_id


def test_driver_collects_two_pages_and_aggregates_success(db):
    run_id = _start_run(db)
    profile_id, _ = create_profile(
        db.conn,
        snapshot={"name": "P", "keywords": ["backend"], "eligible_countries": ["DE"],
                  "remote_rules": {"remote_ok": True}, "min_score_inbox": 0},
        now=NOW,
    )
    status = execute_run(db.conn, run_id)
    assert status == "SUCCEEDED"
    # two canonical jobs through the provenance spine
    jobs = db.conn.execute("SELECT id, title FROM jobs ORDER BY title").fetchall()
    assert [j["title"] for j in jobs] == ["Backend Engineer", "Platform Engineer"]
    # requests: page 1 + page 2 both SUCCEEDED
    requests = db.conn.execute(
        "SELECT request_type, status, page_class FROM scrape_requests WHERE request_type='LIST_FETCH'"
        " ORDER BY created_at"
    ).fetchall()
    assert len(requests) == 2
    assert all(r["status"] == "SUCCEEDED" for r in requests)
    assert [r["page_class"] for r in requests] == ["VALID_LIST", "EMPTY"]
    # coverage COMPLETE + absence-authoritative
    cov = db.conn.execute("SELECT * FROM enumeration_coverage").fetchone()
    assert cov["completion_state"] == "COMPLETE"
    assert cov["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
    assert cov["items_observed"] >= 0
    # obligations drained: eligibility + scores exist for the profile
    elig = db.conn.execute("SELECT COUNT(*) FROM job_eligibility").fetchone()[0]
    scores = db.conn.execute("SELECT COUNT(*) FROM job_scores").fetchone()[0]
    assert elig == 2 and scores == 2
    # inbox events surfaced (NEW_ELIGIBLE_APPEARANCE)
    events = db.conn.execute(
        "SELECT COUNT(*) FROM job_profile_inbox_events WHERE event_kind='NEW_ELIGIBLE_APPEARANCE'"
    ).fetchone()[0]
    assert events == 2
    # evidence chain persisted
    assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 2


def test_driver_zero_job_feed_is_success(db, server):
    # point the binding at an empty feed (page 1 immediately empty)
    db.conn.execute(
        "UPDATE source_adapter_binding_revisions SET config_json = ?"
        " WHERE id = 'bndrev-1'",
        (json.dumps({
            "url_template": f"http://127.0.0.1:{server.server_address[1]}/empty?page={{page}}",
            "items_path": "jobs",
            "fields": {"source_job_id": {"path": "id", "required": True},
                        "title": {"path": "title", "required": True}},
        }),),
    )
    db.conn.commit()
    run_id = _start_run(db)
    status = execute_run(db.conn, run_id)
    assert status == "SUCCEEDED"  # valid complete zero-job result (RUN-01)
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_driver_invalid_content_is_partial_not_failure(db, server):
    db.conn.execute(
        "UPDATE source_adapter_binding_revisions SET config_json = ?"
        " WHERE id = 'bndrev-1'",
        (json.dumps({
            "url_template": f"http://127.0.0.1:{server.server_address[1]}/loginfeed?page={{page}}",
            "items_path": "jobs",
            "fields": {"source_job_id": {"path": "id", "required": True},
                        "title": {"path": "title", "required": True}},
        }),),
    )
    db.conn.commit()
    run_id = _start_run(db)
    status = execute_run(db.conn, run_id)
    assert status == "PARTIAL"
    req = db.conn.execute(
        "SELECT page_class FROM scrape_requests WHERE request_type='LIST_FETCH'"
    ).fetchone()
    assert req["page_class"] == "LOGIN_REQUIRED"  # typed, not parser-failure
    cov = db.conn.execute("SELECT completion_state FROM enumeration_coverage").fetchone()
    assert cov["completion_state"] == "PARTIAL"  # no absence authority
