"""Atomic claim, lease, heartbeat and reclaim (03 RUN-06, RUN-07, §50).

Single-winner claims via ``BEGIN IMMEDIATE``; a claim mints a fresh
attempt identity, binds it to the current service epoch, and opens a
lease. New claims fail closed when no service epoch is open
(:class:`NoActiveServiceEpoch`): every newly created attempt carries a
non-NULL ``service_epoch_id`` (nullable storage remains only for
historical/pre-S3.1 rows, which heartbeat/ownership never treat as valid
current ownership). An expired lease is *already lost ownership* even if
the reclaimer has not yet executed — a worker MUST NOT revive it
(heartbeat/commit refuse stale tokens). Reclaim records the prior attempt
as ABANDONED and requeues within the attempt budget; budget exhaustion is
terminal failure.

Claim selection is fully evaluated in the selection query: an eligible
request can never be hidden behind blocked/cancelled/disabled/quarantined
or not-yet-due rows, and ``RETRY_WAIT`` is claimable only with a valid
durable ``next_retry_at`` that is due (NULL/empty retry timestamps fail
closed rather than meaning "immediately runnable"). Deterministic order
(priority DESC, created_at ASC, id ASC) and atomic single-winner
transition are preserved.

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
epoch's RUNNING work regardless of lease deadline — but it refuses to
reclaim the current live epoch. Claim and heartbeat transactions run
against the database only — no network/browser/file I/O while holding
them (§50).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobscraper.ids import new_id
from jobscraper.runtime.authorization import (
    AuthorizationDenied,
    acquisition_claim_sql_predicate,
    evaluate_request_authorization,
    require_request_authorized,
)
from jobscraper.runtime.clock import (
    NoActiveServiceEpoch,
    ServiceClockGuard,
    ServiceEpoch,
    StaleServiceEpoch,
    current_service_epoch,
    db_utc_now,
)
from jobscraper.runtime.requests import (
    ACQUISITION_REQUEST_TYPES,
    HOST_NATIVE_REQUEST_TYPES,
)

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
    service_epoch_id: str = ""  # new claims are always epoch-bound (§50)


def _resolve_binding_epoch(
    conn: sqlite3.Connection, epoch: ServiceEpoch | None
) -> ServiceEpoch:
    """Resolve the active service epoch inside the claim transaction.

    New claims fail closed without an open epoch (:class:`NoActiveServiceEpoch`):
    attempts must always be bound to the lifetime that minted them. An
    explicit epoch must still be the current one, otherwise it is stale.
    """
    current = current_service_epoch(conn)
    if current is None:
        raise NoActiveServiceEpoch(
            "claim refused: no active service epoch (begin_service_epoch must"
            " run before claiming; §50)"
        )
    if epoch is not None and current.epoch_id != epoch.epoch_id:
        raise StaleServiceEpoch(
            f"service epoch {epoch.epoch_id} is not the current service epoch",
            epoch_id=epoch.epoch_id,
        )
    return current


def _claim_selection_sql(
    types: frozenset[str] | None, run_source_plan_id: str | None
) -> tuple[str, list]:
    """Deterministic claimable-request selection, fully evaluated in SQL.

    Eligibility lives in the query itself (never in a bounded post-filter
    window), so an eligible request cannot be starved by any number of
    blocked/cancelled/disabled/quarantined or not-yet-due rows ahead of it.
    """
    acq_ph = ", ".join("?" for _ in sorted(ACQUISITION_REQUEST_TYPES))
    native_ph = ", ".join("?" for _ in sorted(HOST_NATIVE_REQUEST_TYPES))
    live_acq = acquisition_claim_sql_predicate()
    sql = f"""
        SELECT req.id
        FROM scrape_requests req
        JOIN scrape_runs run ON run.id = req.run_id
        JOIN sources s ON s.id = req.source_id
        JOIN source_adapter_bindings b ON b.id = req.binding_id
        WHERE req.status IN ('PENDING', 'RETRY_WAIT')
          AND (
                req.status = 'PENDING'
                OR (
                     req.next_retry_at IS NOT NULL
                     AND req.next_retry_at <> ''
                     AND req.next_retry_at <= ?
                   )
              )
          AND (
                req.request_type IN ({native_ph})
                OR (
                     req.request_type IN ({acq_ph})
                     {live_acq}
                   )
              )
    """
    params: list = [None]  # retry-due timestamp bound below
    params.extend(sorted(HOST_NATIVE_REQUEST_TYPES))
    params.extend(sorted(ACQUISITION_REQUEST_TYPES))
    if types:
        sql += (
            " AND req.request_type IN ("
            + ", ".join("?" for _ in sorted(types))
            + ")"
        )
        params.extend(sorted(types))
    if run_source_plan_id:
        sql += " AND req.run_source_plan_id = ?"
        params.append(run_source_plan_id)
    sql += " ORDER BY req.priority DESC, req.created_at ASC, req.id ASC LIMIT 1"
    return sql, params


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
    service epoch. When a :class:`ServiceClockGuard` is supplied, every claim
    first observes the database clock through it: a material wall-clock
    anomaly rotates the epoch (rotation runs its own serialized write
    transaction before the claim transaction begins), and the guarded claim
    then performs the §50 recovery inline — reclaiming the ORIGINAL
    invalidated epoch's orphaned RUNNING work and resuming only after that
    recovery completes, inside the same serialized claim write transaction —
    so the production claim path can never bypass anomaly detection. No
    network or file I/O occurs while the claim transaction is held.
    """
    ts = now or db_utc_now(conn)
    if guard is not None:
        # Guarded claim (§50): observe BEFORE opening the claim transaction,
        # because anomaly rotation ends the live epoch and opens the next
        # one in its own BEGIN IMMEDIATE.
        guard.observe(conn, db_now=ts)
    attempt_id = new_id("att")
    selection_sql, selection_params = _claim_selection_sql(types, run_source_plan_id)
    selection_params[0] = ts  # RETRY_WAIT due comparison uses DB time (RUN-20)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if guard is not None:
            if guard.claims_halted:
                # §50, inside the same serialized write transaction: recover
                # BEFORE any new claim can proceed — reclaim the ORIGINAL
                # invalidated epoch's orphaned RUNNING work (rotation ended
                # it durably), then resume. Claims therefore halt only until
                # recovery is complete, and no coordinator can bypass the
                # anomaly on the guarded production path.
                halt = guard.halt
                if halt is not None:
                    _reclaim_epoch_orphans(
                        conn, halt.invalidated_epoch_id, ts,
                        failure_detail="attempt budget exhausted (clock anomaly)",
                    )
                guard.clear_halt()
        bound_epoch = _resolve_binding_epoch(conn, epoch)
        chosen = conn.execute(selection_sql, selection_params).fetchone()
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
                bound_epoch.epoch_id,
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
        service_epoch_id=bound_epoch.epoch_id,
    )


def heartbeat(
    conn: sqlite3.Connection,
    request_id: str,
    attempt_id: str,
    *,
    now: str | None = None,
    lease_window_s: float = DEFAULT_LEASE_WINDOW_S,
    epoch: ServiceEpoch | None = None,
    guard: ServiceClockGuard | None = None,
) -> str:
    """Renew the lease; refused for stale tokens, expired leases or stale epochs.

    Accepted only when the request is RUNNING under this exact attempt, the
    lease is unexpired by database time, an active service epoch is open and
    the attempt is bound to it (historical NULL-epoch attempts are never
    valid current ownership), and — for acquisition work — the run is not
    cancelled and current source/binding authority still holds (RUN-02 rule
    9, §18). Host-native heartbeats are independent of the source-network
    predicate (R2-F3): accepted local obligations keep draining.

    When a service-lifetime clock guard is supplied, the heartbeat observes
    the database clock before opening its renewal transaction. A material
    wall-clock anomaly therefore rotates the service epoch first; this stale
    attempt then fails the epoch fence and cannot extend its lease (§50).
    Recovery of the invalidated epoch remains the coordinator/claim path's
    responsibility.
    """
    ts = now or db_utc_now(conn)
    if guard is not None:
        # Observe before BEGIN IMMEDIATE because anomaly rotation performs its
        # own serialized write transaction. The resulting fresh epoch makes
        # this old attempt stale before any renewal can be persisted (§50).
        guard.observe(conn, db_now=ts)
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
        if current_epoch is None:
            # Fail closed: with no active service epoch there is no current
            # ownership to renew (§50); historical attempts are not revived.
            conn.execute("ROLLBACK")
            raise StaleOwnership(request_id, "no active service epoch")
        if is_acquisition:
            try:
                require_request_authorized(
                    conn,
                    request_id,
                    attempt_id=attempt_id,
                    require_running=True,
                )
            except AuthorizationDenied as exc:
                conn.execute("ROLLBACK")
                raise StaleOwnership(
                    request_id,
                    f"current acquisition authority revoked: {exc.decision.reason.value}",
                ) from exc
        sql = (
            """
            UPDATE scrape_requests
            SET heartbeat_at = ?, lease_until = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING' AND current_attempt_id = ?
              AND lease_until > ?
              AND EXISTS (
                  SELECT 1 FROM request_attempts a
                  WHERE a.attempt_id = scrape_requests.current_attempt_id
                    AND a.service_epoch_id = ?)
            """
        )
        params: list = [
            ts,
            lease_until,
            ts,
            request_id,
            attempt_id,
            ts,
            current_epoch.epoch_id,
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
    if bound_epoch != current_id:
        # Includes historical NULL-bound attempts: never valid current
        # ownership once an epoch discipline exists (§50).
        return StaleOwnership(
            request_id, "service epoch advanced; attempt ownership invalidated"
        )
    if row["request_type"] in ACQUISITION_REQUEST_TYPES:
        decision = evaluate_request_authorization(
            conn,
            request_id,
            attempt_id=attempt_id,
            require_running=True,
        )
        if not decision.allowed:
            return StaleOwnership(
                request_id,
                f"current acquisition authority revoked: {decision.reason.value}",
            )
        run = conn.execute(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id = ?", (row["run_id"],)
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


def _validate_reclaimable_epoch(conn: sqlite3.Connection, epoch_id: str) -> None:
    """Only an existing, durably ended/invalidated, non-current epoch may be
    reclaimed (§50). The live epoch and unknown/open epochs are refused."""
    current = current_service_epoch(conn)
    if current is not None and current.epoch_id == epoch_id:
        raise ValueError(
            f"refusing to reclaim the current live service epoch {epoch_id}:"
            " only ended/invalidated epochs may be reclaimed (§50)"
        )
    row = conn.execute(
        "SELECT ended_at FROM service_clock_epochs WHERE id = ?", (epoch_id,)
    ).fetchone()
    if row is None:
        raise ValueError(
            f"refusing to reclaim unknown service epoch {epoch_id}: only"
            " existing, durably ended epochs may be reclaimed (§50)"
        )
    if row["ended_at"] is None:
        raise ValueError(
            f"refusing to reclaim service epoch {epoch_id}: it is not durably"
            " ended/invalidated (ended_at IS NULL); only ended epochs may be"
            " reclaimed (§50)"
        )


def _reclaim_epoch_orphans(
    conn: sqlite3.Connection,
    epoch_id: str,
    ts: str,
    *,
    failure_detail: str = "attempt budget exhausted (clock anomaly)",
) -> list[str]:
    """Abandon/requeue the RUNNING work bound to ``epoch_id``. The caller
    owns the enclosing write transaction (single serialized boundary)."""
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
            continue
        _abandon_and_requeue(
            conn,
            current,
            ts=ts,
            abandoned_reason=ABANDONED_CLOCK_ANOMALY,
            failure_detail=failure_detail,
        )
        reclaimed.append(row["id"])
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

    Only an existing epoch with ``ended_at IS NOT NULL`` may be reclaimed:
    the current live epoch, unknown ids and not-yet-ended epochs are refused
    with :class:`ValueError`. Guarded claims perform this same recovery
    inline inside their serialized transaction after a clock-anomaly
    rotation (§50).
    """
    _validate_reclaimable_epoch(conn, epoch_id)
    ts = now or db_utc_now(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        reclaimed = _reclaim_epoch_orphans(conn, epoch_id, ts)
        conn.execute("COMMIT")
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
    "StaleOwnership",
    "claim_next_request",
    "heartbeat",
    "reclaim_expired",
    "reclaim_orphaned_epoch_work",
]
