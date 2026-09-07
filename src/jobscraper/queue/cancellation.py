"""Durable run cancellation.

Authority: module 03 section 18 (cancellation semantics).

Cancellation stops new source-network acquisition, fences running workers at
their next bounded checkpoint, cancels pending/retry-wait acquisition
requests, preserves already accepted observations/evidence, and leaves
durable downstream processing obligations resumable (they drain locally
without new network I/O).
"""

from __future__ import annotations

import json

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import utc_now_s


def cancel_run(db: Database, run_id: str, *, reason: str = "user", now: str | None = None) -> dict:
    now = now or utc_now_s()
    cancelled_requests = 0
    with immediate_transaction(db.conn) as tx:
        run = tx.execute("SELECT status, cancel_requested_at FROM scrape_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        if run["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return {"already_terminal": True, "cancelled_requests": 0}
        tx.execute(
            "UPDATE scrape_runs SET cancel_requested_at=?, cancel_reason=? WHERE id=?",
            (now, reason, run_id),
        )
        cur = tx.execute(
            "UPDATE scrape_requests SET status='CANCELLED', finished_at=COALESCE(finished_at,?),"
            " updated_at=? WHERE run_id=? AND status IN ('PENDING','RETRY_WAIT')",
            (now, now, run_id),
        )
        cancelled_requests = cur.rowcount
        # Running requests are fenced at their next commit/heartbeat; running
        # attempts whose workers observe cancellation abandon their work.
        tx.execute(
            "UPDATE scrape_requests SET status='CANCELLED', finished_at=?, updated_at=?"
            " WHERE run_id=? AND status='RUNNING' AND current_attempt_id IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM processing_obligations o WHERE o.request_id = scrape_requests.id AND o.status='PENDING')",
            (now, now, run_id),
        )
    return {"already_terminal": False, "cancelled_requests": cancelled_requests}


def run_is_cancelled(db: Database, run_id: str) -> bool:
    row = db.query_one("SELECT cancel_requested_at FROM scrape_runs WHERE id=?", (run_id,))
    return bool(row and row["cancel_requested_at"] is not None)
