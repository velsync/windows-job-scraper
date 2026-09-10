"""Slice 2 S2.0/S2.9: migration verification from the accepted Slice-1 schema.

Slice 2 may only *append* migrations after the accepted Slice-1 corrective
(v10).  This module owns:

* the byte pins for every Slice-2 released step (v11-v14);
* the proof that the released Slice-1 steps v1-v10 are untouched;
* the proof that a populated v10 database migrates forward to
  ``LATEST_SCHEMA_VERSION`` with every pre-existing row preserved, foreign-key
  integrity clean, and the whole chain recorded in ``schema_migrations``;
* the completeness rule that every released Slice-2 step is pinned here.

Later slices append their own separately pinned migrations; their existence
must not make this historical Slice-2 ownership test claim those versions.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    current_schema_version,
    migrate_schema,
)
from jobscraper.db.schema_sql import MIGRATION_STEPS
from jobscraper.version import SCHEMA_VERSION

NOW = "2026-09-08T00:00:00.000000Z"
ACCEPTED_SLICE1_HEAD_VERSION = 10
PROMOTED_SLICE2_HEAD_VERSION = 14

#: Slice-1 released bytes, copied verbatim from
#: ``tests/integration/test_schema_slice1.py`` — Slice 2 must not move them.
SLICE1_RELEASED_STEP_SHA256 = {
    1: "a9d335d680150958aee9d8169034721c24d171696b42c6260052e5c62ba87550",
    2: "22ff8b0feb9bed5e132a5b3d393faee60f5cfcb0df671c00fd1b764dd5e6312e",
    3: "e96853bf636e090f626b8013c23153a5fdbc746e9de02bd93d0b0ad7da5ae1f1",
    4: "2ed4e2c33778dbb320794da4d9e5499a06d103526e804e85e20c9b57a2e0e7f8",
    5: "7e8fbdf2e13b2820017bb27bf55ec9a6fc8e6ed46eaa1315dfb3a6cbf226f77e",
    6: "886c588512ef16dbb5a06c00fc5d11063fabb193315d6f06c3f902eb4d91e9e6",
    7: "2a125e1e573b1d5001f1e742cc628f9432d43fab0fa6da40e670cbb44d1e7333",
    8: "a8401fddd96de6b4e5d1f25724e62fa1148ccc616fec488e3f711502f59f0ceb",
    9: "2085d628491a5cac0044f9195c0c3c43fa5dce4927b43d2db0ba567aae5455b0",
    10: "053df479f2c0f1dcf431b73e38aa000c2fa8878b630552c528787759534f5136",
}

#: Slice-2 released steps (v11-v14). Each Slice-2 package appended its own pin.
RELEASED_STEP_SHA256: dict[int, str] = {
    11: "dc3a29389a35b1e24396ac9d662cd6c6dfd460341d95125a3171a504f65e8ddb",
    12: "912888da60343657e561504d7458e9e45707029a3c8e67399f2157f1b9faef60",
    13: "bc4c4da5fadfb9a9a1f4d99a3153cc03e1351c10378ab06568b3e222e5d0d24f",
    14: "eeffc8fe0178518b9b8129fe7739579f94084db3fe72c4389fbfab5146c0f18f",
}


def _digest(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


# ------------------------------------------------- append-only discipline


def test_slice1_released_bytes_are_untouched():
    for version, pinned in SLICE1_RELEASED_STEP_SHA256.items():
        sql = next(sql for v, _name, sql in MIGRATION_STEPS if v == version)
        assert _digest(sql) == pinned, f"released step {version} was edited"


def test_every_slice2_step_is_pinned_exactly_once():
    slice2_released = {
        version
        for version, _name, _sql in MIGRATION_STEPS
        if ACCEPTED_SLICE1_HEAD_VERSION < version <= PROMOTED_SLICE2_HEAD_VERSION
    }
    pinned_here = set(RELEASED_STEP_SHA256)
    pinned_slice1 = set(SLICE1_RELEASED_STEP_SHA256)
    assert pinned_here.isdisjoint(pinned_slice1), "a step is pinned in two places"
    assert slice2_released == pinned_here, (
        f"unpinned Slice-2 steps: {sorted(slice2_released - pinned_here)}"
    )
    # no Slice-1 step is re-pinned here with a different digest
    for version, pinned in SLICE1_RELEASED_STEP_SHA256.items():
        assert RELEASED_STEP_SHA256.get(version, pinned) == pinned


def test_slice2_released_bytes_are_pinned_by_digest():
    """Every appended Slice-2 step is digest-pinned, not just keyed.

    Key presence alone would let a committed migration be edited in place
    (03 §50 append-only).  A deliberate change to a step already committed in
    this slice means updating its digest here *and* in the review record.
    """
    for version, pinned in RELEASED_STEP_SHA256.items():
        sql = next(sql for v, _name, sql in MIGRATION_STEPS if v == version)
        assert _digest(sql) == pinned, f"Slice-2 step {version} was edited"


def test_schema_version_matches_the_latest_step():
    assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION == len(MIGRATION_STEPS)


def test_slice2_steps_add_no_later_slice_objects():
    """Each appended Slice-2 step stays inside ROAD-03 ownership."""
    appended = [
        (v, sql)
        for v, _n, sql in MIGRATION_STEPS
        if ACCEPTED_SLICE1_HEAD_VERSION < v <= PROMOTED_SLICE2_HEAD_VERSION
    ]
    for step_index, sql in appended:
        _assert_no_later_slice_objects(step_index, sql)


def _assert_no_later_slice_objects(step_index: int, sql: str) -> None:
    for forbidden in (
        "recipes",
        "recipe_versions",
        "navigation_plans",
        "source_fixtures",
        "locator_health",
        "job_merges",
        "contacts",
        "fx_rates",
        "user_feedback",
        "snapshots",
        "egress_profiles",
    ):
        assert f"CREATE TABLE {forbidden}" not in sql, (step_index, forbidden)


# ------------------------------------------- v10 → latest forward migration


def _seed_v10_family(conn: sqlite3.Connection) -> None:
    """One coherent, populated family across the accepted Slice-1 spine."""
    rows = [
        (
            "sources",
            dict(
                id="src-1", display_name="Feed", source_family="PUBLIC_FEED",
                entry_url="https://example.test/jobs.json", created_at=NOW, updated_at=NOW,
            ),
        ),
        (
            "adapter_definitions",
            dict(adapter_id="json_api_feed", adapter_version="1.0.0", adapter_api_version="1",
                 manifest_json="{}", created_at=NOW),
        ),
        ("adapter_permission_profiles", dict(id="perm-1", display_name="default", created_at=NOW)),
        (
            "adapter_permission_profile_revisions",
            dict(id="permrev-1", permission_profile_id="perm-1", revision=1,
                 policy_json="{}", created_at=NOW),
        ),
        ("source_adapter_bindings", dict(id="bnd-1", source_id="src-1", display_name="api",
                                         created_at=NOW)),
        (
            "companies",
            dict(id="co-1", name="Fixture Corp", normalized_name="fixture corp",
                 first_seen_at=NOW, created_at=NOW, updated_at=NOW),
        ),
        (
            "jobs",
            dict(id="job-1", company_id="co-1", title="Backend Engineer",
                 normalized_title="backend engineer", description_text="python services",
                 discovered_at=NOW, first_seen_at=NOW, last_seen_at=NOW,
                 created_at=NOW, updated_at=NOW),
        ),
        ("scrape_runs", dict(id="run-1", created_at=NOW)),
    ]
    for table, cols in rows:
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id,"
        " adapter_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at) VALUES"
        " ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0',"
        "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO run_source_plans (id, run_id, source_id, source_plan_group_id,"
        " fallback_rank, binding_id, binding_revision_id, adapter_id, adapter_version,"
        " adapter_api_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at) VALUES"
        " ('rsp-1','run-1','src-1','grp-1',0,'bnd-1','bndrev-1','json_api_feed','1.0.0',"
        "'1','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO scrape_requests (id, run_id, run_source_plan_id, source_id,"
        " binding_id, request_type, request_unique_key, created_at, updated_at)"
        " VALUES ('req-1','run-1','rsp-1','src-1','bnd-1','LIST_FETCH','rk-1',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO request_attempts (attempt_id, request_id, started_at, created_at)"
        " VALUES ('att-1','req-1',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO job_observations (id, run_id, request_id, attempt_id, source_id,"
        " binding_id, adapter_id, adapter_version, strategy, execution_class,"
        " observed_at, observation_unique_key, source_job_id)"
        " VALUES ('obs-1','run-1','req-1','att-1','src-1','bnd-1','json_api_feed','1.0.0',"
        "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP',?,'ouk-1','fx-100')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO field_evidence (id, observation_id, field_name, created_at)"
        " VALUES ('fe-1','obs-1','title',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO job_locations (id, job_id, raw_text, city, remote)"
        " VALUES ('jl-1','job-1','Berlin, Germany','Berlin',0)",
    )
    conn.execute(
        "INSERT INTO job_sources (id, job_id, source_id, binding_id, source_job_id,"
        " discovery_url, raw_source_url, canonical_job_url, application_url,"
        " first_seen_at, last_seen_at, created_at, updated_at)"
        " VALUES ('js-1','job-1','src-1','bnd-1','fx-100',?,?,?,?,?,?,?,?)",
        (
            "http://127.0.0.1:1/jobs?page=1", "http://127.0.0.1:1/jobs?page=1",
            "https://example.test/jobs/fx-100", "https://example.test/jobs/fx-100/apply",
            NOW, NOW, NOW, NOW,
        ),
    )
    conn.commit()


def test_v10_migrates_forward_with_rows_preserved(tmp_path):
    db = Database(tmp_path / "from-v10.db")
    migrate_schema(db.conn, ACCEPTED_SLICE1_HEAD_VERSION)
    assert current_schema_version(db.conn) == ACCEPTED_SLICE1_HEAD_VERSION
    _seed_v10_family(db.conn)

    applied = migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    conn = db.conn
    assert applied == list(range(ACCEPTED_SLICE1_HEAD_VERSION + 1, LATEST_SCHEMA_VERSION + 1))
    assert current_schema_version(conn) == LATEST_SCHEMA_VERSION == SCHEMA_VERSION

    # every pre-existing row survived the appended steps
    for table, expected in (
        ("sources", 1),
        ("adapter_definitions", 1),
        ("source_adapter_bindings", 1),
        ("source_adapter_binding_revisions", 1),
        ("companies", 1),
        ("jobs", 1),
        ("job_locations", 1),
        ("job_sources", 1),
        ("job_observations", 1),
        ("field_evidence", 1),
        ("scrape_requests", 1),
        ("request_attempts", 1),
        # v13 search bookkeeping tables exist and are empty after the append
        ("job_search_docs", 0),
        ("job_search_state", 0),
        ("search_capability", 0),
        # v14 S2.4 evidence tables are empty after the append
        ("ats_fingerprints", 0),
        ("source_route_decisions", 0),
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected, table
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # the Slice-1 invariants still hold on the migrated database
    job = conn.execute("SELECT id, company_id FROM jobs").fetchone()
    assert job["company_id"] == "co-1"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_sources (id, job_id, source_id, binding_id, source_job_id,"
            " first_seen_at, last_seen_at, created_at, updated_at)"
            " VALUES ('js-2','job-1','src-1','bnd-1','fx-100',?,?,?,?)",
            (NOW, NOW, NOW, NOW),
        )
    db.close()


def test_v13_steps_record_cleaning_version_and_search_tables(tmp_path):
    """S2.3 append-only step: additive columns/tables, no FTS5 DDL, no
    later-slice objects, and the v12 rows survive untouched."""
    from jobscraper.db.schema_sql import MIGRATION_STEPS

    # v13 is no longer the last step (v14 was appended for S2.4), so look it
    # up by version number rather than assuming MIGRATION_STEPS[-1].
    v13_entry = next(e for e in MIGRATION_STEPS if e[0] == 13)
    version, name, sql = v13_entry
    assert version == 13
    assert name == "s2_3_content_cleaning_and_search_docs"
    # plain bookkeeping: the FTS5 virtual table is provisioned by capability-
    # gated code in jobscraper.search, never by an unconditional step
    assert "CREATE VIRTUAL TABLE" not in sql
    for forbidden in ("DELETE", "DROP"):
        assert forbidden not in sql
    db = Database(tmp_path / "v13.db")
    migrate_schema(db.conn, 12)
    conn = db.conn
    before = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert migrate_schema(conn, 13) == [13]
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == before + 1
    # the new columns/tables exist and the older rows are untouched
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "content_cleaning_version" in cols
    for table in ("job_search_docs", "job_search_state", "search_capability"):
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone() is not None, table
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_migration_from_v10_is_idempotent_and_refuses_downgrade(tmp_path):
    db = Database(tmp_path / "idempotent.db")
    migrate_schema(db.conn, ACCEPTED_SLICE1_HEAD_VERSION)
    assert migrate_schema(db.conn, LATEST_SCHEMA_VERSION) == list(
        range(ACCEPTED_SLICE1_HEAD_VERSION + 1, LATEST_SCHEMA_VERSION + 1)
    )
    assert migrate_schema(db.conn, LATEST_SCHEMA_VERSION) == []
    with pytest.raises(MigrationError):
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION - 1)
    db.close()
