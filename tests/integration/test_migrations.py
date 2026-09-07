"""Integration tests for schema migrations (S0.2)."""

import sqlite3

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    current_schema_version,
    migrate_schema,
    run_database_checks,
)
from jobscraper.version import SCHEMA_VERSION


def test_latest_matches_declared_version():
    assert LATEST_SCHEMA_VERSION == SCHEMA_VERSION


def test_fresh_database_reaches_latest(tmp_path):
    db = Database(tmp_path / "m.db")
    applied = migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    assert applied == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert current_schema_version(db.conn) == LATEST_SCHEMA_VERSION
    db.close()


def test_migrations_run_twice_fresh_database(tmp_path):
    for i in range(2):
        db = Database(tmp_path / f"m{i}.db")
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
        checks = run_database_checks(db.conn)
        assert checks["ok"], checks["problems"]
        assert checks["fts5"] is True
        pragmas = checks["pragmas"]
        assert pragmas["foreign_keys"] == 1
        assert pragmas["journal_mode"] == "WAL"
        assert pragmas["synchronous"] == 2
        db.close()


def test_idempotent_at_target(tmp_path):
    db = Database(tmp_path / "m.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    assert migrate_schema(db.conn, LATEST_SCHEMA_VERSION) == []
    db.close()


def test_downgrade_refused(tmp_path):
    db = Database(tmp_path / "m.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    with pytest.raises(MigrationError):
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION - 1)
    db.close()


def test_unknown_target_refused(tmp_path):
    db = Database(tmp_path / "m.db")
    with pytest.raises(MigrationError):
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION + 1)
    db.close()


def test_partial_then_continue(tmp_path):
    db = Database(tmp_path / "m.db")
    migrate_schema(db.conn, 1)
    assert current_schema_version(db.conn) == 1
    applied = migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    assert applied == list(range(2, LATEST_SCHEMA_VERSION + 1))
    checks = run_database_checks(db.conn)
    assert checks["ok"], checks["problems"]
    db.close()


def test_failed_step_leaves_prior_schema_usable(tmp_path, monkeypatch):
    db = Database(tmp_path / "m.db")
    migrate_schema(db.conn, 1)

    import jobscraper.db.migrations as mig

    original = mig._migration_sql_map()

    def broken_map():
        m = dict(original)
        name, _sql = m[2]
        m[2] = (name, "CREATE TABLE app_meta (x INTEGER);")  # name clash -> fails
        return m

    monkeypatch.setattr(mig, "_migration_sql_map", broken_map)
    with pytest.raises(sqlite3.Error):
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    # Prior schema remains usable and consistent.
    assert current_schema_version(db.conn) == 1
    checks = run_database_checks(db.conn)
    assert checks["ok"], checks["problems"]
    db.close()


REQUIRED_TABLES = {
    "schema_migrations",
    "app_meta",
    "events",
}


def test_slice_boundary_is_version_scoped(tmp_path):
    """The Slice 0 product-table boundary is scoped to schema version 2:
    a database held at the Slice 0 baseline has no product tables; the
    Slice 1 domain model arrives only through migrations >= 3."""
    db = Database(tmp_path / "v2.db")
    migrate_schema(db.conn, 2)
    tables = {
        r[0]
        for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    missing = REQUIRED_TABLES - tables
    assert not missing, f"missing tables: {missing}"
    forbidden = {
        "jobs",
        "sources",
        "job_observations",
        "scrape_requests",
        "job_profile_state",
        "applications",
    }
    assert not (forbidden & tables), "product tables before migration v3"
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    tables = {
        r[0]
        for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    assert forbidden <= tables, "Slice 1 domain tables missing after v3-v9"
    db.close()


def test_events_indexes_exist(tmp_path):
    db = Database(tmp_path / "m.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    indexes = {
        r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"idx_events_at", "idx_events_kind", "idx_events_run"} <= indexes
    db.close()


def test_application_consistency_checks_report_ok(tmp_path):
    db = Database(tmp_path / "m.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    checks = run_database_checks(db.conn)
    assert checks["ok"] is True
    assert "integrity_check" in checks
    db.close()
