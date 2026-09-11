"""Durable host/binding cooldown and circuit state (03 §15, S3.4).

The table is owned by migration v15.  This module never invents egress
identity: it only consumes the host-owned identity already present in an
ExecutionPlanEnvelope.  Direct egress remains the default (R2-F6).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

from jobscraper.acquisition.envelope import ExecutionPlanEnvelope
from jobscraper.ids import new_id
from jobscraper.runtime.claims import _add_seconds


@dataclass(frozen=True)
class RateKey:
    binding_id: str
    host: str
    egress_identity: str | None = None


@dataclass(frozen=True)
class RateGate:
    allowed: bool
    cooldown_until: str | None = None
    circuit_state: str = "CLOSED"


def rate_key_for_envelope(envelope: ExecutionPlanEnvelope) -> RateKey:
    host = (urlsplit(envelope.payload.url).hostname or "").lower()
    if not host:
        raise ValueError("rate accounting requires a concrete request host")
    return RateKey(
        binding_id=envelope.binding_id,
        host=host,
        egress_identity=envelope.payload.egress_requirement,
    )


def dispatch_gate(conn: sqlite3.Connection, key: RateKey, *, now: str) -> RateGate:
    row = conn.execute(
        """
        SELECT circuit_state, cooldown_until
          FROM binding_host_rate_state
         WHERE binding_id = ? AND host = ?
           AND COALESCE(egress_identity, '') = COALESCE(?, '')
        """,
        (key.binding_id, key.host, key.egress_identity),
    ).fetchone()
    if row is None:
        return RateGate(True)
    cooldown = row["cooldown_until"]
    if cooldown and cooldown > now:
        return RateGate(False, cooldown, row["circuit_state"])
    # OPEN with no future cooldown is an administrative/manual-stop shape
    # (e.g. authentication required).  Time-bound OPEN circuits may probe once
    # their cooldown elapses and close on a clean result.
    if row["circuit_state"] == "OPEN" and not cooldown:
        return RateGate(False, None, "OPEN")
    return RateGate(True, cooldown, row["circuit_state"])


def _upsert(
    conn: sqlite3.Connection,
    key: RateKey,
    *,
    circuit_state: str,
    cooldown_until: str | None,
    failure_delta: int,
    success_delta: int,
    retry_after: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO binding_host_rate_state (
            id, binding_id, host, egress_identity, circuit_state,
            cooldown_until, recent_failure_count, recent_success_count,
            last_retry_after, last_rate_event_at, last_success_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            circuit_state = excluded.circuit_state,
            cooldown_until = excluded.cooldown_until,
            recent_failure_count = CASE
                WHEN excluded.recent_failure_count = 0 THEN 0
                ELSE binding_host_rate_state.recent_failure_count + excluded.recent_failure_count
            END,
            recent_success_count = CASE
                WHEN excluded.recent_success_count = 0 THEN binding_host_rate_state.recent_success_count
                ELSE binding_host_rate_state.recent_success_count + excluded.recent_success_count
            END,
            last_retry_after = COALESCE(excluded.last_retry_after,
                                        binding_host_rate_state.last_retry_after),
            last_rate_event_at = excluded.last_rate_event_at,
            last_success_at = COALESCE(excluded.last_success_at,
                                       binding_host_rate_state.last_success_at),
            updated_at = excluded.updated_at
        """,
        (
            new_id("rate"),
            key.binding_id,
            key.host,
            key.egress_identity,
            circuit_state,
            cooldown_until,
            int(failure_delta),
            int(success_delta),
            retry_after,
            now,
            now if success_delta else None,
            now,
        ),
    )


def record_success(
    conn: sqlite3.Connection,
    key: RateKey,
    *,
    now: str,
    commit: bool = True,
) -> None:
    # Clean success closes a timed circuit and resets the consecutive-failure
    # pressure.  Historical request_attempts/fetch evidence remains immutable.
    _upsert(
        conn,
        key,
        circuit_state="CLOSED",
        cooldown_until=None,
        failure_delta=0,
        success_delta=1,
        retry_after=None,
        now=now,
    )
    conn.execute(
        """
        UPDATE binding_host_rate_state
           SET recent_failure_count = 0, circuit_state = 'CLOSED',
               cooldown_until = NULL, last_success_at = ?, updated_at = ?
         WHERE binding_id = ? AND host = ?
           AND COALESCE(egress_identity, '') = COALESCE(?, '')
        """,
        (now, now, key.binding_id, key.host, key.egress_identity),
    )
    if commit:
        conn.commit()


def record_failure(
    conn: sqlite3.Connection,
    key: RateKey,
    *,
    failure_kind: str,
    delay_s: float | None,
    retry_after_raw: str | None,
    now: str,
    commit: bool = True,
) -> None:
    """Persist source-rate pressure without misclassifying local failures."""

    local_only = {"WORKER_CRASH", "BROWSER_CRASH", "LEASE_LOST", "CANCELLED"}
    if failure_kind in local_only:
        return

    if failure_kind in {"AUTH_REQUIRED", "POLICY_REJECTED"}:
        # Authorization/security denial is controlled by the live authority
        # plane, not a source-rate circuit.  Do not turn a policy decision
        # into an adaptive network-state heuristic.
        return

    source_pressure = failure_kind in {
        "RATE_LIMIT",
        "CHALLENGE",
        "BLOCKED",
        "HTTP_5XX",
        "CONNECT_ERROR",
        "DNS_ERROR",
        "TLS_ERROR",
        "TIMEOUT",
    }
    if not source_pressure:
        return

    cooldown_until = None if delay_s is None else _add_seconds(now, max(0.0, delay_s))
    # A transient source-pressure event without an effective delay must never
    # become an accidental permanent/manual OPEN circuit.  Timed pressure is
    # OPEN; otherwise retain counters while leaving dispatch eligible.
    circuit_state = "OPEN" if cooldown_until is not None else "CLOSED"
    _upsert(
        conn,
        key,
        circuit_state=circuit_state,
        cooldown_until=cooldown_until,
        failure_delta=1,
        success_delta=0,
        retry_after=retry_after_raw,
        now=now,
    )
    if commit:
        conn.commit()


__all__ = [
    "RateGate",
    "RateKey",
    "dispatch_gate",
    "rate_key_for_envelope",
    "record_failure",
    "record_success",
]
