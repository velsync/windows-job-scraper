"""S1.11 integration tests: restart recovery + terminal §18 cancellation.

Proves the crash/cancellation semantics the acceptance gate relies on:

* a fresh service epoch orphans every RUNNING request (RUN-09: the
  service process is the single claim/capacity coordinator, so no worker
  can exist after a restart) — restart recovery reclaims them with the
  RUN-07 transitions: prior attempt ABANDONED, RETRY_WAIT while the
  attempt budget remains, FAILED once exhausted;
* runs whose durable cancellation was interrupted by the crash finalize
  to CANCELLED after recovery (pending acquisition work cancelled,
  groups closed, aggregate status persisted);
* non-cancelled interrupted runs stay honestly non-terminal (the
  operator cancels or starts a new run that resumes from the cursor).
"""

from __future__ import annotations

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.recovery import recover_interrupted_requests
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import aggregate_run, create_run

NOW = "2026-09-08T08:00:00.000000Z"
LATER = "2026-09-08T08:05:00.000000Z"


def _family(db: Database):
    conn = db.conn
    conn.execute(
        "INSERT INTO sources (id, display_name, source_family, entry_url, desired_state,"
        " administrative_state, created_at, updated_at)"
        " VALUES ('src-1','Feed','PUBLIC_FEED','https://example.test/feed','ENABLED','NORMAL', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version,"
        " manifest_json, created_at) VALUES ('feed','1.0.0','1','{}',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles (id, display_name, created_at)"
        " VALUES ('perm-1','default',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id,"
        " revision, policy_json, created_at) VALUES ('permrev-1','perm-1',1,'{}',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)"
        " VALUES ('bnd-1','src-1','api',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id,"
        " adapter_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at)"
        " VALUES ('bndrev-1','bnd-1',1,'feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT',"
        "'HTTP','perm-1',1,?)",
        (NOW,),
    )
    conn.commit()


def _plan():
    return dict(
        source_id="src-1",
        source_plan_group_id="grp-1",
        fallback_rank=0,
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "recovery.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    _family(database)
    # Model the service lifetime: the coordinator opens its service epoch
    # before any claiming (03 §50; claims fail closed without one).
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    yield database
    database.close()


def _interrupted_request(db: Database, *, request_type="LIST_FETCH", priority=10):
    """A run with one in-flight (claimed, worker now dead) request."""
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    request_id, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type=request_type,
        target_identity="https://example.test/feed?page=1", priority=priority,
    )
    claim = claim_next_request(db.conn, "dead-worker", now=NOW)
    assert claim is not None and claim.request_id == request_id
    return run_id, plans[0], request_id, claim


def test_restart_recovery_reclaims_orphaned_running_requests(db):
    run_id, _plan_id, request_id, claim = _interrupted_request(db)

    report = recover_interrupted_requests(db.conn, now=LATER)

    assert report["reclaimed"] == [request_id]
    row = db.conn.execute(
        "SELECT status, current_worker_id, current_attempt_id, lease_until,"
        " next_retry_at FROM scrape_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    assert row["status"] == "RETRY_WAIT"  # budget remains (1 of 3 attempts)
    assert row["current_worker_id"] is None and row["current_attempt_id"] is None
    assert row["lease_until"] is None
    assert row["next_retry_at"] > LATER  # backed off before retry
    attempt = db.conn.execute(
        "SELECT outcome, abandoned_reason FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert attempt["outcome"] == "ABANDONED"
    assert attempt["abandoned_reason"] == "SERVICE_RESTART"
    # a non-cancelled interrupted run stays honestly non-terminal
    run = db.conn.execute("SELECT status FROM scrape_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] in ("QUEUED", "RUNNING")


def test_restart_recovery_fails_requests_with_exhausted_budget(db):
    _run_id, _plan_id, request_id, _claim = _interrupted_request(db)
    # the crashed attempt was the last allowed one
    db.conn.execute(
        "UPDATE scrape_requests SET attempt_count = max_attempts WHERE id = ?",
        (request_id,),
    )
    db.conn.commit()

    report = recover_interrupted_requests(db.conn, now=LATER)

    assert report["reclaimed"] == [request_id]
    row = db.conn.execute(
        "SELECT status, last_failure_kind FROM scrape_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    assert row["status"] == "FAILED"
    assert row["last_failure_kind"] == "LEASE_LOST"


def test_recovery_finalizes_cancelled_runs_after_crash(db):
    """Durable cancellation interrupted by a crash reaches its terminal
    CANCELLED state after restart recovery (§18)."""
    run_id, _plan_id, request_id, _claim = _interrupted_request(db)
    request_run_cancellation(db.conn, run_id, now=LATER)
    # the cooperative abandon never happened (worker died mid-request)
    row = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (request_id,)
    ).fetchone()
    assert row["status"] == "RUNNING"

    report = recover_interrupted_requests(db.conn, now=LATER)

    assert report["reclaimed"] == [request_id]
    assert report["finalized_cancelled_runs"] == [run_id]
    row = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (request_id,)
    ).fetchone()
    assert row["status"] == "CANCELLED"
    group = db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert group["group_outcome"] == "CANCELLED"
    run = db.conn.execute(
        "SELECT status, finished_at FROM scrape_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert run["status"] == "CANCELLED" and run["finished_at"] is not None
    # idempotent: a second recovery pass changes nothing
    again = recover_interrupted_requests(db.conn, now=LATER)
    assert again["reclaimed"] == [] and again["finalized_cancelled_runs"] == []
    assert aggregate_run(db.conn, run_id, now=LATER) == "CANCELLED"


def test_recovery_on_quiescent_database_is_a_noop(db):
    report = recover_interrupted_requests(db.conn, now=LATER)
    assert report == {"reclaimed": [], "finalized_cancelled_runs": []}
