"""Service-owned control transitions for a claimed request that has not run I/O.

These transitions are coordination outcomes, not provider attempts: capacity or
persisted cooldown may make a just-selected unit temporarily undispatchable.
The request is returned to PENDING/RETRY_WAIT without consuming its execution
attempt budget, while the minted attempt remains auditably ABANDONED.
"""

from __future__ import annotations

import sqlite3

from jobscraper.runtime.clock import current_service_epoch, db_utc_now


def yield_unstarted_claim(
    conn: sqlite3.Connection,
    request_id: str,
    attempt_id: str,
    *,
    reason: str,
    next_retry_at: str | None = None,
    now: str | None = None,
) -> bool:
    """Return a still-owned, never-dispatched claim to durable eligibility.

    ``attempt_count`` is decremented because no executor was invoked.  The
    abandoned attempt row is retained so a crash/race remains inspectable.
    """

    ts = now or db_utc_now(conn)
    target_status = "RETRY_WAIT" if next_retry_at is not None else "PENDING"
    conn.execute("BEGIN IMMEDIATE")
    try:
        epoch = current_service_epoch(conn)
        if epoch is None:
            conn.execute("ROLLBACK")
            return False
        row = conn.execute(
            """
            SELECT req.status, req.current_attempt_id, req.lease_until,
                   req.attempt_count, a.service_epoch_id, a.execution_plan_id
              FROM scrape_requests req
              LEFT JOIN request_attempts a
                ON a.attempt_id = req.current_attempt_id
             WHERE req.id = ?
            """,
            (request_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "RUNNING"
            or row["current_attempt_id"] != attempt_id
            or (row["lease_until"] or "") <= ts
            or row["service_epoch_id"] != epoch.epoch_id
            or row["execution_plan_id"] is not None
        ):
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """
            UPDATE request_attempts
               SET outcome = 'ABANDONED', abandoned_reason = ?, finished_at = ?
             WHERE attempt_id = ? AND request_id = ?
            """,
            (reason, ts, attempt_id, request_id),
        )
        conn.execute(
            """
            UPDATE scrape_requests
               SET status = ?, next_retry_at = ?,
                   attempt_count = CASE WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END,
                   current_worker_id = NULL, current_attempt_id = NULL,
                   lease_until = NULL, heartbeat_at = NULL, updated_at = ?
             WHERE id = ? AND status = 'RUNNING' AND current_attempt_id = ?
            """,
            (target_status, next_retry_at, ts, request_id, attempt_id),
        )
        conn.execute("COMMIT")
        return True
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


__all__ = ["yield_unstarted_claim"]
