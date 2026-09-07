"""SQLite connection management and durability verification.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 50
(SQLite operating rules); docs/plans/slice-0-worker-implementation-plan-v0313.md
(S0.2).

Release baseline connections enable foreign keys and use WAL with
``synchronous=FULL``. These are verified as *effective* values on every
relevant connection, not merely configured.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from jobscraper.timeutil import utc_now_s

BUSY_TIMEOUT_MS_DEFAULT = 5000


@dataclass(frozen=True)
class SqliteSettings:
    foreign_keys: int
    journal_mode: str
    synchronous: int
    busy_timeout_ms: int

    def as_dict(self) -> dict[str, object]:
        return {
            "foreign_keys": self.foreign_keys,
            "journal_mode": self.journal_mode,
            "synchronous": self.synchronous,
            "busy_timeout_ms": self.busy_timeout_ms,
        }


def _quote_pragma(value: object) -> str:
    return str(value).replace("'", "''")


def connect_db(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS_DEFAULT) -> sqlite3.Connection:
    """Open a connection with the release durability baseline applied."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=busy_timeout_ms / 1000.0,
        isolation_level=None,  # explicit transaction control
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def read_sqlite_settings(conn: sqlite3.Connection) -> SqliteSettings:
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    return SqliteSettings(
        foreign_keys=int(fk),
        journal_mode=str(journal).upper(),
        synchronous=int(sync),
        busy_timeout_ms=int(busy),
    )


def verify_sqlite_settings(conn: sqlite3.Connection) -> SqliteSettings:
    """Verify the effective PRAGMA values; raise if the baseline is violated."""
    settings = read_sqlite_settings(conn)
    if settings.foreign_keys != 1:
        raise sqlite3.DatabaseError(f"foreign_keys must be ON, got {settings.foreign_keys}")
    if settings.journal_mode != "WAL":
        raise sqlite3.DatabaseError(f"journal_mode must be WAL, got {settings.journal_mode}")
    if settings.synchronous != 2:  # FULL == 2
        raise sqlite3.DatabaseError(f"synchronous must be FULL(2), got {settings.synchronous}")
    if settings.busy_timeout_ms <= 0:
        raise sqlite3.DatabaseError("busy_timeout must be positive")
    return settings


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.__fts5_probe USING fts5(x)"
        )
        conn.execute("DROP TABLE temp.__fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE transaction; commits on success, rolls back on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover - already rolled back
            pass
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover
            pass
        raise
    else:
        conn.execute("COMMIT")


class Database:
    """Single-writer database coordinator.

    The service process owns one write coordinator; readers may open separate
    connections. No network/browser wait may occur while a write transaction is
    held (enforced by discipline in callers; the lock is held only for the
    duration of the ``write`` block).
    """

    def __init__(self, path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS_DEFAULT) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._write_conn = connect_db(self.path, busy_timeout_ms=busy_timeout_ms)
        self._local = threading.local()
        verify_sqlite_settings(self._write_conn)

    # -- write path ---------------------------------------------------------
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._write_conn

    @contextmanager
    def write_immediate(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            with immediate_transaction(self._write_conn) as conn:
                yield conn

    # -- read path ----------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        """Thread-shared write connection (single-writer discipline)."""
        return self._write_conn

    def reader(self) -> sqlite3.Connection:
        """A per-thread read connection with the same baseline PRAGMAs."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect_db(self.path, busy_timeout_ms=self.busy_timeout_ms)
            self._local.conn = conn
        return conn

    # -- helpers ------------------------------------------------------------
    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._write_conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._write_conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._write_conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            try:
                self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:  # pragma: no cover
                pass
            self._write_conn.close()

    def now(self) -> str:
        return utc_now_s()
