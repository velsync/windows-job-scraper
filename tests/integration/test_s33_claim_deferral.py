"""S3.3 durable claim/capacity coordination tests."""

from __future__ import annotations

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.claim_control import yield_unstarted_claim
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.clock import begin_service_epoch
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-11T08:00:00.000000Z"


def _seed(database: Database) -> str:
    c = database.conn
    c.execute(
        "INSERT INTO sources (id,display_name,source_family,entry_url,created_at,updated_at)"
        " VALUES ('src','S','PUBLIC_FEED','https://example.test/jobs',?,?)",
        (NOW, NOW),
    )
    c.execute(
        "INSERT INTO adapter_definitions (adapter_id,adapter_version,adapter_api_version,manifest_json,created_at)"
        " VALUES ('feed','1','1','{}',?)", (NOW,)
    )
    c.execute(
        "INSERT INTO adapter_permission_profiles (id,display_name,created_at) VALUES ('perm','P',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO adapter_permission_profile_revisions"
        " (id,permission_profile_id,revision,policy_json,created_at) VALUES ('pr','perm',1,'{}',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO source_adapter_bindings (id,source_id,display_name,created_at)"
        " VALUES ('bnd','src','B',?)", (NOW,)
    )
    c.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id,binding_id,revision,adapter_id,adapter_version,strategy,auth_requirement,execution_class,"
        " permission_profile_id,permission_profile_revision,created_at)"
        " VALUES ('br','bnd',1,'feed','1','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','NONE','HTTP','perm',1,?)",
        (NOW,),
    )
    c.commit()
    run_id, plans = create_run(
        c,
        profile_id=None,
        plans=[{
            "source_id": "src", "source_plan_group_id": "g", "fallback_rank": 0,
            "binding_id": "bnd", "binding_revision_id": "br", "adapter_id": "feed",
            "adapter_version": "1", "adapter_api_version": "1",
            "strategy": "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", "execution_class": "HTTP",
            "permission_profile_id": "perm", "permission_profile_revision": 1,
        }],
        now=NOW,
    )
    request_id, _ = enqueue_request(
        c, run_id=run_id, run_source_plan_id=plans[0], source_id="src", binding_id="bnd",
        request_type="LIST_FETCH", target_identity="https://example.test/jobs", now=NOW,
    )
    return request_id


def test_capacity_deferral_leaves_request_unexecuted_and_preserves_attempt_budget(tmp_path):
    db = Database(tmp_path / "capacity.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    begin_service_epoch(db.conn, now=NOW)
    request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    assert claim and claim.request_id == request_id
    assert db.conn.execute(
        "SELECT attempt_count FROM scrape_requests WHERE id=?", (request_id,)
    ).fetchone()[0] == 1

    assert yield_unstarted_claim(
        db.conn, request_id, claim.attempt_id,
        reason="CAPACITY_UNAVAILABLE", now=NOW,
    )
    row = db.conn.execute(
        "SELECT status,attempt_count,current_attempt_id FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    attempt = db.conn.execute(
        "SELECT outcome,abandoned_reason FROM request_attempts WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()
    assert row["status"] == "PENDING"
    assert row["attempt_count"] == 0
    assert row["current_attempt_id"] is None
    assert attempt["outcome"] == "ABANDONED"
    assert attempt["abandoned_reason"] == "CAPACITY_UNAVAILABLE"
    db.close()


def test_capacity_refund_refuses_already_bound_execution_plan(tmp_path):
    db = Database(tmp_path / "capacity-bound.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    begin_service_epoch(db.conn, now=NOW)
    request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    assert claim and claim.request_id == request_id
    db.conn.execute(
        "UPDATE request_attempts SET execution_plan_id='plan-bound' WHERE attempt_id=?",
        (claim.attempt_id,),
    )
    db.conn.commit()
    assert not yield_unstarted_claim(
        db.conn, request_id, claim.attempt_id,
        reason="CAPACITY_UNAVAILABLE", now=NOW,
    )
    row = db.conn.execute(
        "SELECT status,attempt_count,current_attempt_id FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    assert row["status"] == "RUNNING"
    assert row["attempt_count"] == 1
    assert row["current_attempt_id"] == claim.attempt_id
    db.close()
