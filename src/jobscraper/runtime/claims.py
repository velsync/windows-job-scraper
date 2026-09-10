"""Atomic claim, lease, heartbeat and reclaim (03 RUN-06, RUN-07, §50).

Single-winner claims via ``BEGIN IMMEDIATE``; a claim mints a fresh
attempt identity, binds it to the current service epoch, and opens a
lease. An expired lease is *already lost ownership* even if the reclaimer
has not yet executed — a worker MUST NOT revive it (heartbeat/commit refuse
stale tokens). Reclaim records the prior attempt as ABANDONED and requeues
within the attempt budget; budget exhaustion is terminal failure.

Claim/heartbeat authorization is task-class aware (R2-F3):

* **Acquisition requests** require current run/source/binding authority
  (RUN-02 rule 9): after durable cancellation no new source-network claims
  start (§18), and disabled/quarantined sources/bindings are not claimable
  or heartbeat-able.
* **Host-native requests** are local evidence-processing obligations. They
  never gain source-network authority from being claimable, and they must
  not be stranded by the source-network predicate: cancellation, source or
  binding disable/quarantine never blocks their claim or heartbeat, so
  accepted observations keep draining (§18).

Wall-clock anomalies (§50) are handled through service epochs: when the
clock guard rotates the epoch, claim/heartbeat reject stale-epoch ownership
and :func:`reclaim_orphaned_epoch_work` abandons/requeues the invalidated
epoch's RUNNING work regardless of lease deadline. Claim and heartbeat
transactions run against the database only — no network/browser/file I/O
while holding them (§50).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobscraper.ids import new_id
from jobscraper.runtime.clock import (
    ServiceClockGuard,
    ServiceEpoch,
    StaleServiceEpoch,
    current_service_epoch,
    db_utc_now,
)
from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES

DEFAULT_LEASE_WINDOW_S = 120.0
RETRY_BACKOFF_BASE_S = 5.0
RETRY_BACKOFF_CAP_S = 300.0

ABANDONED_LEASE_EXPIRED = "LEASE_EXPIRED"
ABANDONED_SERVICE_RESTART = "SERVICE_RESTART"
ABANDONED_CLOCK_ANOMALY = "CLOCK_ANOMALY"


class StaleOwnership(Exception):
    """The caller no longer owns the request (lease lost/expired/cancelled)."""

    def __init__(self, request_id: str, reason: str):
        super().__init__(f"stale ownership of request {request_id}: {reason}")
        self.request_id = request_id
        self.reason = reason


class ClaimsHalted(Exception):
    """New claims are halted (material clock anomaly pending safe reclaim)."""


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
    service_epoch_id: str | None = None


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
    status = row["status"]
    if status == "PENDING":
        due = True
    elif status == "RETRY_WAIT":
        due = (row["next_retry_at"] or "") <= now  # due only after durable retry time
    else:
        due = False
    if not due:
        return False
    if row["request_type"] in ACQUISITION_REQUEST_TYPES:
        # Acquisition requests require current authority (RUN-02 rule 9):
        # durable cancellation stops new source-network claims (§18), and a
        # disabled/quarantined source or binding is not claimable.
        if row["cancel_requested_at"] is not None:
            return False
        if row["s_desired"] != "ENABLED" or row["s_admin"] != "NORMAL":
            return False
        if row["b_desired"] != "ENABLED" or row["b_admin"] != "NORMAL":
            return False
    # Host-native requests are local evidence-processing obligations: they
    # never gain source-network authority from being claimable, so the
    # source-network predicate above must not strand them (R2-F3, §18).
    return True


def _open_epoch_or_stale_check(
    conn: sqlite3.Connection, epoch: ServiceEpoch | None
) -> ServiceEpoch | None:
    """Resolve the epoch to bind attempts to inside the claim transaction.

    With no explicit epoch the current open epoch is used (``None`` before
    any epoch exists, keeping pre-epoch databases claimable). An explicit
    epoch must still be the current one, otherwise it is stale.
    """
    current = current_service_epoch(conn)
    if epoch is None:
        return current
    if current is None or current.epoch_id != epoch.epoch_id:
        raise StaleServiceEpoch(
            f"service epoch {epoch.epoch_id} is not the current service epoch",
            epoch_id=epoch.epoch_id,
        )
    return current


def claim_next_request(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    now: str | None = None,
    lease_window_s: float = DEFAULT_LEASE_WINDOW_S,
    types: frozenset[str] | None = None,
    run_source_plan_id: str | None = None,
    epoch: ServiceEpoch | None = None,
    guard: ServiceClockGuard | None = None,
) -> Claim | None:
    """Atomically claim the next eligible request (single winner).

    The claim mints a fresh ``attempt_id`` and binds it to the current
    service epoch. When a :class:`ServiceClockGuard` is supplied it is
    observed first: a material wall-clock anomaly rotates the epoch and
    halts new claims (:class:`ClaimsHalted`) until the coordinator reclaims
    the invalidated epoch's work and clears the halt (§50). No network or
    file I/O occurs while the claim transaction is held.
    """
    ts = now or db_utc_now(conn)
    if guard is not None:
        guard.observe(conn, db_now=ts)
        if guard.claims_halted:
            halt = guard.halt
            detail = (
                f" after {halt.direction} clock anomaly; epoch"
                f" {halt.invalidated_epoch_id} invalidated"
                if halt is not None
                else ""
            )
            raise ClaimsHalted(f"new claims halted{detail} (§50)")
    attempt_id = new_id("att")
    conn.execute("BEGIN IMMEDIATE")
    try:
        bound_epoch = _open_epoch_or_stale_check(conn, epoch)
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
                attempt_id, request_id, worker_id, started_at, lease_expires_at,
                service_epoch_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                request_id,
                worker_id,
                ts,
                lease_until,
                bound_epoch.epoch_id if bound_epoch is not None else None,
                ts,
            ),
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
        service_epoch_id=bound_epoch.epoch_id if bound_epoch is not None else None,
    )


def heartbeat(
    conn: sqlite3.Connection,
    request_id: str,
    attempt_id: str,
    *,
    now: str | None = None,
    lease_window_s: float = DEFAULT_LEASE_WINDOW_S,
    epoch: ServiceEpoch | None = None,
) -> str:
    """Renew the lease; refused for stale tokens, expired leases or stale epochs.

    Accepted only when the request is RUNNING under this exact attempt, the
    lease is unexpired by database time, the attempt's service epoch is
    still current, and — for acquisition work — the run is not cancelled
    and current source/binding authority still holds (RUN-02 rule 9, §18).
    Host-native heartbeats are independent of the source-network predicate
    (R2-F3): accepted local obligations keep draining.
    """
    ts = now or db_utc_now(conn)
    if epoch is not None:
        row = conn.execute(
            "SELECT ended_at FROM service_clock_epochs WHERE id = ?",
            (epoch.epoch_id,),
        ).fetchone()
        if row is None or row["ended_at"] is not None:
            raise StaleServiceEpoch(
                f"service epoch {epoch.epoch_id} is not the current service epoch",
                epoch_id=epoch.epoch_id,
            )
    lease_until = _add_seconds(ts, lease_window_s)
    conn.execute("BEGIN IMMEDIATE")
    try:
        request_row = conn.execute(
            "SELECT request_type FROM scrape_requests WHERE id = ?", (request_id,)
        ).fetchone()
        is_acquisition = bool(request_row) and (
            request_row["request_type"] in ACQUISITION_REQUEST_TYPES
        )
        current_epoch = current_service_epoch(conn)
        sql = (
            """
            UPDATE scrape_requests
            SET heartbeat_at = ?, lease_until = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND current_attempt_id = ?
              AND lease_until > ?
              AND EXISTS (
                  SELECT 1 FROM request_attempts a
                  WHERE a.attempt_id = scrape_requests.current_attempt_id
                    AND (a.service_epoch_id IS NULL OR a.service_epoch_id = ?))
            """
        )
        params: list = [
            ts,
            lease_until,
            ts,
            request_id,
            attempt_id,
            ts,
            current_epoch.epoch_id if current_epoch is not None else "",
        ]
        if is_acquisition:
            # Live authorization checkpoint for source-network work (§18,
            # RUN-02 rule 9): cancellation and current source/binding
            # revocation deny further lease renewals.
            sql += """
              AND NOT EXISTS (
                  SELECT 1 FROM scrape_runs r
                  WHERE r.id = scrape_requests.run_id
                    AND r.cancel_requested_at IS NOT NULL)
              AND EXISTS (
                  SELECT 1 FROM sources s
                  WHERE s.id = scrape_requests.source_id
                    AND s.desired_state = 'ENABLED'
                    AND s.administrative_state = 'NORMAL')
              AND EXISTS (
                  SELECT 1 FROM source_adapter_bindings b
                  WHERE b.id = scrape_requests.binding_id
                    AND b.desired_state = 'ENABLED'
                    AND b.administrative_state = 'NORMAL')
            """
        updated = conn.execute(sql, params)
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise _heartbeat_denial(conn, request_id, attempt_id, ts, current_epoch)
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


def _heartbeat_denial(
    conn: sqlite3.Connection,
    request_id: str,
    attempt_id: str,
    ts: str,
    current_epoch: ServiceEpoch | None,
) -> StaleOwnership:
    """Diagnose a refused heartbeat into a typed reason (RUN-07)."""
    row = conn.execute(
        "SELECT status, current_attempt_id, lease_until, request_type, run_id"
        " FROM scrape_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return StaleOwnership(request_id, "request does not exist")
    if row["status"] != "RUNNING":
        return StaleOwnership(request_id, f"request is {row['status']}")
    if row["current_attempt_id"] != attempt_id:
        return StaleOwnership(request_id, "attempt token no longer owns the request")
    if (row["lease_until"] or "") <= ts:
        return StaleOwnership(request_id, "lease already expired")
    att = conn.execute(
        "SELECT service_epoch_id FROM request_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    bound_epoch = att["service_epoch_id"] if att is not None else None
    current_id = current_epoch.epoch_id if current_epoch is not None else None
    if bound_epoch is not None and bound_epoch != current_id:
        return StaleOwnership(
            request_id, "service epoch advanced; attempt ownership invalidated"
        )
    if row["request_type"] in ACQUISITION_REQUEST_TYPES:
        run = conn.execute(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id = ?",
            (row["run_id"],),
        ).fetchone()
        if run is not None and run["cancel_requested_at"] is not None:
            return StaleOwnership(request_id, "run invalidated")
        source = conn.execute(
            "SELECT desired_state, administrative_state FROM sources WHERE id ="
            " (SELECT source_id FROM scrape_requests WHERE id = ?)",
            (request_id,),
        ).fetchone()
        binding = conn.execute(
            "SELECT desired_state, administrative_state FROM source_adapter_bindings"
            " WHERE id = (SELECT binding_id FROM scrape_requests WHERE id = ?)",
            (request_id,),
        ).fetchone()
        if (
            source is None
            or binding is None
            or source["desired_state"] != "ENABLED"
            or source["administrative_state"] != "NORMAL"
            or binding["desired_state"] != "ENABLED"
            or binding["administrative_state"] != "NORMAL"
        ):
            return StaleOwnership(
                request_id, "current source/binding authority revoked"
            )
    return StaleOwnership(request_id, "ownership verification failed")  # pragma: no cover


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
                "SELECT id, status, current_attempt_id, attempt_count, max_attempts,"
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
            _abandon_and_requeue(
                conn,
                current,
                ts=ts,
                abandoned_reason=ABANDONED_LEASE_EXPIRED,
                failure_detail="attempt budget exhausted",
            )
            conn.execute("COMMIT")
            reclaimed.append(row["id"])
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return reclaimed


def reclaim_orphaned_epoch_work(
    conn: sqlite3.Connection, epoch_id: str, *, now: str | None = None
) -> list[str]:
    """Reclaim RUNNING work whose attempts were bound to an invalidated epoch.

    Unlike lease-expiry reclaim the lease deadline is irrelevant: once the
    service epoch is invalidated (clock anomaly or rotation, §50) every
    ownership token minted under it is already lost and must not be
    extended. Prior attempts are recorded ABANDONED (``CLOCK_ANOMALY``);
    requests requeue within the attempt budget and fail terminally once it
    is exhausted.
    """
    ts = now or db_utc_now(conn)
    orphans = conn.execute(
        """
        SELECT req.id, req.current_attempt_id
        FROM scrape_requests req
        JOIN request_attempts a ON a.attempt_id = req.current_attempt_id
        WHERE req.status = 'RUNNING' AND a.service_epoch_id = ?
        ORDER BY req.id
        """,
        (epoch_id,),
    ).fetchall()
    reclaimed: list[str] = []
    for row in orphans:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT id, status, current_attempt_id, attempt_count, max_attempts"
                " FROM scrape_requests WHERE id = ?",
                (row["id"],),
            ).fetchone()
            att = (
                conn.execute(
                    "SELECT service_epoch_id FROM request_attempts"
                    " WHERE attempt_id = ?",
                    (row["current_attempt_id"],),
                ).fetchone()
                if current is not None and current["current_attempt_id"]
                else None
            )
            if (
                current is None
                or current["status"] != "RUNNING"
                or current["current_attempt_id"] != row["current_attempt_id"]
                or att is None
                or att["service_epoch_id"] != epoch_id
            ):
                conn.execute("COMMIT")
                continue
            _abandon_and_requeue(
                conn,
                current,
                ts=ts,
                abandoned_reason=ABANDONED_CLOCK_ANOMALY,
                failure_detail="attempt budget exhausted (clock anomaly)",
            )
            conn.execute("COMMIT")
            reclaimed.append(row["id"])
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return reclaimed


def _abandon_and_requeue(
    conn: sqlite3.Connection,
    current: sqlite3.Row,
    *,
    ts: str,
    abandoned_reason: str,
    failure_detail: str,
) -> str:
    """RUN-07 reclaim transition inside an already-verified write
    transaction: mark the prior attempt ABANDONED and move the request to
    RETRY_WAIT while the attempt budget remains, FAILED once exhausted.
    Returns the new request status."""
    if current["current_attempt_id"]:
        conn.execute(
            "UPDATE request_attempts SET outcome = 'ABANDONED',"
            " abandoned_reason = ?, finished_at = ?"
            " WHERE attempt_id = ?",
            (abandoned_reason, ts, current["current_attempt_id"]),
        )
    if current["attempt_count"] >= current["max_attempts"]:
        conn.execute(
            "UPDATE scrape_requests SET status = 'FAILED',"
            " last_failure_kind = 'LEASE_LOST', last_failure_json = ?,"
            " current_worker_id = NULL, current_attempt_id = NULL,"
            " lease_until = NULL, finished_at = ?, updated_at = ? WHERE id = ?",
            (
                '{"kind": "LEASE_LOST", "detail": "' + failure_detail + '"}',
                ts,
                ts,
                current["id"],
            ),
        )
        return "FAILED"
    backoff = min(
        RETRY_BACKOFF_BASE_S * max(1, current["attempt_count"]),
        RETRY_BACKOFF_CAP_S,
    )
    conn.execute(
        "UPDATE scrape_requests SET status = 'RETRY_WAIT',"
        " next_retry_at = ?, current_worker_id = NULL,"
        " current_attempt_id = NULL, lease_until = NULL, updated_at = ?"
        " WHERE id = ?",
        (_add_seconds(ts, backoff), ts, current["id"]),
    )
    return "RETRY_WAIT"


def _add_seconds(rfc3339: str, seconds: float) -> str:
    from datetime import datetime, timedelta

    from jobscraper.timeutil import parse_rfc3339, to_rfc3339

    dt: datetime = parse_rfc3339(rfc3339)
    return to_rfc3339(dt + timedelta(seconds=seconds))


__all__ = [
    "ABANDONED_CLOCK_ANOMALY",
    "ABANDONED_LEASE_EXPIRED",
    "ABANDONED_SERVICE_RESTART",
    "Claim",
    "ClaimsHalted",
    "StaleOwnership",
    "claim_next_request",
    "heartbeat",
    "reclaim_expired",
    "reclaim_orphaned_epoch_work",
]
