"""Fenced request-owned commit (03 RUN-08, Slice 3 S3.2/S3.4).

Request-owned output and the request transition share one serialized boundary.
The fence verifies exact attempt ownership, an unexpired lease, the current
service epoch and live authorization *inside* the transaction before any
caller mutation executes.  A revoked/cancelled/stale worker therefore commits
zero observations, child work, cursor state or terminal state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from contextlib import contextmanager

from jobscraper.runtime.authorization import require_request_authorized
from jobscraper.runtime.claims import StaleOwnership, _add_seconds
from jobscraper.runtime.clock import current_service_epoch, db_utc_now

_FENCED_OUTCOMES = frozenset({"SUCCEEDED", "FAILED", "RETRY_WAIT"})

@dataclass(frozen=True)
class FenceTransition:
    """Terminal request transition selected under the output fence."""

    outcome: str = "SUCCEEDED"
    retry_delay_s: float | None = None
    failure_kind: str | None = None
    failure_json: str | None = None

    def validate(self) -> None:
        if self.outcome not in _FENCED_OUTCOMES:
            raise ValueError(
                f"fenced outcome must be one of {sorted(_FENCED_OUTCOMES)}"
            )
        if self.outcome == "RETRY_WAIT" and (
            self.retry_delay_s is None or self.retry_delay_s < 0
        ):
            raise ValueError("RETRY_WAIT requires a non-negative retry_delay_s")


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
    failure_json: str | None = None,
    mutate: Callable[[sqlite3.Connection], None] | None = None,
    transition_resolver: Callable[[], FenceTransition] | None = None,
):
    """Run caller mutations and the request transition under one fence."""

    transition = FenceTransition(
        outcome=outcome,
        retry_delay_s=retry_delay_s,
        failure_kind=failure_kind,
        failure_json=failure_json,
    )
    transition.validate()

    ts = now or db_utc_now(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        epoch = current_service_epoch(conn)
        if epoch is None:
            raise StaleOwnership(request_id, "no active service epoch")

        # First prove lease/attempt/epoch ownership.  This write also gives the
        # transaction a concrete single-row fence before output persistence.
        verified = conn.execute(
            """
            UPDATE scrape_requests
               SET heartbeat_at = ?, updated_at = ?
             WHERE id = ? AND status = 'RUNNING' AND current_attempt_id = ?
               AND lease_until > ?
               AND EXISTS (
                   SELECT 1 FROM request_attempts a
                    WHERE a.attempt_id = scrape_requests.current_attempt_id
                      AND a.request_id = scrape_requests.id
                      AND a.service_epoch_id = ?
               )
            """,
            (ts, ts, request_id, attempt_id, ts, epoch.epoch_id),
        )
        if verified.rowcount != 1:
            raise StaleOwnership(request_id, "ownership/lease/service-epoch fence failed")

        # Current authority is re-read under the same write transaction.  For
        # host-native work this intentionally means LOCAL_PROCESSING authority;
        # source-network cancellation/quarantine does not strand accepted
        # evidence (R2-F3).
        require_request_authorized(
            conn,
            request_id,
            attempt_id=attempt_id,
            require_running=True,
        )

        yield conn
        if mutate is not None:
            mutate(conn)
        if transition_resolver is not None:
            transition = transition_resolver()
            transition.validate()

        if transition.outcome == "SUCCEEDED":
            conn.execute(
                """
                UPDATE scrape_requests
                   SET status = 'SUCCEEDED', finished_at = ?,
                       next_retry_at = NULL,
                       current_worker_id = NULL, current_attempt_id = NULL,
                       lease_until = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (ts, ts, request_id),
            )
        elif transition.outcome == "FAILED":
            conn.execute(
                """
                UPDATE scrape_requests
                   SET status = 'FAILED', finished_at = ?,
                       next_retry_at = NULL,
                       current_worker_id = NULL, current_attempt_id = NULL,
                       lease_until = NULL, last_failure_kind = ?,
                       last_failure_json = COALESCE(?, last_failure_json),
                       updated_at = ?
                 WHERE id = ?
                """,
                (ts, transition.failure_kind, transition.failure_json, ts, request_id),
            )
        else:
            next_retry = _add_seconds(ts, float(transition.retry_delay_s))
            conn.execute(
                """
                UPDATE scrape_requests
                   SET status = 'RETRY_WAIT', next_retry_at = ?,
                       current_worker_id = NULL, current_attempt_id = NULL,
                       lease_until = NULL, last_failure_kind = ?,
                       last_failure_json = COALESCE(?, last_failure_json),
                       updated_at = ?
                 WHERE id = ?
                """,
                (next_retry, transition.failure_kind, transition.failure_json, ts, request_id),
            )

        conn.execute(
            """
            UPDATE request_attempts
               SET outcome = ?, failure_kind = ?, finished_at = ?
             WHERE attempt_id = ? AND request_id = ?
            """,
            (transition.outcome, transition.failure_kind, ts, attempt_id, request_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover - already rolled back
            pass
        raise


__all__ = ["FenceTransition", "fenced_commit"]
