"""Tests for SQLite connection/PRAGMA enforcement (S0.2).

Includes the corrective regression tests for reader-connection lifecycle and
FTS5 capability/status separation from the audit findings.
"""

import sqlite3
import threading

import pytest

from jobscraper.db.connection import (
    Database,
    connect_db,
    fts5_available,
    immediate_transaction,
    read_sqlite_settings,
    verify_sqlite_settings,
)
from jobscraper.paths import build_app_paths


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
    db = Database(build_app_paths(data_root).database_file)
    with db.write_immediate() as tx:
        tx.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        tx.execute("INSERT INTO kv VALUES ('a', 'b')")
    assert db.query_one("SELECT v FROM kv WHERE k='a'")["v"] == "b"
    db.close()


def test_reader_connection_is_per_thread_and_baselined(data_root):
    db = Database(build_app_paths(data_root).database_file)
    readers: dict[str, sqlite3.Connection] = {}

    def open_reader(name: str) -> None:
        readers[name] = db.reader()

    t = threading.Thread(target=open_reader, args=("worker",))
    t.start()
    t.join()
    reader = readers["worker"]
    # The reader gets the same effective PRAGMA baseline.
    assert read_sqlite_settings(reader).journal_mode == "WAL"
    assert db.reader() is not reader  # main-thread reader is a distinct connection
    db.close()


def test_close_closes_reader_connections(data_root):
    """Regression (audit finding): reader connections must not leak past close.

    Every reader opened through Database.reader() must be closed by
    Database.close(); a leaked reader keeps a WAL database pinned and blocks
    checkpoint/truncate on shutdown.
    """
    from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema

    db = Database(build_app_paths(data_root).database_file)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    db.conn.execute(
        "INSERT INTO events(at, level, kind, message, data_json) "
        "VALUES ('2026-01-01T00:00:00.000000Z','INFO','T','m','{}')"
    )

    readers: list[sqlite3.Connection] = []

    def open_reader() -> None:
        readers.append(db.reader())

    threads = [threading.Thread(target=open_reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    readers.append(db.reader())  # main-thread reader too
    assert len(db._readers) == 4

    db.close()

    for reader in readers:
        # A closed connection raises ProgrammingError on use.
        with pytest.raises(sqlite3.ProgrammingError):
            reader.execute("SELECT 1").fetchone()
    assert db._readers == set()


def test_run_database_checks_fts5_capability_separate_from_integrity(tmp_path, monkeypatch):
    """Regression (audit finding): FTS5 unavailability must be a capability
    result that health consumers can act on — not a problems entry the ``ok``
    flag never reflected."""
    from jobscraper.db import migrations as mig

    conn = connect_db(tmp_path / "f.db")
    migrate = None
    from jobscraper.db.migrations import migrate_schema

    migrate_schema(conn, 2)

    monkeypatch.setattr(mig, "fts5_available", lambda _conn: False)
    checks = mig.run_database_checks(conn)
    assert checks["fts5"] is False
    assert checks["fts5_available"] is False
    # Integrity itself is still fine; the capability is surfaced separately so
    # Doctor/health can turn it into the correct status.
    assert checks["ok"] is True
    assert checks["problems"] == []
    conn.close()
    assert migrate is None
