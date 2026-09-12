"""Regression probes for the S3.9 run/fallback terminal-work barrier."""

from __future__ import annotations

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import RunStateConflict, aggregate_run, create_run, set_group_outcome

NOW = "2026-09-12T08:00:00.000000Z"
LATER = "2026-09-12T08:01:00.000000Z"
FAR_FUTURE = "2026-09-12T10:00:00.000000Z"


def _db(path) -> Database:
    db = Database(path)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    c = db.conn
    c.execute(
        "INSERT INTO sources(id,display_name,source_family,entry_url,created_at,updated_at)"
        " VALUES('src','fixture','PUBLIC_FEED','https://example.test/jobs',?,?)",
        (NOW, NOW),
    )
    c.execute(
        "INSERT INTO adapter_definitions(adapter_id,adapter_version,adapter_api_version,"
        " manifest_json,created_at) VALUES('fixture','1.0.0','1','{}',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO adapter_permission_profiles(id,display_name,created_at)"
        " VALUES('perm','default',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO adapter_permission_profile_revisions"
        " (id,permission_profile_id,revision,policy_json,created_at)"
        " VALUES('permrev','perm',1,'{}',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO source_adapter_bindings(id,source_id,display_name,created_at)"
        " VALUES('bnd','src','fixture',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id,binding_id,revision,adapter_id,adapter_version,strategy,execution_class,"
        " permission_profile_id,permission_profile_revision,created_at)"
        " VALUES('bndrev','bnd',1,'fixture','1.0.0','HTTP_HTML','HTTP','perm',1,?)",
        (NOW,),
    )
    c.commit()
    return db


def _run(db: Database) -> tuple[str, str]:
    run_id, plan_ids = create_run(
        db.conn,
        profile_id=None,
        plans=[{
            "source_id": "src",
            "source_plan_group_id": "grp",
            "fallback_rank": 0,
            "binding_id": "bnd",
            "binding_revision_id": "bndrev",
            "adapter_id": "fixture",
            "adapter_version": "1.0.0",
            "adapter_api_version": "1",
            "strategy": "HTTP_HTML",
            "execution_class": "HTTP",
            "permission_profile_id": "perm",
            "permission_profile_revision": 1,
        }],
        now=NOW,
    )
    return run_id, plan_ids[0]


def test_future_retry_is_still_open_work_for_terminal_plan_truth(tmp_path):
    db = _db(tmp_path / "future-retry.db")
    run_id, plan_id = _run(db)
    db.conn.execute(
        "INSERT INTO scrape_requests"
        " (id,run_id,run_source_plan_id,source_id,binding_id,request_type,request_unique_key,"
        " status,next_retry_at,created_at,updated_at)"
        " VALUES('retry',?,?, 'src','bnd','LIST_FETCH','retry-key','RETRY_WAIT',?,?,?)",
        (run_id, plan_id, FAR_FUTURE, NOW, NOW),
    )
    db.conn.commit()

    with pytest.raises(RunStateConflict):
        set_group_outcome(db.conn, plan_id, "FAILED", now=LATER)
    db.close()


def test_partial_group_does_not_terminalize_run_around_pending_acquisition(tmp_path):
    db = _db(tmp_path / "partial-pending.db")
    run_id, plan_id = _run(db)
    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src",
        binding_id="bnd",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?page=2",
        logical_key="page-2",
        now=NOW,
    )
    set_group_outcome(db.conn, plan_id, "SATISFIED_PARTIAL", now=LATER)

    assert aggregate_run(db.conn, run_id, now=LATER) is None
    db.close()
