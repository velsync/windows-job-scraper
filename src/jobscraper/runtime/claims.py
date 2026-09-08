"""Atomic claim, lease, heartbeat and reclaim (03 RUN-06, RUN-07).

Single-winner claims via ``BEGIN IMMEDIATE``; a claim mints a fresh
attempt identity and opens a lease. An expired lease is *already lost
ownership* even if the reclaimer has not yet executed — a worker MUST NOT
revive it (heartbeat/commit refuse stale tokens). Reclaim records the
prior attempt as ABANDONED and requeues within the attempt budget.

Claims respect source/binding desired+administrative state and run
cancellation: after cancellation only host-native request types stay
claimable, so accepted evidence drains while no new source I/O starts
(§18).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobscraper.ids import new_id
from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES

DEFAULT_LEASE_WINDOW_S = 120.0
RETRY_BACKOFF_BASE_S = 5.0
RETRY_BACKOFF_CAP_S = 300.0


class StaleOwnership(Exception):
    """The caller no longer owns the request (lease lost/expired/cancelled)."""

    def __init__(self, request_id: str, reason: str):
        super().__init__(f"stale ownership of request {request_id}: {reason}")
        self.request_id = request_id
        self.reason = reason


@dataclass(frozen=True)
class Claim:
    request_id: str
    attempt_id: str
    run_id: str
    run_source_plan_id: str | None
    request_type: str
    payload: dict
    strategy: str | None
    execution_class: str | None
    lease_until: str


_ELIGIBLE_SQL = """
    SELECT req.id FROM scrape_requests req
    JOIN scrape_runs run ON run.id = req.run_id
    JOIN sources s ON s.id = req.source_id
    JOIN source_adapter_bindings b ON b.id = req.binding_id
    WHERE req.status = 'PENDING'
       OR (req.status = 'RETRY_WAIT' AND req.next_retry_at <= :now)
    ORDER BY req.priority DESC, req.created_at ASC, req.id ASC
    LIMIT 50
"""


def _claimable(row: sqlite3.Row, now: str) -> bool:
    if row["cancel_requested_at"] is not None and row["request_type"] in ACQUISITION_REQUEST_TYPES:
        return False  # §18: no new source-network acquisition claims after cancellation
    if row["s_desired"] != "ENABLED" or row["s_admin"] != "NORMAL":
        return False
    if row["b_desired"] != "ENABLED" or row["b_admin"] != "NORMAL":
        return False
    status = row["status"]
    if status == "PENDING":
        return True
    if status == "RETRY_WAIT":
        return (row["next_retry_at"] or "") <= now
    return False


def claim_next_request(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    now: str | None = None,
    lease_window_s: float = DEFAULT_LEASE_WINDOW_S,
    types: frozenset[str] | None = None,
    run_source_plan_id: str | None = None,
) -> Claim | None:
    """Atomically claim the next eligible request (single winner)."""
    ts = now or db_utc_now(conn)
    attempt_id = new_id("att")
    conn.execute("BEGIN IMMEDIATE")
    try:
        type_filter = ""
        params: list = []
        if types:
            type_filter = (
                " AND req.request_type IN ("
                + ", ".join("?" for _ in sorted(types))
                + ")"
            )
            params.extend(sorted(types))
        if run_source_plan_id:
            type_filter += " AND req.run_source_plan_id = ?"
            params.append(run_source_plan_id)
        candidates = conn.execute(
            """
            SELECT req.id, req.status, req.next_retry_at, req.request_type,
                   run.cancel_requested_at,
                   s.desired_state AS s_desired, s.administrative_state AS s_admin,
                   b.desired_state AS b_desired, b.administrative_state AS b_admin
            FROM scrape_requests req
            JOIN scrape_runs run ON run.id = req.run_id
            JOIN sources s ON s.id = req.source_id
            JOIN source_adapter_bindings b ON b.id = req.binding_id
            WHERE req.status IN ('PENDING', 'RETRY_WAIT')
            """ + type_filter + """
            ORDER BY req.priority DESC, req.created_at ASC, req.id ASC
            LIMIT 100
            """,
            params,
        ).fetchall()
        chosen = next((row for row in candidates if _claimable(row, ts)), None)
        if chosen is None:
            conn.execute("COMMIT")
            return None
        request_id = chosen["id"]
        lease_until = _add_seconds(ts, lease_window_s)
        updated = conn.execute(
            """
            UPDATE scrape_requests
            SET status = 'RUNNING', current_worker_id = ?, current_attempt_id = ?,
                lease_until = ?, heartbeat_at = ?, attempt_count = attempt_count + 1,
                started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'RETRY_WAIT')
            """,
            (worker_id, attempt_id, lease_until, ts, ts, ts, request_id),
        )
        if updated.rowcount != 1:  # pragma: no cover - serialized by IMMEDIATE
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            """
            INSERT INTO request_attempts (
                attempt_id, request_id, worker_id, started_at, lease_expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, request_id, worker_id, ts, lease_until, ts),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    row = conn.execute(
        "SELECT * FROM scrape_requests WHERE id = ?", (request_id,)
    ).fetchone()
    import json as _json

    return Claim(
        request_id=request_id,
        attempt_id=attempt_id,
        run_id=row["run_id"],
        run_source_plan_id=row["run_source_plan_id"],
        request_type=row["request_type"],
        payload=_json.loads(row["payload_json"] or "{}"),
        strategy=row["strategy"],
        execution_class=row["execution_class"],
        lease_until=lease_until,
    )


def heartbeat(
    conn: sqlite3.Connection,
    request_id: str,
    attempt_id: str,
    *,
    now: str | None = None,
    lease_window_s: float = DEFAULT_LEASE_WINDOW_S,
) -> str:
    """Renew the lease; refused for stale tokens or expired leases."""
    ts = now or db_utc_now(conn)
    lease_until = _add_seconds(ts, lease_window_s)
    conn.execute("BEGIN IMMEDIATE")
    try:
        updated = conn.execute(
            """
            UPDATE scrape_requests
            SET heartbeat_at = ?, lease_until = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND current_attempt_id = ?
              AND lease_until > ?
              AND NOT EXISTS (
                  SELECT 1 FROM scrape_runs r WHERE r.id = scrape_requests.run_id
                    AND r.cancel_requested_at IS NOT NULL)
            """,
            (ts, lease_until, ts, request_id, attempt_id, ts),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            row = conn.execute(
                "SELECT status, current_attempt_id, lease_until FROM scrape_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise StaleOwnership(request_id, "request does not exist")
            if row["status"] != "RUNNING":
                raise StaleOwnership(request_id, f"request is {row['status']}")
            if row["current_attempt_id"] != attempt_id:
                raise StaleOwnership(request_id, "attempt token no longer owns the request")
            if (row["lease_until"] or "") <= ts:
                raise StaleOwnership(request_id, "lease already expired")
            raise StaleOwnership(request_id, "run invalidated")  # pragma: no cover
        conn.execute(
            "UPDATE request_attempts SET last_heartbeat_at = ?, lease_expires_at = ?"
            " WHERE attempt_id = ?",
            (ts, lease_until, attempt_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover
            pass
        raise
    return lease_until


def reclaim_expired(conn: sqlite3.Connection, *, now: str | None = None) -> list[str]:
    """Reclaim requests whose leases expired: prior attempt ABANDONED, then
    RETRY_WAIT within budget or FAILED when the budget is exhausted."""
    ts = now or db_utc_now(conn)
    expired = conn.execute(
        """
        SELECT id, current_attempt_id, attempt_count, max_attempts
        FROM scrape_requests
        WHERE status = 'RUNNING' AND lease_until IS NOT NULL AND lease_until <= ?
        """,
        (ts,),
    ).fetchall()
    reclaimed: list[str] = []
    for row in expired:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: the worker may have committed.
            current = conn.execute(
                "SELECT status, current_attempt_id, attempt_count, max_attempts,"
                " lease_until"
                " FROM scrape_requests WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if (
                current is None
                or current["status"] != "RUNNING"
                or current["current_attempt_id"] != row["current_attempt_id"]
                or not (current["lease_until"] or "")
                or current["lease_until"] > ts
            ):
                conn.execute("COMMIT")
                continue
            if current["current_attempt_id"]:
                conn.execute(
                    "UPDATE request_attempts SET outcome = 'ABANDONED',"
                    " abandoned_reason = 'LEASE_EXPIRED', finished_at = ?"
                    " WHERE attempt_id = ?",
                    (ts, current["current_attempt_id"]),
                )
            if current["attempt_count"] >= current["max_attempts"]:
                conn.execute(
                    "UPDATE scrape_requests SET status = 'FAILED',"
                    " last_failure_kind = 'LEASE_LOST', last_failure_json = ?,"
                    " current_worker_id = NULL, current_attempt_id = NULL,"
                    " finished_at = ?, updated_at = ? WHERE id = ?",
                    (
                        '{"kind": "LEASE_LOST", "detail": "attempt budget exhausted"}',
                        ts,
                        ts,
                        row["id"],
                    ),
                )
            else:
                backoff = min(
                    RETRY_BACKOFF_BASE_S * max(1, current["attempt_count"]),
                    RETRY_BACKOFF_CAP_S,
                )
                conn.execute(
                    "UPDATE scrape_requests SET status = 'RETRY_WAIT',"
                    " next_retry_at = ?, current_worker_id = NULL,"
                    " current_attempt_id = NULL, lease_until = NULL, updated_at = ?"
                    " WHERE id = ?",
                    (_add_seconds(ts, backoff), ts, row["id"]),
                )
            conn.execute("COMMIT")
            reclaimed.append(row["id"])
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return reclaimed


def _add_seconds(rfc3339: str, seconds: float) -> str:
    from datetime import datetime, timedelta

    from jobscraper.timeutil import parse_rfc3339, to_rfc3339

    dt: datetime = parse_rfc3339(rfc3339)
    return to_rfc3339(dt + timedelta(seconds=seconds))


__all__ = [
    "Claim",
    "StaleOwnership",
    "claim_next_request",
    "heartbeat",
    "reclaim_expired",
]
