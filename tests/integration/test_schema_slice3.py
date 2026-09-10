"""Slice 3 S3.0 schema gate — append-only v15 runtime foundation.

The promoted Slice-2 schema ends at v14. S3.0 appends the first Slice-3
runtime-foundation migration and must not pre-create crawler/coverage/fallback
schema owned by later S3 packages. Later packages may append new versions but
must never edit v15.
"""

from __future__ import annotations

import hashlib
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

    # R2-F1: do not steal later S3/future-slice schema into v15.
    for forbidden in (
        "CREATE TABLE cache_representation",
        "CREATE TABLE coverage_scope_membership",
        "CREATE TABLE source_plan_group_state",
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
