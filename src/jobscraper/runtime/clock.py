"""Durable clock for the runtime core (RUN-20).

Lease and scheduling comparisons use one consistent UTC source: the
database's own clock. ``db_utc_now`` reads SQLite's ``strftime`` and
normalizes it to the repository's canonical RFC-3339 microsecond format so
durable timestamps written by the runtime compare lexicographically with
``timeutil`` output.
"""

from __future__ import annotations

import sqlite3


def db_utc_now(conn: sqlite3.Connection) -> str:
    """The database's UTC now, normalized to RFC-3339 with microseconds."""
    value = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%f', 'now')"
    ).fetchone()[0]
    # '2026-09-08T08:00:00.123' -> '2026-09-08T08:00:00.123000Z'
    if "." not in value:
        return value + ".000000Z"
    head, frac = value.split(".", 1)
    return f"{head}.{frac[:6].ljust(6, '0')}Z"


__all__ = ["db_utc_now"]
