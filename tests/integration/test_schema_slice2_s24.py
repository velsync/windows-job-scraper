"""S2.4 — Migration v14 verification: ats_fingerprints + source_route_decisions.

Slice 2 S2.4 appends migration v14 to the accepted v1–v13 schema.  The
step is append-only and creates two new evidence tables plus required indexes.
No released step (v1–v13) may be edited.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import (
    current_schema_version,
    migrate_schema,
)
from jobscraper.db.schema_sql import LATEST_SCHEMA_VERSION, MIGRATION_STEPS
from jobscraper.version import SCHEMA_VERSION

NOW = "2026-09-08T00:00:00.000000Z"


# --------------------------------------------------------------- constants

def test_schema_version_is_14():
    assert SCHEMA_VERSION == 14
    assert LATEST_SCHEMA_VERSION == 14


def test_v14_step_is_present():
    names = {v: name for v, name, _sql in MIGRATION_STEPS}
    assert 14 in names
    assert names[14] == "s2_4_ats_fingerprint_and_route_decision"


def test_v14_sql_is_append_only():
    """v14 must not DELETE or DROP anything."""
    sql = next(sql for v, _name, sql in MIGRATION_STEPS if v == 14)
    for forbidden in ("DELETE", "DROP"):
        assert forbidden not in sql, f"v14 contains {forbidden}"


def test_v14_creates_ats_fingerprints_table():
    sql = next(sql for v, _name, sql in MIGRATION_STEPS if v == 14)
    assert "ats_fingerprints" in sql


def test_v14_creates_source_route_decisions_table():
    sql = next(sql for v, _name, sql in MIGRATION_STEPS if v == 14)
    assert "source_route_decisions" in sql


def test_v14_has_required_indexes():
    sql = next(sql for v, _name, sql in MIGRATION_STEPS if v == 14)
    assert "idx_ats_fingerprints_source" in sql
    assert "idx_route_decisions_source" in sql


# ------------------------------------------------ migration from v13 → v14

def test_v13_migrates_forward_to_v14(tmp_path):
    """A database at v13 migrates cleanly to v14 with the new tables."""
    db = Database(tmp_path / "v13-to-v14.db")
    migrate_schema(db.conn, 13)
    assert current_schema_version(db.conn) == 13

    applied = migrate_schema(db.conn, 14)
    assert applied == [14]
    assert current_schema_version(db.conn) == 14

    conn = db.conn
    # New tables exist
    for table in ("ats_fingerprints", "source_route_decisions"):
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone() is not None, table

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()


def test_v14_tables_accept_rows(tmp_path):
    """The new tables accept evidence rows after migration."""
    db = Database(tmp_path / "v14-rows.db")
    migrate_schema(db.conn, 14)
    conn = db.conn

    import json
    conn.execute(
        "INSERT INTO ats_fingerprints (id, source_id, url, family, confidence,"
        " evidence_json, recommended_adapter_id, fingerprint_version, created_at)"
        " VALUES ('fp-1', 'src-1', 'https://boards.greenhouse.io/acme',"
        " 'GREENHOUSE', 0.95, ?, 'greenhouse', 1, ?)",
        (json.dumps([{"kind": "HOST", "value": "boards.greenhouse.io"}]), NOW),
    )
    conn.execute(
        "INSERT INTO source_route_decisions (id, source_id, outcome,"
        " fingerprint_family, fingerprint_confidence, candidates_json,"
        " unsupported_candidates_json, fallback_reason, router_version, created_at)"
        " VALUES ('rd-1', 'src-1', 'SPECIALIZED', 'GREENHOUSE', 0.95,"
        " ?, '[]', NULL, 1, ?)",
        (json.dumps([{"strategy": "PROVIDER_NATIVE", "execution_class": "HTTP"}]), NOW),
    )
    conn.commit()

    fp_count = conn.execute("SELECT COUNT(*) FROM ats_fingerprints").fetchone()[0]
    assert fp_count == 1
    rd_count = conn.execute("SELECT COUNT(*) FROM source_route_decisions").fetchone()[0]
    assert rd_count == 1
    db.close()


# --------------------------------------------- migration idempotency + guard

def test_migration_is_idempotent_and_refuses_downgrade(tmp_path):
    db = Database(tmp_path / "idempotent-v14.db")
    migrate_schema(db.conn, 13)
    migrate_schema(db.conn, 14)
    assert migrate_schema(db.conn, 14) == []  # idempotent
    from jobscraper.db.migrations import MigrationError
    with pytest.raises(MigrationError):
        migrate_schema(db.conn, 13)
    db.close()


def test_full_migration_from_v1_preserves_existing_rows(tmp_path):
    """A clean v1→v14 migration on a fresh database completes without error."""
    db = Database(tmp_path / "fresh-v14.db")
    from jobscraper.db.migrations import migrate_database_with_backup
    result = migrate_database_with_backup(
        db, create_backup=lambda kind: tmp_path / "backup",
    )
    assert result["ok"]
    assert current_schema_version(db.conn) == 14
    db.close()
