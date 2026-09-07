"""Tests for SQLite connection/PRAGMA enforcement (S0.2)."""

import sqlite3

import pytest

from jobscraper.db.connection import (
    Database,
    connect_db,
    fts5_available,
    immediate_transaction,
    read_sqlite_settings,
    verify_sqlite_settings,
)


def test_connect_applies_and_reports_baseline(tmp_path):
    conn = connect_db(tmp_path / "a.db")
    settings = read_sqlite_settings(conn)
    assert settings.foreign_keys == 1
    assert settings.journal_mode == "WAL"
    assert settings.synchronous == 2  # FULL
    assert settings.busy_timeout_ms == 5000
    verify_sqlite_settings(conn)  # must not raise
    conn.close()


def test_effective_pragmas_observed_not_configured(tmp_path):
    conn = connect_db(tmp_path / "b.db")
    # Effective value must come from the database engine, not defaults.
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    conn.close()


def test_fts5_available(tmp_path):
    conn = connect_db(tmp_path / "c.db")
    assert fts5_available(conn) is True
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    conn.execute("INSERT INTO t VALUES ('hello world')")
    assert conn.execute("SELECT count(*) FROM t WHERE t MATCH 'hello'").fetchone()[0] == 1
    conn.close()


def test_immediate_transaction_rollback(tmp_path):
    conn = connect_db(tmp_path / "d.db")
    conn.execute("CREATE TABLE t (x INTEGER)")
    try:
        with immediate_transaction(conn) as tx:
            tx.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    conn.close()


def test_foreign_keys_enforced(tmp_path):
    conn = connect_db(tmp_path / "e.db")
    conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE child (id TEXT PRIMARY KEY, p TEXT REFERENCES parent(id))")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO child VALUES ('c1', 'missing')")
    conn.close()


def test_database_coordinator_write_and_read(data_root):
    from jobscraper.paths import build_app_paths

    db = Database(build_app_paths(data_root).database_file)
    with db.write_immediate() as tx:
        tx.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        tx.execute("INSERT INTO kv VALUES ('a', 'b')")
    assert db.query_one("SELECT v FROM kv WHERE k='a'")["v"] == "b"
    db.close()
