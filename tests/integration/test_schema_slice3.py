"""Slice 3 S3.0 schema gate — append-only v15 runtime foundation.

The promoted Slice-2 schema ends at v14. S3.0 appends the first Slice-3
runtime-foundation migration and must not pre-create crawler/coverage schema
owned by later S3 packages, including S3.9 fallback/group state (R2-F1).
The Batch-A audit corrects the unpromoted v15; later packages append versions.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import (
    LATEST_SCHEMA_VERSION,
    current_schema_version,
    migrate_database_with_backup,
    migrate_schema,
)
from jobscraper.db.schema_sql import MIGRATION_STEPS
from jobscraper.version import SCHEMA_VERSION

PROMOTED_SLICE2_SCHEMA_VERSION = 14
NOW = "2026-09-10T15:00:00.000000Z"

# Exact released bytes through the promoted Slice-2 candidate. S3.0 must not
# edit any of them; future Slice-3 packages append new versions instead.
PROMOTED_STEP_SHA256 = {
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
    11: "dc3a29389a35b1e24396ac9d662cd6c6dfd460341d95125a3171a504f65e8ddb",
    12: "912888da60343657e561504d7458e9e45707029a3c8e67399f2157f1b9faef60",
    13: "bc4c4da5fadfb9a9a1f4d99a3153cc03e1351c10378ab06568b3e222e5d0d24f",
    14: "eeffc8fe0178518b9b8129fe7739579f94084db3fe72c4389fbfab5146c0f18f",
}

# S3.0 freezes the first Slice-3 migration at package completion. This digest
# is over schema_sql.MIGRATION_STEPS' stripped SQL text.
S3_0_STEP_SHA256 = {
    15: "1848e82890da89863de913cee74a039485bf0939ba4536b607e2f94b58a902bc",
}

# S3.5 freezes the binding-revision cursor rebuild. v1-v15 remain immutable.
S3_5_STEP_SHA256 = {
    16: "176f8fac243b988bd5bd1180048ec8ee897184a3ceb741f6a7b6ffd2d397acd0",
}

# S3.7 appends the next unused migration per R2-F1. v16 stays byte-frozen.
S3_7_STEP_SHA256 = {
    17: "485d0f05caf5b9b85231e7d5aa9e55890183ee735d0c015d78b0e4507179ed31",
}


def _digest(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


def _step(version: int) -> tuple[str, str]:
    return next((name, sql) for v, name, sql in MIGRATION_STEPS if v == version)


def test_promoted_v1_through_v14_bytes_are_untouched():
    assert set(PROMOTED_STEP_SHA256) == set(range(1, 15))
    for version, pinned in PROMOTED_STEP_SHA256.items():
        _name, sql = _step(version)
        assert _digest(sql) == pinned, f"promoted migration {version} was edited"


def test_s30_v15_is_sequential_and_pinned():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions == list(range(1, max(versions) + 1))
    assert 15 in versions
    assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION == max(versions)
    name, sql = _step(15)
    assert name == "s3_0_runtime_foundation"
    assert _digest(sql) == S3_0_STEP_SHA256[15]


def test_s35_v16_is_sequential_and_pinned():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:16] == list(range(1, 17))
    name, sql = _step(16)
    assert name == "s3_5_binding_revision_crawl_cursor"
    assert _digest(sql) == S3_5_STEP_SHA256[16]
    # Later packages append only; v15/v16 remain exact accepted bytes.
    assert _digest(_step(15)[1]) == S3_0_STEP_SHA256[15]


def test_s37_v17_is_frozen_after_later_slice3_migrations():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:17] == list(range(1, 18))
    name, sql = _step(17)
    assert name == "s3_7_revalidation_cache"
    assert _digest(sql) == S3_7_STEP_SHA256[17]
    assert _digest(_step(16)[1]) == S3_5_STEP_SHA256[16]


def test_s38_v18_is_frozen_after_s39_append():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:18] == list(range(1, 19))
    name, sql = _step(18)
    assert name == "s3_8_coverage_authority_and_scope_membership"
    assert _digest(sql) == "6075470c2667d581472d8c47ef1baaa8e30fda87d05dbfe996c2c948d4e2c3cf"
    assert _digest(_step(17)[1]) == S3_7_STEP_SHA256[17]


def test_s39_v19_is_frozen_after_s310_append():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:19] == list(range(1, 20))
    name, sql = _step(19)
    assert name == "s3_9_logical_fallback_group_state"
    assert _digest(sql) == "e7028ec8fd11ba751bfc3a3e0145d3f1df31f34bb257c38f6fa00979f671ff68"
    assert _digest(_step(18)[1]) == "6075470c2667d581472d8c47ef1baaa8e30fda87d05dbfe996c2c948d4e2c3cf"


def test_s310_v20_is_next_unused_sequential_and_pinned():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions == list(range(1, 21))
    assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION == 20
    name, sql = _step(20)
    assert name == "s3_10_presence_availability_order"
    assert _digest(sql) == "bd4c690eab18938604f49fd894c4b25c2cdda1a29f62d14d6c678f211faf6c9b"
    assert _digest(_step(19)[1]) == "e7028ec8fd11ba751bfc3a3e0145d3f1df31f34bb257c38f6fa00979f671ff68"
    assert "coverage_presence_application" in sql
    assert "last_absence_coverage_id IS NOT NULL" in sql
    assert "THEN 'LEGACY_ACTIVE'" in sql

    # The db/schema_sql.py migration owner is allowed to backfill canonical
    # projections during an exclusive upgrade, but v20 must touch only the five
    # availability-order columns it adds. This keeps the runtime single-writer
    # contract strong rather than granting migrations an unbounded exception.
    update = sql.split("UPDATE job_sources", 1)[1].split("CREATE INDEX", 1)[0]
    assigned = set(
        re.findall(r"(?m)^\s*(?:SET\s+)?([a-z_][a-z0-9_]*)\s*=", update)
    )
    assert assigned == {
        "availability_effective_at",
        "availability_received_at",
        "availability_evidence_kind",
        "availability_evidence_ref",
        "availability_revision",
    }


def test_s310_v20_adds_only_presence_order_metadata(tmp_path):
    db = Database(tmp_path / "s310-v20.db")
    try:
        migrate_schema(db.conn, 19)
        before = {
            row[1] for row in db.conn.execute("PRAGMA table_info(job_sources)")
        }
        migrate_schema(db.conn, 20)
        after = {
            row[1] for row in db.conn.execute("PRAGMA table_info(job_sources)")
        }
        assert after - before == {
            "availability_effective_at",
            "availability_received_at",
            "availability_evidence_kind",
            "availability_evidence_ref",
            "availability_revision",
        }
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()

def test_v16_rebuild_preserves_legacy_cursor_without_guessing_provenance(tmp_path):
    db = Database(tmp_path / "cursor-v16.db")
    try:
        migrate_schema(db.conn, 15)
        db.conn.execute(
            "INSERT INTO sources(id,display_name,source_family,entry_url,created_at,updated_at)"
            " VALUES ('src','Fixture','CAREERS','https://jobs.example.test',?,?)",
            (NOW, NOW),
        )
        db.conn.execute(
            "INSERT INTO source_adapter_bindings(id,source_id,display_name,created_at)"
            " VALUES ('bnd','src','fixture',?)", (NOW,),
        )
        db.conn.execute(
            "INSERT INTO crawl_cursors(id,source_id,binding_id,adapter_id,adapter_version,"
            " cursor_schema_version,state_json,checkpoint_at)"
            " VALUES ('cur-old','src','bnd','fixture','1.0.0',1,'{\"page\":2}',?)",
            (NOW,),
        )
        db.conn.commit()
        migrate_schema(db.conn, 16)
        row = db.conn.execute("SELECT * FROM crawl_cursors WHERE id='cur-old'").fetchone()
        assert row["binding_revision_id"] is None
        assert row["checkpoint_run_source_plan_id"] is None
        assert row["state_json"] == '{"page":2}'
        assert row["guard_state_json"] == '{}'
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_v16_shares_one_compatible_cursor_row_with_plan_provenance(tmp_path):
    db = Database(tmp_path / "cursor-identity-v16.db")
    try:
        migrate_schema(db.conn, 16)
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(crawl_cursors)")}
        assert {"binding_revision_id", "guard_state_json", "checkpoint_run_source_plan_id"} <= columns
        # Partial unique index exists on the compatible lookup identity:
        # legacy NULL rows may coexist, while S3.5 rows share exactly one
        # cursor per compatible binding revision + code pins.
        indexes = db.conn.execute("PRAGMA index_list(crawl_cursors)").fetchall()
        assert any(row[1] == "idx_crawl_cursors_compatible_identity" and row[2] == 1 for row in indexes)
    finally:
        db.close()


def test_v15_contains_only_s31_to_s34_runtime_foundation_storage():
    _name, sql = _step(15)
    assert "CREATE TABLE service_clock_epochs" in sql
    assert "ALTER TABLE request_attempts ADD COLUMN service_epoch_id" in sql
    assert "CREATE TABLE binding_host_rate_state" in sql
    for required in (
        "circuit_state",
        "cooldown_until",
        "recent_failure_count",
        "recent_success_count",
        "last_retry_after",
        "last_rate_event_at",
        "last_success_at",
        "egress_identity",
    ):
        assert required in sql

    # R2-F1: do not steal crawler/coverage/future-slice schema into v15.
    for forbidden in (
        "source_plan_group_state",
        "active_fallback_rank",
        "group_outcome",
        "CREATE TABLE cache_representation",
        "CREATE TABLE coverage_scope_membership",
        "CREATE TABLE recipes",
        "CREATE TABLE recipe_versions",
        "CREATE TABLE navigation_plans",
        "CREATE TABLE contacts",
        "CREATE TABLE fx_rates",
        "CREATE TABLE user_feedback",
        "ALTER TABLE enumeration_coverage",
        "ALTER TABLE job_sources",
        "ALTER TABLE job_eligibility",
        "ALTER TABLE job_scores",
    ):
        assert forbidden not in sql, forbidden


def test_promoted_v14_migrates_through_backup_gate_with_existing_rows_preserved(tmp_path):
    db = Database(tmp_path / "from-v14.db")
    migrate_schema(db.conn, PROMOTED_SLICE2_SCHEMA_VERSION)
    db.conn.execute(
        "INSERT INTO events(at, level, kind, message) VALUES (?, 'INFO', 'S3_FIXTURE', 'keep me')",
        (NOW,),
    )
    db.conn.commit()
    assert current_schema_version(db.conn) == 14

    calls: list[str] = []

    def create_backup(*, kind: str):
        calls.append(kind)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir(exist_ok=True)
        return backup_dir

    report = migrate_database_with_backup(
        db,
        create_backup=create_backup,
        target_version=15,
    )
    assert calls == ["PRE_MIGRATION"]
    assert report["applied"] == [15]
    assert report["ok"] is True
    assert current_schema_version(db.conn) == 15
    assert db.conn.execute(
        "SELECT message FROM events WHERE kind='S3_FIXTURE'"
    ).fetchone()[0] == "keep me"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_v15_service_epoch_is_durable_and_request_attempts_reference_it(tmp_path):
    db = Database(tmp_path / "epoch.db")
    migrate_schema(db.conn, 15)
    tables = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "service_clock_epochs" in tables
    columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(request_attempts)").fetchall()
    }
    assert "service_epoch_id" in columns
    fks = db.conn.execute("PRAGMA foreign_key_list(request_attempts)").fetchall()
    assert any(
        row[2] == "service_clock_epochs" and row[3] == "service_epoch_id"
        for row in fks
    )
    db.close()


def test_v15_rate_state_identity_is_binding_host_and_optional_egress(tmp_path):
    db = Database(tmp_path / "rate.db")
    migrate_schema(db.conn, 15)
    db.conn.execute(
        "INSERT INTO sources(id, display_name, source_family, entry_url, created_at, updated_at)"
        " VALUES ('src-1','Fixture','CAREERS','https://jobs.example.test',?,?)",
        (NOW, NOW),
    )
    db.conn.execute(
        "INSERT INTO source_adapter_bindings(id, source_id, display_name, created_at)"
        " VALUES ('bnd-1','src-1','fixture',?)",
        (NOW,),
    )
    db.conn.execute(
        "INSERT INTO binding_host_rate_state(id, binding_id, host, updated_at)"
        " VALUES ('rate-1','bnd-1','jobs.example.test',?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO binding_host_rate_state(id, binding_id, host, updated_at)"
            " VALUES ('rate-2','bnd-1','jobs.example.test',?)",
            (NOW,),
        )
    db.conn.execute(
        "INSERT INTO binding_host_rate_state(id, binding_id, host, egress_identity, updated_at)"
        " VALUES ('rate-3','bnd-1','jobs.example.test','egress-approved-1',?)",
        (NOW,),
    )
    row = db.conn.execute(
        "SELECT circuit_state, recent_failure_count, recent_success_count"
        " FROM binding_host_rate_state WHERE id='rate-1'"
    ).fetchone()
    assert tuple(row) == ("CLOSED", 0, 0)
    db.close()


def test_v15_does_not_precreate_s39_group_storage(tmp_path):
    db = Database(tmp_path / "no-group-state.db")
    try:
        migrate_schema(db.conn, 14)
        before = {row[0] for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        migrate_schema(db.conn, 15)
        after = {row[0] for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert after - before == {"service_clock_epochs", "binding_host_rate_state"}
        assert "source_plan_group_state" not in after
    finally:
        db.close()
