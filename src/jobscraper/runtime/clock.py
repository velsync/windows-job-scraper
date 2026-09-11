"""Durable clock and service epochs for the runtime core (RUN-20, §50).

Lease and scheduling comparisons use one consistent UTC source: the
database's own clock. ``db_utc_now`` reads SQLite's ``strftime`` and
normalizes it to the repository's canonical RFC-3339 microsecond format so
durable timestamps written by the runtime compare lexicographically with
``timeutil`` output.

§50 lease correctness additionally uses a *service clock epoch*. Every
service lifetime opens an epoch row; attempts claimed under it are bound to
it. ``begin_service_epoch`` is the startup entry point: it records any
still-open epoch as ended by ``SERVICE_RESTART`` (only a dead process can
leave one open, because the service is the single durable claim/capacity
coordinator — RUN-09) and opens the fresh epoch.

``ServiceClockGuard`` checkpoints the database clock against a monotonic
elapsed baseline. If the wall clock drifts beyond the configured tolerance
in either direction it is a material anomaly: the guard ends the current
epoch with a durable anomaly record, opens a fresh epoch, halts new claims
and thereby invalidates all in-memory ownership — rather than extending an
already elapsed lease. Monotonic time governs only the live elapsed
comparison; every durable comparison remains database UTC (RUN-20).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from jobscraper.ids import new_id
from jobscraper.timeutil import parse_rfc3339

DEFAULT_CLOCK_ANOMALY_TOLERANCE_S = 30.0

END_REASON_SERVICE_RESTART = "SERVICE_RESTART"
END_REASON_CLOCK_ANOMALY_FORWARD = "CLOCK_ANOMALY_FORWARD"
END_REASON_CLOCK_ANOMALY_BACKWARD = "CLOCK_ANOMALY_BACKWARD"


def db_utc_now(conn: sqlite3.Connection) -> str:
    """The database's UTC now, normalized to RFC-3339 with microseconds."""
    value = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%f', 'now')"
    ).fetchone()[0]
    # '2026-09-08T08:00:00.123' -> '2026-09-08T08:00:00.123000Z'
    if "." not in value:
        return value + ".000000Z"
    head, frac = value.split(".", 1)
    return f"{head}.{frac[:6].ljust(6, '0')}Z"


@dataclass(frozen=True)
class ServiceEpoch:
    """One durable service-clock epoch (``service_clock_epochs`` row)."""

    epoch_id: str
    started_at: str


class StaleServiceEpoch(Exception):
    """The supplied service epoch is ended or not the current epoch."""

    def __init__(self, message: str, epoch_id: str | None = None):
        super().__init__(message)
        self.epoch_id = epoch_id


class NoActiveServiceEpoch(Exception):
    """No service epoch is currently open; ownership operations fail closed.

    New claims must always bind to an active service epoch (§50). A
    database with no open epoch has had no ``begin_service_epoch`` for this
    service lifetime, so claim/heartbeat refuse rather than mint unbound
    ownership. Nullable ``request_attempts.service_epoch_id`` remains only
    for historical/pre-S3.1 rows, never for newly created attempts.
    """


@dataclass(frozen=True)
class ClockAnomaly:
    """A material wall-clock anomaly detected by ``ServiceClockGuard``."""

    direction: str  # "FORWARD" | "BACKWARD"
    drift_s: float
    invalidated_epoch_id: str
    new_epoch_id: str
    observed_db_now: str


def open_service_epoch(conn: sqlite3.Connection, *, now: str | None = None) -> ServiceEpoch:
    """Open a new service epoch without ending any open one.

    Normal startup uses :func:`begin_service_epoch`; this low-level form
    exists for callers that already reconciled open epochs themselves.
    """
    ts = now or db_utc_now(conn)
    epoch_id = new_id("epoch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO service_clock_epochs (id, started_at, created_at)"
            " VALUES (?, ?, ?)",
            (epoch_id, ts, ts),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return ServiceEpoch(epoch_id=epoch_id, started_at=ts)


def current_service_epoch(conn: sqlite3.Connection) -> ServiceEpoch | None:
    """The current (still open) service epoch, or ``None`` before one opens."""
    row = conn.execute(
        "SELECT id, started_at FROM service_clock_epochs"
        " WHERE ended_at IS NULL ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return ServiceEpoch(epoch_id=row["id"], started_at=row["started_at"])


def end_service_epoch(
    conn: sqlite3.Connection,
    epoch_id: str,
    *,
    reason: str,
    now: str | None = None,
) -> bool:
    """End an open epoch with a durable reason; ``False`` if already ended."""
    ts = now or db_utc_now(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE service_clock_epochs SET ended_at = ?, end_reason = ?"
            " WHERE id = ? AND ended_at IS NULL",
            (ts, reason, epoch_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount == 1


def begin_service_epoch(conn: sqlite3.Connection, *, now: str | None = None) -> ServiceEpoch:
    """Start a fresh service epoch for this service lifetime.

    Any still-open epoch can only be residue of a dead process (RUN-09: the
    service is the single durable claim/capacity coordinator), so it is
    recorded as ended by ``SERVICE_RESTART``. Restart recovery runs under the
    fresh epoch; ownership from prior epochs is invalid from this moment on.
    """
    ts = now or db_utc_now(conn)
    epoch_id = new_id("epoch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE service_clock_epochs SET ended_at = ?,"
            " end_reason = ? WHERE ended_at IS NULL",
            (ts, END_REASON_SERVICE_RESTART),
        )
        conn.execute(
            "INSERT INTO service_clock_epochs (id, started_at, created_at)"
            " VALUES (?, ?, ?)",
            (epoch_id, ts, ts),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return ServiceEpoch(epoch_id=epoch_id, started_at=ts)


def _delta_seconds(earlier: str, later: str) -> float:
    return (parse_rfc3339(later) - parse_rfc3339(earlier)).total_seconds()


class ServiceClockGuard:
    """Wall-clock anomaly detector for the service claim/coordinator loop (§50).

    The guard checkpoints the database UTC clock against a monotonic elapsed
    baseline. A drift beyond ``tolerance_s`` in either direction is a
    material anomaly:

    * the current epoch is ended with ``CLOCK_ANOMALY_FORWARD`` /
      ``CLOCK_ANOMALY_BACKWARD`` (the durable anomaly record) and a fresh
      epoch is opened;
    * new claims halt (:attr:`claims_halted`) until the coordinator reclaims
      the invalidated epoch's orphaned work and calls :meth:`clear_halt`;
    * the in-memory epoch handle advances, so ownership tokens minted under
      the old epoch are stale from the guard's perspective and durably
      rejected by the epoch gate in claim/heartbeat.

    While halted, further anomalies do NOT rotate again: the original
    invalidated epoch is retained until its recovery/reclaim completes, so
    repeated anomalies cannot overwrite the epoch still requiring recovery.

    The injected ``monotonic`` source governs only the live elapsed
    comparison; durable lease/scheduling comparisons remain database UTC
    (RUN-20).
    """

    def __init__(
        self,
        epoch: ServiceEpoch,
        *,
        tolerance_s: float = DEFAULT_CLOCK_ANOMALY_TOLERANCE_S,
        monotonic=time.monotonic,
    ):
        self._epoch = epoch
        self._tolerance_s = float(tolerance_s)
        self._monotonic = monotonic
        self._last_db_now: str | None = None
        self._last_monotonic: float | None = None
        self._halt: ClockAnomaly | None = None

    @property
    def epoch(self) -> ServiceEpoch:
        """The current in-memory epoch (advances on anomaly rotation)."""
        return self._epoch

    @property
    def halt(self) -> ClockAnomaly | None:
        return self._halt

    @property
    def claims_halted(self) -> bool:
        return self._halt is not None

    def clear_halt(self) -> None:
        """Re-enable claims after the coordinator reclaimed orphaned work.

        Also re-baselines the clock checkpoint: across a halt/reclaim the
        wall-clock baseline is unreliable, so the next observation starts a
        fresh comparison window instead of judging drift against a
        pre-anomaly sample.
        """
        self._halt = None
        self._last_db_now = None
        self._last_monotonic = None

    def monotonic_elapsed_since_observation(self) -> float | None:
        """Live (non-durable) seconds since the last checkpoint, or ``None``
        before the first observation. Suitable for heartbeat cadence."""
        if self._last_monotonic is None:
            return None
        return self._monotonic() - self._last_monotonic

    def observe(
        self, conn: sqlite3.Connection, *, db_now: str | None = None,
        owns_empty_transaction: bool = False,
    ) -> ClockAnomaly | None:
        """Checkpoint the database clock; rotate the epoch on material drift.

        Returns the detected :class:`ClockAnomaly`, or ``None`` when the
        clock moved within tolerance.

        ``owns_empty_transaction`` is only for the claim/heartbeat preflight:
        the caller acquired BEGIN IMMEDIATE but has made no mutations. On a
        new anomaly, rotation commits that transaction before updating the
        in-memory epoch. The caller must reacquire and resample before work.
        Without rotation the transaction remains open for ownership checks.
        """
        if owns_empty_transaction and not conn.in_transaction:
            raise ValueError("clock preflight requires BEGIN IMMEDIATE")
        now = db_now or db_utc_now(conn)
        mono = self._monotonic()
        direction: str | None = None
        drift = 0.0
        if self._last_db_now is not None:
            wall_s = _delta_seconds(self._last_db_now, now)
            elapsed_s = mono - self._last_monotonic
            forward_drift = wall_s - elapsed_s
            backward_drift = elapsed_s - wall_s
            if forward_drift > self._tolerance_s:
                direction, drift = "FORWARD", forward_drift
            elif backward_drift > self._tolerance_s:
                direction, drift = "BACKWARD", backward_drift
        self._last_db_now, self._last_monotonic = now, mono
        if direction is None:
            return None
        if self._halt is not None:
            # Already halted on an earlier anomaly whose recovery/reclaim may
            # still be pending: retain the ORIGINAL invalidated epoch and do
            # not rotate a new one over it. Claims stay halted; the anomaly
            # checkpoint keeps re-baselining so post-halt observations are
            # judged from fresh samples (§50).
            return self._halt
        return self._rotate(conn, direction, drift, now, owns_empty_transaction=owns_empty_transaction)

    def _rotate(
        self, conn: sqlite3.Connection, direction: str, drift_s: float, now: str,
        *, owns_empty_transaction: bool = False,
    ) -> ClockAnomaly:
        reason = (
            END_REASON_CLOCK_ANOMALY_FORWARD
            if direction == "FORWARD"
            else END_REASON_CLOCK_ANOMALY_BACKWARD
        )
        new_epoch_id = new_id("epoch")
        if not owns_empty_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE service_clock_epochs SET ended_at = ?, end_reason = ?"
                " WHERE id = ? AND ended_at IS NULL",
                (now, reason, self._epoch.epoch_id),
            )
            conn.execute(
                "INSERT INTO service_clock_epochs (id, started_at, created_at)"
                " VALUES (?, ?, ?)",
                (new_epoch_id, now, now),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        invalidated = self._epoch.epoch_id
        self._epoch = ServiceEpoch(epoch_id=new_epoch_id, started_at=now)
        self._halt = ClockAnomaly(
            direction=direction,
            drift_s=drift_s,
            invalidated_epoch_id=invalidated,
            new_epoch_id=new_epoch_id,
            observed_db_now=now,
        )
        return self._halt


__all__ = [
    "DEFAULT_CLOCK_ANOMALY_TOLERANCE_S",
    "END_REASON_CLOCK_ANOMALY_BACKWARD",
    "END_REASON_CLOCK_ANOMALY_FORWARD",
    "END_REASON_SERVICE_RESTART",
    "ClockAnomaly",
    "NoActiveServiceEpoch",
    "ServiceClockGuard",
    "ServiceEpoch",
    "StaleServiceEpoch",
    "begin_service_epoch",
    "current_service_epoch",
    "db_utc_now",
    "end_service_epoch",
    "open_service_epoch",
]
