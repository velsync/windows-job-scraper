"""Capability-gated, idempotent provisioning of the S2.3 search surface.

Migration v13 creates only plain bookkeeping tables.  This module owns the
host-side derived search surfaces:

* ``job_search_docs`` / ``job_search_state`` are backfilled from canonical jobs
  at provisioning time, so a v12→v13 upgrade is searchable immediately even
  before any job is re-observed;
* the FTS5 virtual table and sync triggers are created only when the connection
  really supports FTS5;
* whenever FTS5 is (re-)provisioned, the virtual table is reconciled exactly
  from durable ``job_search_docs`` before ``FTS5_ACTIVE`` is recorded.  This
  closes the recovery window where documents may have changed while sync
  triggers were absent.

``search_capability`` is written last, so the application never advertises
FTS5/BM25 over an incomplete derived index.
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


def _sync_durable_docs(conn: sqlite3.Connection, *, now: str) -> None:
    """Backfill/refresh the durable search corpus from every canonical job.

    ``sync_search_doc`` is revision-checked, so normal repeated service starts
    are no-ops.  This pass matters on the first v13 start because migration
    deliberately does not perform data-dependent search writes.
    """
    from jobscraper.search.index import sync_search_doc

    for row in conn.execute("SELECT id FROM jobs ORDER BY id").fetchall():
        sync_search_doc(conn, job_id=row["id"], now=now)


def _reconcile_fts_from_docs(conn: sqlite3.Connection) -> None:
    """Make the derived FTS table an exact mirror of durable search docs.

    Recreating missing triggers is insufficient when docs changed while those
    triggers were absent.  The virtual table is derived state, so rebuilding it
    from ``job_search_docs`` is safe and deterministic.  Capability is recorded
    only after this reconciliation succeeds.
    """
    conn.execute(f"DELETE FROM {FTS_TABLE}")
    conn.execute(
        f"INSERT INTO {FTS_TABLE}"
        " (rowid, job_id, title, company, description_text, locations_text, fact_text)"
        " SELECT doc_id, job_id, title, company, description_text, locations_text, fact_text"
        " FROM job_search_docs ORDER BY doc_id"
    )


def provision_search(conn: sqlite3.Connection, *, now: str | None = None) -> dict:
    """Provision a complete search surface; idempotent and capability-gated.

    Returns ``{"mode", "fts5_detected", "warning"}``.  The durable search
    corpus is synchronized on every provisioning pass.  When FTS5 is present,
    its objects are created/repaired and the index is then reconciled exactly
    from the durable corpus before ``FTS5_ACTIVE`` is recorded.
    """
    effective_now = now or utc_now_s()

    # Always maintain the durable corpus, including on non-FTS5 hosts where it
    # is the authoritative SUBSTRING_FALLBACK search surface.
    _sync_durable_docs(conn, now=effective_now)

    detected = fts5_available(conn)
    if detected:
        _provision_fts_objects(conn)
        _reconcile_fts_from_docs(conn)

    warning = None if detected else SUBSTRING_WARNING
    mode = SEARCH_MODE_FTS5 if detected else SEARCH_MODE_SUBSTRING
    record_capability(
        conn,
        mode=mode,
        fts5_detected=detected,
        warning=warning,
        now=effective_now,
    )
    return {"mode": mode, "fts5_detected": detected, "warning": warning}


__all__ = [
    "FTS_TABLE",
    "fts_table_present",
    "provision_search",
    "triggers_exist",
]
