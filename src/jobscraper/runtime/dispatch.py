"""Service-owned HTTP dispatch seam for Slice 3.

The durable service owns claim/authorization/capacity.  The HTTP executor is
pure I/O and never touches SQLite.  This seam deliberately performs every DB
checkpoint before or after network I/O, never while a write transaction is
held (§50).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobscraper.acquisition.envelope import (
    ExecutionPlanEnvelope,
    bind_execution_plan,
)
from jobscraper.acquisition.httpexec import execute_request
from jobscraper.acquisition.pagevalidity import PageClassification, classify_page
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.net.destination import DestinationPolicy
from jobscraper.runtime.authorization import (
    AuthorizationDenied,
    require_request_authorized,
)
from jobscraper.runtime.capacity import (
    CapacityCoordinator,
    CapacityUnavailable,
    capacity_key_for_envelope,
    service_capacity_coordinator,
)
from jobscraper.runtime.rate import RateGate, RateKey, dispatch_gate, rate_key_for_envelope
from jobscraper.runtime.retry import RetryDecision, decide_retry
from jobscraper.runtime.claims import StaleOwnership
from jobscraper.runtime.clock import db_utc_now
from jobscraper.timeutil import parse_rfc3339




class UnsupportedExecutionClass(RuntimeError):
    def __init__(self, execution_class: str):
        self.execution_class = execution_class
        super().__init__(
            f"HTTP dispatcher cannot execute class {execution_class!r}"
        )


@dataclass(frozen=True)
class DispatchDeferred(RuntimeError):
    reason: str
    next_retry_at: str | None = None
    failure_kind: str | None = None

    def __str__(self) -> str:
        return f"dispatch deferred: {self.reason}"




@dataclass(frozen=True)
class DispatchRejected(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return f"dispatch rejected: {self.reason}"


@dataclass(frozen=True)
class DispatchExecutionError(RuntimeError):
    error_type: str

    def __str__(self) -> str:
        return f"dispatch executor failed locally: {self.error_type}"


@dataclass(frozen=True)
class DispatchOutcome:
    result: ResultEnvelope
    classification: PageClassification
    retry: RetryDecision
    rate_key: RateKey


def dispatch_http(
    conn: sqlite3.Connection,
    envelope: ExecutionPlanEnvelope,
    policy: DestinationPolicy,
    *,
    now: str | None = None,
    attempt_count: int,
    max_attempts: int,
    expect: str,
    detail_closure_allowed: bool = False,
    coordinator: CapacityCoordinator | None = None,
) -> DispatchOutcome:
    """Authorize, reserve, execute, classify and derive retry intent.

    No SQLite transaction is open during ``execute_request``.  Capacity is
    reserved immediately before dispatch and released in ``finally`` on every
    success/failure/exception path.  A current authorization revocation after
    plan binding but before I/O is rechecked and fails closed.
    """

    if envelope.execution_class != "HTTP":
        # Typed dispatch boundary: browser-class work must never enter the HTTP
        # executor merely to be rejected later by payload validation.
        raise UnsupportedExecutionClass(envelope.execution_class)

    # Current network authority is checked before rate/capacity deferral so a
    # revocation cannot be hidden indefinitely behind a cooldown.  The final
    # transactional checkpoint still occurs in bind_execution_plan immediately
    # before network I/O.
    require_request_authorized(
        conn,
        envelope.request_id,
        attempt_id=envelope.attempt_id,
        require_running=True,
    )
    gate_now = now or db_utc_now(conn)

    # S3.4: persisted source-rate pressure survives restart and blocks a new
    # network request without consuming provider capacity.
    try:
        rkey = rate_key_for_envelope(envelope)
    except ValueError as exc:
        raise DispatchRejected(type(exc).__name__) from exc
    gate: RateGate = dispatch_gate(conn, rkey, now=gate_now)
    if not gate.allowed:
        raise DispatchDeferred(
            reason=f"rate circuit {gate.circuit_state}",
            next_retry_at=gate.cooldown_until,
            failure_kind="RATE_LIMIT" if gate.cooldown_until else "BLOCKED",
        )

    # S3.3: service-owned capacity only.  The executor never claims work.
    owner = coordinator or service_capacity_coordinator()
    try:
        ckey = capacity_key_for_envelope(envelope)
    except ValueError as exc:
        raise DispatchRejected(type(exc).__name__) from exc
    reservation = owner.try_reserve(ckey)
    if reservation is None:
        raise CapacityUnavailable(ckey)

    try:
        # S3.2: the final bounded checkpoint before I/O atomically proves the
        # exact request/attempt/run-plan identity, current live authorization,
        # unexpired lease and current service epoch, then persists plan_id.
        try:
            bind_execution_plan(conn, envelope, now=now)
        except (AuthorizationDenied, StaleOwnership):
            raise
        except ValueError as exc:
            raise DispatchRejected(type(exc).__name__) from exc

        try:
            result = execute_request(envelope, policy)  # NO transaction held
        except ValueError as exc:
            # Host plan validation failures are local policy/integrity
            # rejections, not source/parser drift.
            raise DispatchRejected(type(exc).__name__) from exc
        except Exception as exc:
            # The executor already normalizes expected network failures into a
            # ResultEnvelope.  Anything escaping it is local infrastructure.
            raise DispatchExecutionError(type(exc).__name__) from exc
    finally:
        reservation.release()

    classification = classify_page(result, expect=expect)
    retry_now = now or db_utc_now(conn)
    retry = decide_retry(
        result.failure,
        page_class=classification.state.value,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        headers=result.headers_redacted,
        closure_is_success=(
            detail_closure_allowed
            and classification.state.value in {"NOT_FOUND", "JOB_CLOSED"}
        ),
        now=parse_rfc3339(retry_now),
    )
    return DispatchOutcome(result, classification, retry, rkey)


__all__ = [
    "DispatchDeferred",
    "DispatchExecutionError",
    "DispatchOutcome",
    "DispatchRejected",
    "UnsupportedExecutionClass",
    "dispatch_http",
]
