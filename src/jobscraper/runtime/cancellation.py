"""Durable cancellation semantics (03 §18, S3.4).

Cancelling a run stops new source-network acquisition while preserving
already-accepted evidence and its host-native processing obligations.  Running
acquisition workers lose terminal-commit authority at the fence; host-native
obligations remain locally claimable and never gain source-network authority.
"""

from __future__ import annotations

import json
import sqlite3

from jobscraper.runtime.clock import current_service_epoch, db_utc_now
from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES
from jobscraper.runtime.runs import cancel_open_groups


def request_run_cancellation(
    conn: sqlite3.Connection, run_id: str, *, now: str | None = None
) -> None:
    """Durably request cancellation and close pending network acquisition."""
    ts = now or db_utc_now(conn)
    detail = json.dumps(
        {"kind": "CANCELLED", "detail": "run cancellation requested"},
        sort_keys=True,
        separators=(",", ":"),
    )
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
                   next_retry_at = NULL, current_worker_id = NULL, current_attempt_id = NULL,
                   lease_until = NULL, last_failure_kind = 'CANCELLED',
                   last_failure_json = ?
             WHERE run_id = ? AND status IN ('PENDING', 'RETRY_WAIT')
               AND request_type IN ({placeholders})
            """,
            (ts, ts, detail, run_id, *sorted(ACQUISITION_REQUEST_TYPES)),
        )
        cancel_open_groups(conn, run_id, now=ts, commit=False)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def abandon_request_for_cancellation(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    attempt_id: str | None = None,
    now: str | None = None,
) -> bool:
    """Close the still-current running acquisition after its fence was denied.

    Request-owned outputs were rolled back before this function is called.
    Existing observations from earlier accepted attempts are never deleted.
    """
    detail = json.dumps(
        {"kind": "CANCELLED", "detail": "current run authority cancelled"},
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        ts = now or db_utc_now(conn)
        epoch = current_service_epoch(conn)
        row = conn.execute(
            """
            SELECT req.current_attempt_id, req.request_type, req.status, req.lease_until,
                   a.service_epoch_id, run.cancel_requested_at
              FROM scrape_requests req
              JOIN scrape_runs run ON run.id = req.run_id
              LEFT JOIN request_attempts a ON a.attempt_id = req.current_attempt_id
             WHERE req.id = ?
            """,
            (request_id,),
        ).fetchone()
        if (
            row is None
            or epoch is None
            or row["status"] != "RUNNING"
            or (row["lease_until"] or "") <= ts
            or row["service_epoch_id"] != epoch.epoch_id
            or row["request_type"] not in ACQUISITION_REQUEST_TYPES
            or row["cancel_requested_at"] is None
            or (attempt_id is not None and row["current_attempt_id"] != attempt_id)
        ):
            # R2-F3: this helper can never silently cancel a host-native
            # obligation or a newer owner that replaced the caller's attempt.
            conn.execute("COMMIT")
            return False
        if row["current_attempt_id"]:
            conn.execute(
                """
                UPDATE request_attempts
                   SET outcome = 'CANCELLED', failure_kind = 'CANCELLED',
                       finished_at = ?
                 WHERE attempt_id = ? AND request_id = ?
                """,
                (ts, row["current_attempt_id"], request_id),
            )
        conn.execute(
            """
            UPDATE scrape_requests
               SET status = 'CANCELLED', finished_at = ?,
                   next_retry_at = NULL, current_worker_id = NULL, current_attempt_id = NULL,
                   lease_until = NULL, last_failure_kind = 'CANCELLED',
                   last_failure_json = ?, updated_at = ?
             WHERE id = ? AND status = 'RUNNING'
            """,
            (ts, detail, ts, request_id),
        )
        conn.execute("COMMIT")
        return True
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
