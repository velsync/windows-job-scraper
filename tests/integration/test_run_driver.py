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
import time
from pathlib import Path

import pytest

from jobscraper.db.connection import Database, connect_db
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.driver import execute_run
from jobscraper.profiles.core import create_profile
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T09:00:00.000000Z"

# cross-thread state for the fixture feed handler (db path for the
# lease-expiring endpoint)
_HANDLER_STATE: dict = {}


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
        elif self.path.startswith("/slowpage2"):
            # page 1 answers immediately with jobs; page 2 hangs long
            # enough for a concurrent durable cancellation to land while
            # its fetch is in flight
            if "page=1" in self.path or "page=1&" in self.path or self.path.endswith("/slowpage2"):
                body = json.dumps(
                    {"jobs": [
                        {"id": "fx-1", "title": "Backend Engineer",
                         "company": "Fixture Corp",
                         "url": "https://jobs.example.test/jobs/fx-1"},
                        {"id": "fx-2", "title": "Platform Engineer",
                         "company": "Fixture Corp",
                         "url": "https://jobs.example.test/jobs/fx-2"},
                    ]}
                ).encode()
            else:
                time.sleep(4.0)
                body = json.dumps({"jobs": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/expiring"):
            # page 1 that expires the caller's lease mid-fetch: the
            # fenced commit must then lose ownership (RUN-07)
            from jobscraper.db.connection import connect_db

            db_path = _HANDLER_STATE.get("db_path")
            if db_path:
                other = connect_db(Path(db_path))
                try:
                    other.execute(
                        "UPDATE scrape_requests SET lease_until = '2020-01-01T00:00:00Z'"
                        " WHERE status = 'RUNNING'"
                    )
                    other.commit()
                finally:
                    other.close()
            body = json.dumps(
                {"jobs": [
                    {"id": "fx-1", "title": "Backend Engineer",
                     "company": "Fixture Corp",
                     "url": "https://jobs.example.test/jobs/fx-1"},
                ]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/redirect-denied-feed"):
            # redirect hop to a TEST-NET destination: policy-denied
            # mid-chain (the initial loopback URL is granted)
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.9/evil")
            self.send_header("Content-Length", "0")
            self.end_headers()
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
    _HANDLER_STATE["db_path"] = str(tmp_path / "driver.db")
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
    # Model the service lifetime: the coordinator opens its service epoch
    # before any claiming (03 §50; claims fail closed without one).
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
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


def _slow_binding(server):
    return json.dumps(
        {
            "url_template": f"http://127.0.0.1:{server.server_address[1]}/slowpage2?page={{page}}",
            "items_path": "jobs",
            "fields": {
                "source_job_id": {"path": "id", "required": True},
                "title": {"path": "title", "required": True},
                "company": {"path": "company"},
                "job_url": {"path": "url"},
            },
        }
    )


def test_driver_mid_run_cancellation_no_late_commits(db, server):
    """§18 mid-run: durable cancellation while page 2 is in flight.

    The service executes runs synchronously, so a concurrent API request
    cannot be served while the run handler holds the loop; the durable
    cancellation here is written through a second DB connection — the
    same durable primitive the cancel route applies.  The driver must:

    * lose commit authority at the fence for the in-flight acquisition
      page (no post-cancellation data commits, evidence rolled back),
    * cooperatively abandon the request,
    * aggregate the run to CANCELLED,
    * keep the already accepted page-1 work and drain its obligations.
    """
    db.conn.execute(
        "UPDATE source_adapter_binding_revisions SET config_json = ? WHERE id = 'bndrev-1'",
        (_slow_binding(server),),
    )
    db.conn.commit()
    profile_id, _ = create_profile(
        db.conn,
        snapshot={"name": "P", "keywords": ["engineer"], "eligible_countries": ["DE"],
                  "remote_rules": {"remote_ok": True}, "min_score_inbox": 0},
        now=NOW,
    )
    run_id = _start_run(db)

    result = {}

    def _drive():
        result["status"] = execute_run(db.conn, run_id)

    worker = threading.Thread(target=_drive)
    worker.start()
    try:
        # wait until page 1 committed (its request reached SUCCEEDED)
        other = connect_db(Path(_HANDLER_STATE["db_path"]))
        deadline = time.time() + 30
        page1 = None
        while time.time() < deadline:
            page1 = other.execute(
                "SELECT id, status FROM scrape_requests"
                " WHERE run_id = ? AND status = 'SUCCEEDED' ORDER BY created_at",
                (run_id,),
            ).fetchall()
            if page1:
                break
            time.sleep(0.05)
        assert page1, "page 1 never committed before the cancellation window"
        # page 2 fetch is now hanging (4s): cancel durably mid-run
        request_run_cancellation(other, run_id)
        other.close()
    finally:
        worker.join(timeout=60)

    assert result.get("status") == "CANCELLED"
    run = db.conn.execute("SELECT status FROM scrape_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "CANCELLED"
    group = db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert group["group_outcome"] == "CANCELLED"
    requests = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE run_id = ? ORDER BY created_at", (run_id,)
    ).fetchall()
    # accepted page-1 work preserved; no request claims success after cancellation
    assert any(r["status"] == "SUCCEEDED" for r in requests)
    assert all(r["status"] in ("SUCCEEDED", "CANCELLED") for r in requests)
    # only page 1 committed evidence; page 2's rolled back at the fence
    assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 2
    # coverage can not claim terminal enumeration after cancellation.
    # S3.8 records an explicit CANCELLED coverage state instead of the old
    # PARTIAL blur (S3.8 corrective review #9); like PARTIAL it never applies
    # absence because absence requires a COMPLETE barrier.
    cov = db.conn.execute("SELECT completion_state FROM enumeration_coverage").fetchone()
    assert cov["completion_state"] == "CANCELLED"
    # §18: host-native obligations for accepted work still drained
    assert db.conn.execute("SELECT COUNT(*) FROM job_scores").fetchone()[0] == 2


def test_driver_lease_loss_stops_cleanly(db, server):
    """RUN-07 mid-run: an expired lease is already lost ownership.  The
    driver's fenced commit is refused, nothing is committed for that
    page, the expired request is reclaimed (RETRY_WAIT), and the run
    aggregates without crashing."""
    db.conn.execute(
        "UPDATE source_adapter_binding_revisions SET config_json = ? WHERE id = 'bndrev-1'",
        (json.dumps({
            "url_template": f"http://127.0.0.1:{server.server_address[1]}/expiring?page={{page}}",
            "items_path": "jobs",
            "fields": {"source_job_id": {"path": "id", "required": True},
                        "title": {"path": "title", "required": True}},
        }),),
    )
    db.conn.commit()
    run_id = _start_run(db)

    status = execute_run(db.conn, run_id)

    assert status is None  # reclaimed RETRY_WAIT keeps terminal run truth open
    run = db.conn.execute(
        "SELECT status, finished_at FROM scrape_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert tuple(run) == ("RUNNING", None)
    plan = db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert plan["group_outcome"] is None
    req = db.conn.execute(
        "SELECT status, last_failure_kind FROM scrape_requests WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    assert all(r["status"] == "RETRY_WAIT" for r in req)
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 0


def test_driver_redirect_denied_is_partial_and_grants_no_absence_authority(db, server):
    """§21/RUN-13 regression: a mid-chain policy denial carries the hop's
    status code but no page.  The run must end PARTIAL with coverage
    PARTIAL — never a terminal EMPTY that would grant absence authority
    over jobs the run never actually enumerated.

    S3.4 normative transition supersedes the old SUCCEEDED expectation for
    the owning request: a definitive policy failure is a failed acquisition
    unit (FAILED with POLICY_REJECTED evidence), not a success. The safety
    assertions — PARTIAL run/coverage, no absence authority, no retry
    storm — are unchanged and still hold.
    """
    db.conn.execute(
        "UPDATE source_adapter_binding_revisions SET config_json = ?"
        " WHERE id = 'bndrev-1'",
        (json.dumps({
            "url_template": f"http://127.0.0.1:{server.server_address[1]}"
            "/redirect-denied-feed?page={page}",
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
        "SELECT status, page_class FROM scrape_requests WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert req["status"] == "FAILED"  # definitive policy failure: failed unit
    assert req["page_class"] == "UNKNOWN"
    fa = db.conn.execute(
        "SELECT failure_kind FROM fetch_attempts WHERE request_id ="
        " (SELECT id FROM scrape_requests WHERE run_id = ?)",
        (run_id,),
    ).fetchone()
    assert fa["failure_kind"] == "POLICY_REJECTED"
    cov = db.conn.execute("SELECT * FROM enumeration_coverage").fetchone()
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0
    group = db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert group["group_outcome"] == "SATISFIED_PARTIAL"


def test_execute_run_claim_path_cannot_bypass_clock_anomaly_detection(db):
    """S3.1 corrective round 2: the real service run path claims through the
    service-lifetime clock guard. A material forward wall-clock anomaly
    mid-run must rotate the epoch, reclaim RUNNING work bound to the
    invalidated ORIGINAL epoch, and resume — ``execute_run`` cannot bypass
    detection (03 §50, R2-F11 seam)."""
    from jobscraper.runtime.claims import claim_next_request
    from jobscraper.runtime.clock import (
        ServiceClockGuard,
        current_service_epoch,
        db_utc_now,
    )

    run_id = _start_run(db)

    # an orphaned RUNNING attempt bound to the current epoch in a separate
    # run (worker claimed, then died before commit): anomaly recovery inside
    # the claim path must abandon and requeue it
    orphan_plan = dict(
        source_id="src-1", source_plan_group_id="grp-orphan", fallback_rank=0,
        binding_id="bnd-1", binding_revision_id="bndrev-1",
        adapter_id="json_api_feed", adapter_version="1.0.0", adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", execution_class="HTTP",
        permission_profile_id="perm-1", permission_profile_revision=1,
    )
    orphan_run, orphan_plans = create_run(
        db.conn, profile_id=None, plans=[orphan_plan], now=NOW
    )
    orphan_req, _ = enqueue_request(
        db.conn, run_id=orphan_run, run_source_plan_id=orphan_plans[0],
        source_id="src-1", binding_id="bnd-1", request_type="DETAIL_FETCH",
        target_identity="http://127.0.0.1/jobs/orphan",
        logical_key='{"orphan": true}',
    )
    orphan_claim = claim_next_request(
        db.conn, "dead-worker", now=db_utc_now(db.conn),
        run_source_plan_id=orphan_plans[0],
    )
    assert orphan_claim is not None and orphan_claim.request_id == orphan_req

    epoch = current_service_epoch(db.conn)
    # deterministic seam: real database clock vs a frozen monotonic baseline
    # at zero tolerance, so the real elapsed time between claims is a
    # material forward drift
    guard = ServiceClockGuard(epoch, tolerance_s=0.0, monotonic=lambda: 0.0)

    status = execute_run(db.conn, run_id, guard=guard)
    assert status == "SUCCEEDED"

    # detection is durable: at least one epoch ended by a clock anomaly
    anomalies = db.conn.execute(
        "SELECT COUNT(*) FROM service_clock_epochs"
        " WHERE end_reason LIKE 'CLOCK_ANOMALY%'"
    ).fetchone()[0]
    assert anomalies >= 1

    # recovery reclaimed the original epoch's orphaned RUNNING work
    att = db.conn.execute(
        "SELECT outcome, abandoned_reason FROM request_attempts WHERE attempt_id = ?",
        (orphan_claim.attempt_id,),
    ).fetchone()
    assert att["outcome"] == "ABANDONED"
    assert att["abandoned_reason"] == "CLOCK_ANOMALY"
    row = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (orphan_req,)
    ).fetchone()
    assert row["status"] == "RETRY_WAIT"
    # the run's own work completed under the post-anomaly epoch(es)
    pages = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE run_id = ? AND request_type = 'LIST_FETCH'"
        " ORDER BY created_at",
        (run_id,),
    ).fetchall()
    assert len(pages) == 2 and all(p["status"] == "SUCCEEDED" for p in pages)


def test_epoch_rotation_with_live_lease_leaves_plan_open(db):
    """Stale service epoch + unexpired lease: no false plan/run closure.

    The orphaned RUNNING attempt is still live (lease unexpired), so the
    pass must stay open for redrive: no group outcome, no run terminal
    status, no coverage finalization, request untouched. Epoch invalidation
    alone closes nothing.
    """
    from jobscraper.runtime.claims import claim_next_request
    from jobscraper.runtime.clock import begin_service_epoch

    run_id = _start_run(db)
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim is not None
    row = db.conn.execute(
        "SELECT status, current_attempt_id, lease_until FROM scrape_requests"
        " WHERE id = ?",
        (claim.request_id,),
    ).fetchone()
    assert row["status"] == "RUNNING" and row["lease_until"] > NOW

    # rotate the service epoch: the live lease is now epoch-stale, but the
    # attempt itself was not consumed.
    begin_service_epoch(db.conn, now=NOW)

    assert execute_run(db.conn, run_id) is None
    row = db.conn.execute(
        "SELECT status, current_attempt_id FROM scrape_requests WHERE id = ?",
        (claim.request_id,),
    ).fetchone()
    assert row["status"] == "RUNNING"
    assert row["current_attempt_id"] == claim.attempt_id
    group = db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert group["group_outcome"] is None
    run = db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert run["status"] == "RUNNING"
    coverage = db.conn.execute(
        "SELECT completion_state, finalized_at, terminal_enumeration_proven"
        " FROM enumeration_coverage"
    ).fetchall()
    assert all(
        row["finalized_at"] is None and not row["terminal_enumeration_proven"]
        for row in coverage
    )
