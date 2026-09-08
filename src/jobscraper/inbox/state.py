"""Per-profile disposition state (01 PROD-01, PROD-02).

User disposition is persisted independently for each (job_id,
profile_id) pair. Mutations from multiple dashboard tabs use an explicit
row-revision optimistic-concurrency token — silent stale overwrites are
not allowed.
"""

from __future__ import annotations

import sqlite3

from jobscraper.ids import new_id


class DispositionConflict(Exception):
    """A stale row revision was presented (optimistic concurrency)."""


DISPOSITIONS = frozenset({"NONE", "SHORTLISTED", "DISMISSED", "SNOOZED", "ARCHIVED"})


def set_disposition(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    profile_id: str,
    disposition: str,
    now: str,
    reason: str | None = None,
    snoozed_until: str | None = None,
    expected_row_revision: int | None = None,
) -> sqlite3.Row:
    """Set the disposition for one (job, profile) pair.

    ``expected_row_revision`` enables optimistic concurrency: when given,
    a mismatch with the current row revision raises ``DispositionConflict``
    instead of silently overwriting a newer write.
    """
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition: {disposition!r}")
    if disposition == "SNOOZED" and not snoozed_until:
        raise ValueError("SNOOZED requires snoozed_until")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM job_profile_state WHERE job_id = ? AND profile_id = ?",
            (job_id, profile_id),
        ).fetchone()
        if row is None:
            if expected_row_revision is not None:
                raise DispositionConflict(
                    f"no existing row for ({job_id}, {profile_id});"
                    " creation cannot be conditional"
                )
            conn.execute(
                """
                INSERT INTO job_profile_state (
                    job_id, profile_id, disposition, dismissed_reason,
                    snoozed_until, triaged_at, archived_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    profile_id,
                    disposition,
                    reason if disposition == "DISMISSED" else None,
                    snoozed_until,
                    now if disposition != "NONE" else None,
                    now if disposition == "ARCHIVED" else None,
                    now,
                    now,
                ),
            )
        else:
            if (
                expected_row_revision is not None
                and int(expected_row_revision) != int(row["row_revision"])
            ):
                raise DispositionConflict(
                    f"stale row revision {expected_row_revision} !="
                    f" {row['row_revision']} for ({job_id}, {profile_id})"
                )
            conn.execute(
                """
                UPDATE job_profile_state SET
                    disposition = ?,
                    dismissed_reason = ?,
                    snoozed_until = ?,
                    triaged_at = CASE WHEN ? != 'NONE' THEN ? ELSE triaged_at END,
                    archived_at = CASE WHEN ? = 'ARCHIVED' THEN ? ELSE archived_at END,
                    row_revision = row_revision + 1,
                    updated_at = ?
                WHERE job_id = ? AND profile_id = ?
                """,
                (
                    disposition,
                    reason if disposition == "DISMISSED" else None,
                    snoozed_until,
                    disposition,
                    now,
                    disposition,
                    now,
                    now,
                    job_id,
                    profile_id,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return conn.execute(
        "SELECT * FROM job_profile_state WHERE job_id = ? AND profile_id = ?",
        (job_id, profile_id),
    ).fetchone()


def get_state(conn: sqlite3.Connection, job_id: str, profile_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM job_profile_state WHERE job_id = ? AND profile_id = ?",
        (job_id, profile_id),
    ).fetchone()


__all__ = ["DISPOSITIONS", "DispositionConflict", "get_state", "set_disposition"]
