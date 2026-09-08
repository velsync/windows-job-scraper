"""Capability-gated, idempotent provisioning of the FTS5 search index.

The migration (v13) creates only plain bookkeeping tables.  This module owns
the FTS5 virtual table and its sync triggers and creates them only when the
connection really supports FTS5 (``fts5_available``), so a non-FTS5 host
migrates cleanly and records an honest ``SUBSTRING_FALLBACK`` capability.

Invariants:

* ``FTS5_ACTIVE`` is recorded only after the virtual table and all three
  triggers exist;
* provisioning is idempotent — repeated calls never duplicate objects and
  never rewrite ``search_capability.provisioned_at``;
* the sync triggers mirror every ``job_search_docs`` row into the FTS5
  table on INSERT/UPDATE/DELETE, so document maintenance and the index can
  never diverge.
"""

from __future__ import annotations

import sqlite3

from jobscraper.db.connection import fts5_available
from jobscraper.search.capability import (
    SEARCH_MODE_FTS5,
    SEARCH_MODE_SUBSTRING,
    SUBSTRING_WARNING,
    record_capability,
)
from jobscraper.timeutil import utc_now_s

#: FTS5 virtual table (plain, not external-content: the durable content
#: source is ``job_search_docs`` and this table mirrors it through triggers).
FTS_TABLE = "job_search_fts"

_FTS_COLUMNS = (
    "job_id UNINDEXED",
    "title",
    "company",
    "description_text",
    "locations_text",
    "fact_text",
)

_FTS_INSERT = (
    f"INSERT INTO {FTS_TABLE}(rowid, job_id, title, company, description_text,"
    " locations_text, fact_text) VALUES (NEW.doc_id, NEW.job_id, NEW.title,"
    " NEW.company, NEW.description_text, NEW.locations_text, NEW.fact_text)"
)

#: (name, sql) — one per AFTER INSERT/DELETE/UPDATE on job_search_docs.
_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        f"{FTS_TABLE}_ai",
        f"CREATE TRIGGER {FTS_TABLE}_ai AFTER INSERT ON job_search_docs BEGIN"
        f" {_FTS_INSERT}; END",
    ),
    (
        f"{FTS_TABLE}_ad",
        f"CREATE TRIGGER {FTS_TABLE}_ad AFTER DELETE ON job_search_docs BEGIN"
        f" DELETE FROM {FTS_TABLE} WHERE rowid = OLD.doc_id; END",
    ),
    (
        f"{FTS_TABLE}_au",
        f"CREATE TRIGGER {FTS_TABLE}_au AFTER UPDATE ON job_search_docs BEGIN"
        f" DELETE FROM {FTS_TABLE} WHERE rowid = OLD.doc_id; {_FTS_INSERT}; END",
    ),
)


def fts_table_present(conn: sqlite3.Connection) -> bool:
    """True when the FTS5 virtual table exists on this connection."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (FTS_TABLE,),
    ).fetchone()
    return row is not None


def triggers_exist(conn: sqlite3.Connection) -> bool:
    """True when all three FTS sync triggers exist (mirrors the table)."""
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    return all(name in existing for name, _sql in _TRIGGERS)


def _provision_fts_objects(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
        + ", ".join(_FTS_COLUMNS)
        + ")"
    )
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    for name, sql in _TRIGGERS:
        if name not in existing:
            conn.execute(sql)


def provision_search(conn: sqlite3.Connection, *, now: str | None = None) -> dict:
    """Provision search for this connection; idempotent and capability-gated.

    Returns ``{"mode", "fts5_detected", "warning"}``.  Runs outside an
    explicit transaction: FTS5 DDL is idempotent (``IF NOT EXISTS`` +
    existence checks) and the capability row is written last, so a recorded
    ``FTS5_ACTIVE`` always implies the objects exist.
    """
    detected = fts5_available(conn)
    if detected:
        _provision_fts_objects(conn)
    warning = None if detected else SUBSTRING_WARNING
    mode = SEARCH_MODE_FTS5 if detected else SEARCH_MODE_SUBSTRING
    record_capability(
        conn,
        mode=mode,
        fts5_detected=detected,
        warning=warning,
        now=now or utc_now_s(),
    )
    return {"mode": mode, "fts5_detected": detected, "warning": warning}


__all__ = [
    "FTS_TABLE",
    "fts_table_present",
    "provision_search",
    "triggers_exist",
]
