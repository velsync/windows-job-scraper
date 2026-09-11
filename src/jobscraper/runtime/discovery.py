"""Durable first-probe source discovery (02 §12.1 corrective).

The authoritative acquisition spec forbids discovery as pre-queue I/O. This
module gives the first careers-page probe the same durable authority boundary
as ordinary acquisition work without pulling ROAD-04 generic crawling into
Slice 2:

* create/reuse a provisional Source and immutable ``generic_discovery`` binding;
* persist a pinned RunSourcePlan and ``SOURCE_DISCOVERY`` request before I/O;
* claim the request with the normal lease machinery;
* plan through the manifest-validated discovery adapter and execute under the
  host-owned destination policy;
* atomically fence fetch/result/page-validity evidence, the pure fingerprint +
  route decision, and the discovery plan/run terminal state.

This is source classification only. It deliberately creates no enumeration
coverage, parse attempt, job observation or generic HTML crawl.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from jobscraper.acquisition.envelope import (
    ExecutionPlanEnvelope,
    RequestPlan,
    policy_snapshot_reference,
)
from jobscraper.acquisition.failures import FailureKind, FailureRecord
from jobscraper.acquisition.pagevalidity import PageClass, classify_page
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import AdapterTask, AdapterTaskKind, PlanningContext
from jobscraper.adapters.fingerprint import AtsFingerprint, classify_content
from jobscraper.adapters.registry import build_adapter
from jobscraper.adapters.router import RouteDecision, plan_routes
from jobscraper.ids import new_id
from jobscraper.pipeline.driver import (
    _persist_fetch_attempt,
    _record_evidence,
    source_policy,
)
from jobscraper.runtime.authorization import (
    AuthorizationDenied,
    AuthorizationReason,
    finalize_authorization_denial,
)
from jobscraper.runtime.cancellation import abandon_request_for_cancellation
from jobscraper.runtime.claim_control import yield_unstarted_claim
from jobscraper.runtime.claims import StaleOwnership, claim_next_request, reclaim_expired
from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.dispatch import (
    CapacityUnavailable,
    DispatchDeferred,
    DispatchExecutionError,
    DispatchRejected,
    UnsupportedExecutionClass,
    dispatch_http,
)
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.rate import (
    record_failure as record_rate_failure,
)
from jobscraper.runtime.rate import (
    record_success as record_rate_success,
)
from jobscraper.runtime.retry import RetryAction
from jobscraper.runtime.provisioning import (
    ensure_builtin_adapter_definition,
    provision_source_and_binding,
    record_fingerprint,
    record_route_decision,
)
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.retry import RetryAction, decide_retry
from jobscraper.runtime.runs import create_run, mark_run_started

_DISCOVERY_ADAPTER_ID = "generic_discovery"
_DISCOVERY_STRATEGY = "GENERIC_DISCOVERY"
_DISCOVERY_EXECUTION_CLASS = "HTTP"
_VALID_DISCOVERY_CLASSES = frozenset(
    {PageClass.VALID_LIST, PageClass.VALID_JOB, PageClass.EMPTY}
)


class DiscoveryError(RuntimeError):
    """The durable discovery request cannot be queued or executed safely."""


@dataclass(frozen=True)
class QueuedDiscovery:
    run_id: str
    run_source_plan_id: str
    request_id: str
    source_id: str
    binding_id: str
    binding_revision_id: str


@dataclass(frozen=True)
class DiscoveryOutcome:
    queued: QueuedDiscovery
    result: ResultEnvelope
    page_class: PageClass
    fingerprint: AtsFingerprint | None
    decision: RouteDecision | None
    fingerprint_id: str | None
    route_decision_id: str | None
    run_status: str


def _revision_plan(conn: sqlite3.Connection, binding_revision_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT r.*, d.adapter_api_version
        FROM source_adapter_binding_revisions r
        JOIN adapter_definitions d
          ON d.adapter_id = r.adapter_id
         AND d.adapter_version = r.adapter_version
        WHERE r.id = ?
        """,
        (binding_revision_id,),
    ).fetchone()
    if row is None:
        raise DiscoveryError("discovery binding revision is missing")
    return row


def queue_source_discovery(
    conn: sqlite3.Connection,
    *,
    display_name: str,
    entry_url: str,
    source_family: str = "EMPLOYER_CAREERS",
    canonical_host: str | None = None,
    now: str | None = None,
    timeout_s: float = 10.0,
    max_bytes: int = 1_000_000,
) -> QueuedDiscovery:
    """Persist all authority for a first careers probe, without doing I/O.

    A successful return is the §12.1 precondition for a network probe: Source,
    immutable binding revision, pinned run plan and SOURCE_DISCOVERY request are
    already committed. The run, plan and request are committed as one durable
    creation boundary, so a crash cannot expose a QUEUED discovery run without
    its work item. Invalid URLs/configuration fail during provisioning; this
    function never calls the network executor.
    """
    definition = ensure_builtin_adapter_definition(
        conn, _DISCOVERY_ADAPTER_ID, now=now
    )
    if not definition.verified:
        raise DiscoveryError(
            f"generic discovery adapter definition conflict: {definition.conflict_reason}"
        )
    provisioned = provision_source_and_binding(
        conn,
        display_name=display_name,
        source_family=source_family,
        entry_url=entry_url,
        canonical_host=canonical_host,
        adapter_id=_DISCOVERY_ADAPTER_ID,
        adapter_version=definition.adapter_version,
        strategy=_DISCOVERY_STRATEGY,
        execution_class=_DISCOVERY_EXECUTION_CLASS,
        config={"timeout_s": timeout_s, "max_bytes": max_bytes},
        now=now,
    )
    revision = _revision_plan(conn, provisioned.binding_revision_id)
    if revision["adapter_id"] != _DISCOVERY_ADAPTER_ID:
        raise DiscoveryError("provisioned discovery revision changed adapter identity")
    plan = {
        "source_id": provisioned.source_id,
        "source_plan_group_id": f"discovery-{provisioned.source_id}",
        "fallback_rank": 0,
        "binding_id": provisioned.binding_id,
        "binding_revision_id": provisioned.binding_revision_id,
        "adapter_id": revision["adapter_id"],
        "adapter_version": revision["adapter_version"],
        "adapter_api_version": revision["adapter_api_version"],
        "strategy": revision["strategy"],
        "execution_class": revision["execution_class"],
        "permission_profile_id": revision["permission_profile_id"],
        "permission_profile_revision": revision["permission_profile_revision"],
        "cursor_schema_version": 1,
    }
    try:
        run_id, plan_ids = create_run(
            conn, profile_id=None, plans=[plan], now=now, commit=False
        )
        plan_id = plan_ids[0]
        request_id, created = enqueue_request(
            conn,
            run_id=run_id,
            run_source_plan_id=plan_id,
            source_id=provisioned.source_id,
            binding_id=provisioned.binding_id,
            request_type="SOURCE_DISCOVERY",
            target_identity=entry_url,
            payload={"target_reference": entry_url},
            strategy=_DISCOVERY_STRATEGY,
            execution_class=_DISCOVERY_EXECUTION_CLASS,
            max_attempts=3,
            now=now,
            commit=False,
        )
        if not created:
            raise DiscoveryError("new discovery run unexpectedly reused a request")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return QueuedDiscovery(
        run_id=run_id,
        run_source_plan_id=plan_id,
        request_id=request_id,
        source_id=provisioned.source_id,
        binding_id=provisioned.binding_id,
        binding_revision_id=provisioned.binding_revision_id,
    )


_DISCOVERY_RESUME_SQL = """
SELECT req.id AS request_id, req.run_id AS run_id,
       req.run_source_plan_id AS run_source_plan_id,
       req.source_id AS source_id, req.binding_id AS request_binding_id,
       req.status AS status,
       plan.binding_id AS plan_binding_id,
       plan.binding_revision_id AS binding_revision_id,
       plan.source_id AS plan_source_id
FROM scrape_requests req
JOIN run_source_plans plan
  ON plan.id = req.run_source_plan_id AND plan.run_id = req.run_id
WHERE req.request_type = 'SOURCE_DISCOVERY'
"""

# A resumed probe must still be claimable. RUNNING is refused rather than
# silently retried: 03 §18/RUN-07 makes restart reclamation
# (`recover_interrupted_requests`) the single authority for orphaned
# ownership, and it must run under the fresh service epoch before any
# continuation is attempted.
_CLAIMABLE_DISCOVERY_STATUSES = frozenset({"PENDING", "RETRY_WAIT"})


def load_queued_discovery(
    conn: sqlite3.Connection,
    *,
    request_id: str | None = None,
    run_id: str | None = None,
) -> QueuedDiscovery:
    """Re-derive a durable first-probe continuation from committed rows.

    02 §12.1 requires that "crash/restart resumes from that durable identity".
    A dead process holds no Python objects, so the continuation must be
    reconstructible from what was committed. Exactly one of ``request_id`` or
    ``run_id`` identifies the work; the pinned plan, the provisional Source and
    the binding revision are re-validated against the request before any I/O is
    planned, so a half-written or crossed identity is refused rather than
    executed against the wrong source.
    """
    if (request_id is None) == (run_id is None):
        raise DiscoveryError("exactly one of request_id or run_id is required")
    if request_id is not None:
        row = conn.execute(
            _DISCOVERY_RESUME_SQL + " AND req.id = ?", (request_id,)
        ).fetchone()
    else:
        rows = conn.execute(
            _DISCOVERY_RESUME_SQL + " AND req.run_id = ?", (run_id,)
        ).fetchall()
        if len(rows) != 1:
            raise DiscoveryError(
                f"run {run_id!r} does not hold exactly one SOURCE_DISCOVERY request"
            )
        row = rows[0]
    if row is None:
        raise DiscoveryError("no durable SOURCE_DISCOVERY request matches")
    if row["status"] not in _CLAIMABLE_DISCOVERY_STATUSES:
        raise DiscoveryError(
            f"SOURCE_DISCOVERY request {row['request_id']!r} is {row['status']};"
            " a restart must run recovery before resuming"
        )
    if (
        row["request_binding_id"] != row["plan_binding_id"]
        or row["source_id"] != row["plan_source_id"]
    ):
        raise DiscoveryError(
            "SOURCE_DISCOVERY request and pinned plan disagree on binding/source"
        )
    revision = _revision_plan(conn, row["binding_revision_id"])
    if revision["adapter_id"] != _DISCOVERY_ADAPTER_ID:
        raise DiscoveryError("resumed discovery revision changed adapter identity")
    return QueuedDiscovery(
        run_id=row["run_id"],
        run_source_plan_id=row["run_source_plan_id"],
        request_id=row["request_id"],
        source_id=row["source_id"],
        binding_id=row["plan_binding_id"],
        binding_revision_id=row["binding_revision_id"],
    )


def _plan_request(
    conn: sqlite3.Connection,
    queued: QueuedDiscovery,
    claim,
) -> tuple[sqlite3.Row, sqlite3.Row, RequestPlan, ExecutionPlanEnvelope]:
    plan_row = conn.execute(
        "SELECT * FROM run_source_plans WHERE id = ? AND run_id = ?",
        (queued.run_source_plan_id, queued.run_id),
    ).fetchone()
    if plan_row is None:
        raise DiscoveryError("pinned discovery RunSourcePlan is missing")
    source = conn.execute(
        "SELECT * FROM sources WHERE id = ?", (queued.source_id,)
    ).fetchone()
    if source is None:
        raise DiscoveryError("provisional discovery Source is missing")
    revision = _revision_plan(conn, queued.binding_revision_id)
    try:
        config: Any = json.loads(revision["config_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise DiscoveryError("discovery binding config is not valid JSON") from exc
    adapter = build_adapter(plan_row["adapter_id"], config)
    task = AdapterTask(kind=AdapterTaskKind.DISCOVER, payload=dict(claim.payload or {}))
    planning_ctx = PlanningContext(
        run_id=queued.run_id,
        run_source_plan_id=queued.run_source_plan_id,
        source_snapshot_ref=f"source://{queued.source_id}",
        binding_revision_id=queued.binding_revision_id,
        permission_profile_revision=plan_row["permission_profile_revision"],
        cursor_schema_version=plan_row["cursor_schema_version"],
    )
    try:
        request_plan = adapter.plan(task, None, ctx=planning_ctx)
    except (TypeError, ValueError) as exc:
        raise DiscoveryError(f"generic discovery plan refused: {exc}") from exc
    envelope = ExecutionPlanEnvelope(
        plan_id=new_id("plan"),
        request_id=claim.request_id,
        attempt_id=claim.attempt_id,
        run_id=queued.run_id,
        run_source_plan_id=queued.run_source_plan_id,
        source_id=queued.source_id,
        binding_id=queued.binding_id,
        binding_revision_id=queued.binding_revision_id,
        adapter_id=plan_row["adapter_id"],
        adapter_version=plan_row["adapter_version"],
        strategy=plan_row["strategy"],
        execution_class=plan_row["execution_class"],
        policy_snapshot_ref=policy_snapshot_reference(plan_row),
        permission_profile_id=plan_row["permission_profile_id"],
        permission_profile_revision=plan_row["permission_profile_revision"],
        payload_kind="REQUEST",
        payload=request_plan,
    )
    return plan_row, source, request_plan, envelope


def _finalize_discovery_run_under_fence(
    conn: sqlite3.Connection,
    queued: QueuedDiscovery,
    *,
    has_fingerprint: bool,
    now: str,
) -> str:
    """Persist the one-probe plan/run terminal state without committing.

    The caller is the request ownership fence.  A discovery run is intentionally
    one immutable plan plus one ``SOURCE_DISCOVERY`` request; if that shape has
    changed, refuse the terminalization so the entire fenced transaction rolls
    back rather than guessing an aggregate for unexpected work.

    Keeping these writes inside the same transaction as the request terminal
    transition closes the crash window where the request could be ``SUCCEEDED``
    but its plan/run still be open and therefore no longer resumable.
    """
    shape = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM run_source_plans WHERE run_id = ?) AS plans,
          (SELECT COUNT(*) FROM scrape_requests WHERE run_id = ?) AS requests,
          (SELECT COUNT(*) FROM scrape_requests
             WHERE run_id = ? AND request_type = 'SOURCE_DISCOVERY') AS discoveries
        """,
        (queued.run_id, queued.run_id, queued.run_id),
    ).fetchone()
    if shape is None or tuple(shape) != (1, 1, 1):
        raise DiscoveryError("discovery run shape changed before fenced terminalization")

    group_outcome = "SATISFIED" if has_fingerprint else "SATISFIED_PARTIAL"
    run_status = "SUCCEEDED" if has_fingerprint else "PARTIAL"

    plan_update = conn.execute(
        "UPDATE run_source_plans SET group_outcome = ?"
        " WHERE id = ? AND run_id = ? AND group_outcome IS NULL",
        (group_outcome, queued.run_source_plan_id, queued.run_id),
    )
    if plan_update.rowcount != 1:
        raise DiscoveryError("discovery plan could not be terminalized under fence")

    run_update = conn.execute(
        """
        UPDATE scrape_runs
        SET status = ?,
            finished_at = COALESCE(finished_at, ?),
            requests_total = (SELECT COUNT(*) FROM scrape_requests WHERE run_id = ?),
            requests_failed = (SELECT COUNT(*) FROM scrape_requests
                               WHERE run_id = ? AND status = 'FAILED')
        WHERE id = ? AND status = 'RUNNING'
        """,
        (run_status, now, queued.run_id, queued.run_id, queued.run_id),
    )
    if run_update.rowcount != 1:
        raise DiscoveryError("discovery run could not be terminalized under fence")
    return run_status


def _terminalize_discovery_run(
    conn: sqlite3.Connection,
    queued: QueuedDiscovery,
    *,
    run_status: str,
    group_outcome: str,
    now: str,
) -> None:
    """Terminalize a discovery run whose probe never produced evidence.

    Used for typed authorization denial/cancellation and dispatch rejection:
    the request was already terminalized by its typed handler, so the run
    and its single plan mirror that verdict instead of stranding RUNNING.
    """
    shape = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM run_source_plans WHERE run_id = ?) AS plans,
          (SELECT COUNT(*) FROM scrape_requests WHERE run_id = ?) AS requests
        """,
        (queued.run_id, queued.run_id),
    ).fetchone()
    if shape is None or tuple(shape) != (1, 1):
        raise DiscoveryError("discovery run shape changed before terminalization")
    conn.execute(
        "UPDATE run_source_plans SET group_outcome = ?"
        " WHERE id = ? AND run_id = ? AND group_outcome IS NULL",
        (group_outcome, queued.run_source_plan_id, queued.run_id),
    )
    conn.execute(
        """
        UPDATE scrape_runs
        SET status = ?,
            finished_at = COALESCE(finished_at, ?),
            requests_total = (SELECT COUNT(*) FROM scrape_requests WHERE run_id = ?),
            requests_failed = (SELECT COUNT(*) FROM scrape_requests
                               WHERE run_id = ? AND status = 'FAILED')
        WHERE id = ?
        """,
        (run_status, now, queued.run_id, queued.run_id, queued.run_id),
    )
    conn.commit()


def _terminalize_discovery_denied(
    conn: sqlite3.Connection,
    queued: QueuedDiscovery,
    claim,
    *,
    failure_kind: str,
    detail: dict,
    now: str,
    ownership_now: str | None = None,
) -> None:
    """Record a terminal discovery denial without request-owned outputs.

    No fetch attempt exists (no I/O happened), so only denial evidence is
    recorded under the ownership fence; the request goes FAILED and the
    single-probe run mirrors that verdict instead of stranding RUNNING.
    """
    from jobscraper.pipeline.driver import _record_evidence

    def mutate(cursor_conn: sqlite3.Connection) -> None:
        _record_evidence(
            cursor_conn,
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            kind="FAILURE",
            ref=f"DENIED:{failure_kind}",
            detail=detail,
            content_hash=None,
            now=now,
        )

    try:
        with fenced_commit(
            conn,
            claim.request_id,
            claim.attempt_id,
            now=ownership_now,
            outcome="FAILED",
            failure_kind=failure_kind,
            failure_json=json.dumps(detail, sort_keys=True),
            mutate=mutate,
        ):
            pass
    except StaleOwnership as exc:
        raise DiscoveryError(
            "discovery ownership was lost before denial commit"
        ) from exc
    _terminalize_discovery_run(
        conn, queued, run_status="FAILED", group_outcome="FAILED", now=now
    )


def _discovery_local_retry(conn, claim, budget, *, now: str, ownership_now: str | None = None):
    """Open a retry window for a local executor failure, if budget remains.

    Returns the retry delay when the request was parked RETRY_WAIT, else
    None (budget exhausted — the caller terminalizes). Source health and
    circuit state are untouched: local infrastructure failure never poisons
    them (S3.4 local-vs-source separation).
    """
    from jobscraper.pipeline.driver import _record_evidence

    failure = FailureRecord(
        kind=FailureKind.WORKER_CRASH,
        retryable=True,
        source_health_impact="NONE",
        source_id="",
        binding_id="",
        adapter_id=_DISCOVERY_ADAPTER_ID,
        adapter_version="",
        run_id="",
        request_id=claim.request_id,
        attempt_id=claim.attempt_id,
        details_redacted={},
        observed_at=now,
    )
    decision = decide_retry(
        failure,
        page_class=None,
        attempt_count=int(budget["attempt_count"]),
        max_attempts=int(budget["max_attempts"]),
        headers={},
    )
    if decision.action is not RetryAction.RETRY:
        return None

    def mutate(cursor_conn: sqlite3.Connection) -> None:
        _record_evidence(
            cursor_conn,
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            kind="FAILURE",
            ref="DISPATCH_EXECUTOR_ERROR",
            detail={"kind": FailureKind.WORKER_CRASH.value},
            content_hash=None,
            now=now,
        )

    with fenced_commit(
        conn,
        claim.request_id,
        claim.attempt_id,
        now=ownership_now,
        outcome="RETRY_WAIT",
        retry_delay_s=decision.delay_s,
        failure_kind=FailureKind.WORKER_CRASH.value,
        failure_json=json.dumps({"retry_reason": decision.reason}, sort_keys=True),
        mutate=mutate,
    ):
        pass
    return decision.delay_s


def _handle_discovery_authorization_denied(conn, queued, claim, exc, *, now: str, ownership_now: str | None = None) -> None:
    """Typed handling for a discovery authorization denial: never strand RUNNING."""
    if exc.decision.reason is AuthorizationReason.RUN_CANCELLED:
        finalized = abandon_request_for_cancellation(
            conn, claim.request_id, attempt_id=claim.attempt_id, now=ownership_now
        )
        if not finalized:
            return
        _terminalize_discovery_run(
            conn, queued, run_status="CANCELLED", group_outcome="CANCELLED",
            now=now,
        )
        return
    finalized = finalize_authorization_denial(
        conn, claim.request_id, claim.attempt_id,
        decision=exc.decision, now=ownership_now,
    )
    if finalized:
        _terminalize_discovery_run(
            conn, queued, run_status="FAILED", group_outcome="FAILED", now=now
        )
    # When not finalized the denial was stale (current authority re-check
    # passed): the still-live attempt stays open for redrive.


def execute_source_discovery(
    conn: sqlite3.Connection,
    queued: QueuedDiscovery,
    *,
    worker_id: str = "source-discovery",
    now: str | None = None,
    guard: "ServiceClockGuard | None" = None,
) -> DiscoveryOutcome:
    """Claim and execute one already-durable first probe.

    The request ID is only a locator. The authoritative run/plan/source/binding
    identity is reconstructed from committed rows and must exactly match the
    caller-supplied continuation before any run transition, claim or network I/O.

    Network I/O occurs only after the request claim exists. All outputs that
    assert what the probe saw — fetch attempt, result/page evidence, fingerprint,
    route decision and discovery plan/run terminal state — commit under the same
    ownership fence. If ownership is stale, none of those outputs are committed.
    """
    canonical = load_queued_discovery(conn, request_id=queued.request_id)
    if canonical != queued:
        raise DiscoveryError(
            "caller-supplied discovery identity disagrees with durable request identity"
        )
    queued = canonical

    claim_ts = now or db_utc_now(conn)
    mark_run_started(conn, queued.run_id, now=claim_ts)
    claim = claim_next_request(
        conn,
        worker_id,
        now=now,
        types=frozenset({"SOURCE_DISCOVERY"}),
        run_source_plan_id=queued.run_source_plan_id,
        guard=guard,
    )
    if claim is None or claim.request_id != queued.request_id:
        raise DiscoveryError("durable SOURCE_DISCOVERY request is not claimable")

    def _event_now() -> str:
        # Ownership-sensitive post-claim mutations must prove current
        # ownership: an explicit injected `now` stays deterministic, while
        # production samples fresh database UTC at event time.  The
        # historical claim timestamp is never reused past the claim.
        return now if now is not None else db_utc_now(conn)

    _plan_row, source, _request_plan, envelope = _plan_request(conn, queued, claim)
    policy = source_policy(source, adapter_id=_DISCOVERY_ADAPTER_ID)
    budget = conn.execute(
        "SELECT attempt_count, max_attempts FROM scrape_requests WHERE id = ?",
        (claim.request_id,),
    ).fetchone()
    try:
        # S3.3/S3.4 service-owned HTTP dispatch: live authorization, durable
        # rate/cooldown gating, capacity reservation, execution-plan binding,
        # network I/O, classification and retry intent. No direct
        # execute_request() call remains on this path.
        dispatched = dispatch_http(
            conn,
            envelope,
            policy,
            attempt_count=int(budget["attempt_count"]),
            max_attempts=int(budget["max_attempts"]),
            expect="LIST",
            # Original injected `now` semantics: an explicit test timestamp
            # stays deterministic, while production (`now is None`) lets
            # dispatch/bind sample fresh database UTC at each authority
            # boundary.  Propagating the captured claim timestamp here would
            # let an expired lease pass the pre-I/O fence and perform I/O
            # after ownership was already lost (lease_until <= database_now).
            now=now,
        )
    except CapacityUnavailable:
        # No network request happened and the provider attempt budget is
        # untouched: the minted attempt is abandoned and the request returns
        # to PENDING eligibility for a later pass.  Event time (not the
        # claim timestamp) proves the lease is still live for the refund.
        yield_unstarted_claim(
            conn, claim.request_id, claim.attempt_id,
            reason="CAPACITY_UNAVAILABLE", now=now,
        )
        raise DiscoveryError(
            "discovery dispatch deferred: no provider capacity"
        ) from None
    except DispatchDeferred as exc:
        if exc.next_retry_at is not None:
            # Durable source cooldown: no I/O, attempt preserved as
            # RETRY_WAIT so the probe resumes after the cooldown expires.
            # Event time proves the lease still covers this mutation.
            yield_unstarted_claim(
                conn, claim.request_id, claim.attempt_id,
                reason="RATE_COOLDOWN", next_retry_at=exc.next_retry_at,
                now=now,
            )
            raise DiscoveryError(
                "discovery dispatch deferred: source cooldown active"
            ) from None
        _terminalize_discovery_denied(
            conn, queued, claim,
            failure_kind=exc.failure_kind or "BLOCKED",
            detail={"reason": exc.reason},
            now=_event_now(), ownership_now=now,
        )
        raise DiscoveryError(
            f"discovery dispatch refused: {exc.reason}"
        ) from None
    except (DispatchRejected, UnsupportedExecutionClass) as exc:
        _terminalize_discovery_denied(
            conn, queued, claim,
            failure_kind="POLICY_REJECTED",
            detail={"reason": "DISPATCH_PLAN_INVALID",
                    "error_type": type(exc).__name__},
            now=_event_now(), ownership_now=now,
        )
        raise DiscoveryError(
            f"discovery dispatch rejected: {type(exc).__name__}"
        ) from None
    except DispatchExecutionError as exc:
        # Local infrastructure failure escaping the executor: retryable
        # WORKER_CRASH with no source-health impact, request stays open.
        # The retry/failure fence uses fresh post-I/O event time so an
        # executor error cannot create retry evidence after its lease expired.
        try:
            local_retry = _discovery_local_retry(conn, claim, budget, now=_event_now(), ownership_now=now)
        except StaleOwnership:
            raise DiscoveryError(
                "discovery executor error committed nothing: lease expired"
            ) from None
        if local_retry is None:
            _terminalize_discovery_denied(
                conn, queued, claim,
                failure_kind="WORKER_CRASH",
                detail={"error_type": exc.error_type},
                now=_event_now(), ownership_now=now,
            )
            raise DiscoveryError(
                f"discovery executor error, budget exhausted: {exc.error_type}"
            ) from None
        raise DiscoveryError(
            f"discovery executor error, retry open: {exc.error_type}"
        ) from None
    except AuthorizationDenied as exc:
        _handle_discovery_authorization_denied(conn, queued, claim, exc, now=_event_now(), ownership_now=now)
        raise DiscoveryError(
            f"discovery authorization denied: {exc.decision.reason.value}"
        ) from None
    except StaleOwnership:
        # Pre-I/O ownership loss: reclaim, then inspect the durable request
        # state. A retryable/open request (PENDING, RETRY_WAIT, RUNNING) must
        # never live under a terminal run: leave the run RUNNING and the
        # plan/group open so a later service pass can redrive it. Only a
        # request that is itself terminal lets the run mirror FAILED.
        # Reclamation and terminalization use the original injected `now`
        # (fresh database UTC in production), never the stale claim timestamp.
        reclaim_expired(conn, now=now)
        state = conn.execute(
            "SELECT status FROM scrape_requests WHERE id = ?",
            (claim.request_id,),
        ).fetchone()
        if state is not None and state["status"] in (
            "PENDING",
            "RETRY_WAIT",
            "RUNNING",
        ):
            raise DiscoveryError(
                "discovery ownership lost before dispatch; request still open"
            ) from None
        _terminalize_discovery_run(
            conn, queued, run_status="FAILED", group_outcome="FAILED",
            now=now if now is not None else db_utc_now(conn),
        )
        raise DiscoveryError(
            "discovery ownership was lost before dispatch; attempt consumed"
        ) from None
    result = dispatched.result
    classification = dispatched.classification
    retry_decision = dispatched.retry
    commit_ts = now or db_utc_now(conn)

    fingerprint: AtsFingerprint | None = None
    decision: RouteDecision | None = None
    if result.failure is None and classification.state in _VALID_DISCOVERY_CLASSES:
        fingerprint = classify_content(
            url=result.final_url or result.requested_url,
            body=result.body,
            content_type=result.content_type or "",
        )
        decision = plan_routes(
            fingerprint=fingerprint,
            supported_execution_classes=frozenset({_DISCOVERY_EXECUTION_CLASS}),
        )

    stored: dict[str, str] = {}

    def mutate(cursor_conn: sqlite3.Connection) -> None:
        fetch_attempt_id = _persist_fetch_attempt(
            cursor_conn, envelope, result, commit_ts
        )
        stored["fetch_attempt_id"] = fetch_attempt_id
        _record_evidence(
            cursor_conn,
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            fetch_attempt_id=fetch_attempt_id,
            kind="RESULT_ENVELOPE",
            ref=result.body_ref,
            detail=result.as_evidence(),
            content_hash=result.normalized_content_hash,
            now=commit_ts,
        )
        if result.security_policy_result != "ALLOWED":
            _record_evidence(
                cursor_conn,
                request_id=claim.request_id,
                attempt_id=claim.attempt_id,
                fetch_attempt_id=fetch_attempt_id,
                kind="SECURITY_POLICY",
                ref=result.security_policy_result,
                detail={
                    "result": result.security_policy_result,
                    "requested_url": result.requested_url,
                },
                content_hash=None,
                now=commit_ts,
            )
        cursor_conn.execute(
            "UPDATE scrape_requests SET page_class = ?, last_failure_kind = ?,"
            " last_failure_json = ? WHERE id = ?",
            (
                classification.state.value,
                result.failure.kind.value if result.failure else None,
                json.dumps(result.failure.as_dict()) if result.failure else None,
                claim.request_id,
            ),
        )
        if retry_decision.action is not RetryAction.SUCCEED:
            # Durable source-protection state survives restart even when the
            # probe itself stays open for retry (S3.4).
            record_rate_failure(
                cursor_conn,
                dispatched.rate_key,
                failure_kind=retry_decision.failure_kind or "SOURCE_CHANGED",
                delay_s=retry_delay_s,
                retry_after_raw=retry_decision.retry_after_raw,
                now=commit_ts,
                commit=False,
            )
        else:
            record_rate_success(cursor_conn, dispatched.rate_key, now=commit_ts, commit=False)
        _record_evidence(
            cursor_conn,
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            fetch_attempt_id=fetch_attempt_id,
            kind="PAGE_VALIDITY",
            ref=f"validity://{classification.state.value}",
            detail=dict(classification.evidence),
            content_hash=result.normalized_content_hash,
            now=commit_ts,
        )
        if fingerprint is not None and decision is not None:
            stored["fingerprint_id"] = record_fingerprint(
                cursor_conn,
                source_id=queued.source_id,
                url=result.final_url or result.requested_url,
                fingerprint=fingerprint,
                now=commit_ts,
                commit=False,
            )
            stored["route_decision_id"] = record_route_decision(
                cursor_conn,
                source_id=queued.source_id,
                fingerprint=fingerprint,
                decision=decision,
                now=commit_ts,
                commit=False,
            )
        if retry_decision.action is RetryAction.RETRY:
            # A retryable transient stays open: the probe is durably RETRY_WAIT
            # with cooldown evidence, and the discovery run must NOT
            # terminalize as success/failure while retry work remains open.
            # A later service pass reclaims it after next_retry_at.
            stored["run_status"] = "RUNNING"
        else:
            stored["run_status"] = _finalize_discovery_run_under_fence(
                cursor_conn,
                queued,
                has_fingerprint=fingerprint is not None,
                now=commit_ts,
            )

    # Post-I/O cooldown/retry delay is sampled after the network wait (S3.4);
    # the same value drives both the durable rate record (in mutate) and the
    # RETRY_WAIT fence transition below.
    retry_delay_s = retry_decision.delay_s
    if (
        retry_delay_s is None
        and retry_decision.failure_kind in {"RATE_LIMIT", "CHALLENGE", "BLOCKED"}
    ):
        retry_delay_s = 300.0
    retry_transition = (
        ("RETRY_WAIT", retry_delay_s)
        if retry_decision.action is RetryAction.RETRY
        else ("SUCCEEDED", None)
    )
    try:
        with fenced_commit(
            conn,
            claim.request_id,
            claim.attempt_id,
            now=now,
            outcome=retry_transition[0],
            retry_delay_s=retry_transition[1],
            failure_kind=retry_decision.failure_kind,
            failure_json=(
                json.dumps(
                    {
                        "kind": retry_decision.failure_kind,
                        "reason": retry_decision.reason,
                        "retry_after": retry_decision.retry_after_raw,
                    }
                ) if retry_decision.failure_kind else None
            ),
            mutate=mutate,
        ):
            pass
    except StaleOwnership as exc:
        raise DiscoveryError("discovery ownership was lost before evidence commit") from exc

    run_status = stored.get("run_status")
    if run_status is None:
        raise DiscoveryError("discovery fenced commit omitted terminal run state")

    return DiscoveryOutcome(
        queued=queued,
        result=result,
        page_class=classification.state,
        fingerprint=fingerprint,
        decision=decision,
        fingerprint_id=stored.get("fingerprint_id"),
        route_decision_id=stored.get("route_decision_id"),
        run_status=run_status,
    )


__all__ = [
    "DiscoveryError",
    "DiscoveryOutcome",
    "QueuedDiscovery",
    "execute_source_discovery",
    "load_queued_discovery",
    "queue_source_discovery",
]
