from __future__ import annotations

from dataclasses import dataclass

from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.acquisition.crawler.revalidation import (
    bind_fetch_representation,
    prepare_revalidation,
    resolve_304,
    restore_membership,
    store_representation,
    touch_representation,
)
from jobscraper.db.connection import Database
from jobscraper.db.migrations import migrate_schema
from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.runs import create_run
from jobscraper.version import SCHEMA_VERSION


@dataclass(frozen=True)
class Obs:
    source_job_id: str | None
    raw_url: str | None = None


def _setup(conn):
    now = db_utc_now(conn)
    conn.execute(
        "INSERT INTO sources(id,display_name,source_family,entry_url,created_at,updated_at)"
        " VALUES ('src','Fixture','CAREERS','https://jobs.example.test',?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO adapter_definitions(adapter_id,adapter_version,adapter_api_version,"
        "manifest_json,created_at) VALUES ('fixture','1','1','{}',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles(id,display_name,created_at)"
        " VALUES ('perm','fixture',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions(id,permission_profile_id,"
        "revision,policy_json,created_at) VALUES ('permrev','perm',1,'{}',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO source_adapter_bindings(id,source_id,display_name,created_at)"
        " VALUES ('bnd','src','fixture',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions(id,binding_id,revision,adapter_id,"
        "adapter_version,strategy,config_json,auth_requirement,execution_class,"
        "permission_profile_id,permission_profile_revision,created_at)"
        " VALUES ('bndrev','bnd',1,'fixture','1','HTTP_HTML','{}','NONE','HTTP','perm',1,?)",
        (now,),
    )
    conn.commit()
    run_id, plans = create_run(
        conn,
        profile_id=None,
        plans=[{
            "source_id": "src",
            "source_plan_group_id": "grp",
            "fallback_rank": 0,
            "binding_id": "bnd",
            "binding_revision_id": "bndrev",
            "adapter_id": "fixture",
            "adapter_version": "1",
            "adapter_api_version": "1",
            "strategy": "HTTP_HTML",
            "execution_class": "HTTP",
            "permission_profile_id": "perm",
            "permission_profile_revision": 1,
            "cursor_schema_version": 1,
            "crawl_policy_snapshot_json": {},
        }],
        now=now,
    )
    plan = conn.execute("SELECT * FROM run_source_plans WHERE id=?", (plans[0],)).fetchone()
    return now, run_id, plan


def _request():
    return RequestPlan(
        method="GET",
        url="https://jobs.example.test/jobs",
        headers={"Accept": "application/json"},
        expected_content_types=("application/json",),
        purpose="LIST_FETCH",
    )


def test_retained_membership_survives_restart_and_304_reuse_restores_seen_union(tmp_path):
    path = tmp_path / "s37-restart.db"
    db = Database(path)
    migrate_schema(db.conn, SCHEMA_VERSION)
    now, run_id, plan = _setup(db.conn)
    rep = store_representation(
        db.conn,
        plan_row=plan,
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b'{"jobs":[{"id":"A"},{"id":"B"}]}',
        body_hash="1" * 64,
        normalized_content_hash="2" * 64,
        content_type="application/json",
        response_headers={"ETag": '"list-v1"'},
        observations=(Obs("A", "https://jobs.example.test/A"), Obs("B", "https://jobs.example.test/B")),
        membership_complete=True,
        normalization_version=1,
        now=now,
    )
    db.close()

    # Simulated service restart: compatibility and membership live only in DB.
    db = Database(path)
    try:
        plan = db.conn.execute("SELECT * FROM run_source_plans LIMIT 1").fetchone()
        now = db_utc_now(db.conn)
        # A durable attempt row is sufficient for the retention hold owner.
        request_id = "req-revalidation"
        db.conn.execute(
            "INSERT INTO scrape_requests(id,run_id,run_source_plan_id,source_id,binding_id,"
            "request_type,request_unique_key,payload_json,status,max_attempts,created_at,updated_at)"
            " VALUES (?,?,?,?,?,'LIST_FETCH','rk','{}','RUNNING',3,?,?)",
            (request_id, plan["run_id"], plan["id"], plan["source_id"], plan["binding_id"], now, now),
        )
        db.conn.execute(
            "INSERT INTO request_attempts(attempt_id,request_id,started_at,outcome,created_at)"
            " VALUES ('att304',?,?,NULL,?)",
            (request_id, now, now),
        )
        coverage_id = "cov304"
        db.conn.execute(
            "INSERT INTO enumeration_coverage(id,run_source_plan_id,source_id,binding_id,"
            "scope_key,generation_key,coverage_authority,absence_inference_allowed,started_at,created_at)"
            " VALUES (?,?,?,?,?,'g','AUTHORITATIVE_FULL_SOURCE',1,?,?)",
            (coverage_id, plan["id"], plan["source_id"], plan["binding_id"], "all", now, now),
        )
        db.conn.commit()

        prep = prepare_revalidation(
            db.conn,
            plan_row=plan,
            request_plan=_request(),
            attempt_id="att304",
            expected_page_classes=("VALID_LIST", "EMPTY"),
            require_membership=True,
            normalization_version=1,
            now=now,
        )
        assert prep.conditional is True
        reuse = resolve_304(
            db.conn,
            preparation=prep,
            plan_row=plan,
            request_plan=prep.request_plan,
            expected_page_classes=("VALID_LIST", "EMPTY"),
            require_membership=True,
            normalization_version=1,
        )
        assert reuse.accepted is True
        assert {m.stable_source_identity for m in reuse.membership} == {"A", "B"}
        restored = restore_membership(
            db.conn,
            representation_id=rep,
            coverage_id=coverage_id,
            plan_row=plan,
            now=now,
        )
        assert restored == 2
        seen = {
            row[0]
            for row in db.conn.execute(
                "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id=?",
                (coverage_id,),
            )
        }
        assert seen == {"A", "B"}
    finally:
        db.close()


def test_v17_cache_tables_and_fetch_reference_are_foreign_key_clean(tmp_path):
    db = Database(tmp_path / "s37-schema.db")
    try:
        migrate_schema(db.conn, SCHEMA_VERSION)
        tables = {
            row[0]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "cache_representation",
            "cache_representation_membership",
            "cache_representation_hold",
        } <= tables
        columns = {
            row[1] for row in db.conn.execute("PRAGMA table_info(fetch_attempts)")
        }
        assert "cache_representation_id" in columns
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
