"""Fenced terminal commit (03 RUN-08).

Output persistence and the request terminal transition are atomic under the
ownership fence:

    BEGIN IMMEDIATE
    verify: request RUNNING, current_attempt_id = this attempt,
            lease_until > now, run not invalidated for terminal commit
    persist: caller's outputs (observations, evidence, obligations,
             child work, cursor)
    transition: SUCCEEDED, or RETRY_WAIT when validated PARTIAL policy
                requires retry
    COMMIT

If the ownership verification affects zero rows the worker MUST NOT commit
request-owned outputs — everything rolls back, including anything the
caller already wrote inside the fence.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import contextmanager

from jobscraper.runtime.claims import StaleOwnership, _add_seconds
from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES
from jobscraper.runtime.clock import db_utc_now

_TERMINAL_OUTCOMES = frozenset({"SUCCEEDED", "RETRY_WAIT"})


@contextmanager
def fenced_commit(
    conn: sqlite3.Connection,
    request_id: str,
    attempt_id: str,
    *,
    now: str | None = None,
    outcome: str = "SUCCEEDED",
    retry_delay_s: float | None = None,
    failure_kind: str | None = None,
    mutate: Callable[[sqlite3.Connection], None] | None = None,
):
    """Run ``mutate`` and the terminal transition under one ownership fence.

    Yields nothing; the caller's ``mutate`` performs request-owned output
    persistence. A ``StaleOwnership`` raised on entry means nothing was
    written; the context body is never entered.
    """
    if outcome not in _TERMINAL_OUTCOMES:
        raise ValueError(f"fenced outcome must be one of {sorted(_TERMINAL_OUTCOMES)}")
    ts = now or db_utc_now(conn)
    # §18: cancellation invalidates terminal commits for source-network
    # acquisition work; host-native obligations keep draining locally (they
    # never initiate source I/O).
    request_type_row = conn.execute(
        "SELECT request_type FROM scrape_requests WHERE id = ?", (request_id,)
    ).fetchone()
    request_type = request_type_row["request_type"] if request_type_row else ""
    cancellation_guard = (
        ""
        if request_type not in ACQUISITION_REQUEST_TYPES
        else """
              AND NOT EXISTS (
                  SELECT 1 FROM scrape_runs r WHERE r.id = scrape_requests.run_id
                    AND r.cancel_requested_at IS NOT NULL)"""
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        verified = conn.execute(
            """
            UPDATE scrape_requests
            SET heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND current_attempt_id = ?
              AND lease_until > ?
            """ + cancellation_guard,
            (ts, ts, request_id, attempt_id, ts),
        )
        if verified.rowcount != 1:
            raise StaleOwnership(request_id, "ownership verification affected zero rows")
        # The with-body (and then the mutate callback) run inside the fence;
        # an exception from either rolls the whole transaction back.
        yield conn
        if mutate is not None:
            mutate(conn)
        if outcome == "SUCCEEDED":
            conn.execute(
                "UPDATE scrape_requests SET status = 'SUCCEEDED', finished_at = ?,"
                " current_worker_id = NULL, current_attempt_id = NULL, lease_until = NULL,"
                " updated_at = ? WHERE id = ?",
                (ts, ts, request_id),
            )
        else:  # RETRY_WAIT: outputs committed, request stays retryable
            next_retry = _add_seconds(ts, retry_delay_s if retry_delay_s is not None else 5.0)
            conn.execute(
                "UPDATE scrape_requests SET status = 'RETRY_WAIT', next_retry_at = ?,"
                " current_worker_id = NULL, current_attempt_id = NULL, lease_until = NULL,"
                " last_failure_kind = ?, updated_at = ? WHERE id = ?",
                (next_retry, failure_kind, ts, request_id),
            )
        conn.execute(
            "UPDATE request_attempts SET outcome = ?, finished_at = ? WHERE attempt_id = ?",
            (outcome, ts, attempt_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover - already rolled back
            pass
        raise


__all__ = ["fenced_commit"]
