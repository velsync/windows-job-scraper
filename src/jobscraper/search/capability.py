"""Search capability records (01 §45 capability honesty).

``search_capability`` holds exactly one row describing which search engine is
active and why.  ``FTS5_ACTIVE`` is recorded only after the FTS5 objects were
really provisioned (see :mod:`jobscraper.search.provision`); every
``SUBSTRING_FALLBACK`` record carries an explicit warning so no consumer can
mistake the fallback for FTS/BM25.
"""

from __future__ import annotations

import sqlite3

#: Recorded active mode when the FTS5 index is really provisioned.
SEARCH_MODE_FTS5 = "FTS5_ACTIVE"

#: Recorded active mode when the host's SQLite lacks FTS5 (or provisioning
#: has not run); BM25 is never claimed in this mode.
SEARCH_MODE_SUBSTRING = "SUBSTRING_FALLBACK"

#: Warning carried by every SUBSTRING_FALLBACK record/response.
SUBSTRING_WARNING = (
    "SQLite FTS5 is unavailable in this build — substring search fallback is"
    " active; BM25/FTS is not claimed."
)


def read_capability(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The recorded capability row, or None when never provisioned."""
    return conn.execute(
        "SELECT mode, fts5_detected, warning, provisioned_at, updated_at"
        " FROM search_capability WHERE id = 1"
    ).fetchone()


def record_capability(
    conn: sqlite3.Connection,
    *,
    mode: str,
    fts5_detected: bool,
    warning: str | None,
    now: str,
) -> None:
    """Upsert the single capability row.

    ``provisioned_at`` keeps the first provisioning instant; re-provisioning
    only refreshes ``mode``/``warning``/``updated_at`` (idempotent).
    """
    conn.execute(
        "INSERT INTO search_capability"
        " (id, mode, fts5_detected, warning, provisioned_at, updated_at)"
        " VALUES (1, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET"
        " mode = excluded.mode, fts5_detected = excluded.fts5_detected,"
        " warning = excluded.warning, updated_at = excluded.updated_at",
        (mode, 1 if fts5_detected else 0, warning, now, now),
    )


__all__ = [
    "SEARCH_MODE_FTS5",
    "SEARCH_MODE_SUBSTRING",
    "SUBSTRING_WARNING",
    "read_capability",
    "record_capability",
]
