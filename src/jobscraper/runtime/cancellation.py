"""Durable cancellation semantics (03 §18).

Cancelling a run:

* sets ``cancel_requested_at`` and stops new source-network acquisition
  claims for that run (enforced in ``claims.claim_next_request``);
* pending/retry-wait acquisition requests become ``CANCELLED``;
* running workers observe cancellation at the fence (their commit is
  refused) and cooperatively abandon via ``abandon_request_for_cancellation``;
* already accepted observations/evidence remain valid;
* durable host-native processing obligations already created for accepted
  observations are NOT cancelled — they keep draining locally and never
  initiate source I/O;
* nothing is deleted.
"""

from __future__ import annotations

import sqlite3

from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES


def request_run_cancellation(conn: sqlite3.Connection, run_id: str, *, now: str | None = None) -> None:
    """Durably request cancellation of a run and cancel its pending
    acquisition work."""
    ts = now or db_utc_now(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE scrape_runs SET cancel_requested_at = COALESCE(cancel_requested_at, ?)"
            " WHERE id = ?",
            (ts, run_id),
        )
        placeholders = ", ".join("?" for _ in ACQUISITION_REQUEST_TYPES)
        conn.execute(
            f"""
            UPDATE scrape_requests
            SET status = 'CANCELLED', finished_at = ?, updated_at = ?,
                current_worker_id = NULL, current_attempt_id = NULL, lease_until = NULL
            WHERE run_id = ? AND status IN ('PENDING', 'RETRY_WAIT')
              AND request_type IN ({placeholders})
            """,
            (ts, ts, run_id, *sorted(ACQUISITION_REQUEST_TYPES)),
        )
        # Groups whose work can never run are terminally CANCELLED.
        conn.execute(
            """
            UPDATE run_source_plans SET group_outcome = 'CANCELLED'
            WHERE run_id = ? AND group_outcome IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM scrape_requests r
                  WHERE r.run_source_plan_id = run_source_plans.id
                    AND r.status NOT IN ('CANCELLED', 'FAILED', 'SUCCEEDED'))
            """,
            (run_id,),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def abandon_request_for_cancellation(
    conn: sqlite3.Connection, request_id: str, *, now: str | None = None
) -> None:
    """A running worker that observed the fence refusing its commit ends
    its request as CANCELLED (its outputs were already rolled back)."""
    ts = now or db_utc_now(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT current_attempt_id FROM scrape_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return
        if row["current_attempt_id"]:
            conn.execute(
                "UPDATE request_attempts SET outcome = 'CANCELLED', finished_at = ?"
                " WHERE attempt_id = ?",
                (ts, row["current_attempt_id"]),
            )
        conn.execute(
            "UPDATE scrape_requests SET status = 'CANCELLED', finished_at = ?,"
            " current_worker_id = NULL, current_attempt_id = NULL, lease_until = NULL,"
            " updated_at = ? WHERE id = ? AND status = 'RUNNING'",
            (ts, ts, request_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def run_is_cancelled(conn: sqlite3.Connection, run_id: str) -> bool:
    row = conn.execute(
        "SELECT cancel_requested_at FROM scrape_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return bool(row and row["cancel_requested_at"] is not None)


__all__ = [
    "abandon_request_for_cancellation",
    "request_run_cancellation",
    "run_is_cancelled",
]
