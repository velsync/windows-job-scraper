"""Service-owned run driver (S1.10; 03 RUN-09, ARC-05).

The service process is the authoritative durable claim and capacity
coordinator. For each claimed acquisition request it:

1. builds the ExecutionPlanEnvelope from the immutable RunSourcePlan and
   the adapter's RequestPlan (host validation before any I/O);
2. executes the fetch with NO transaction held (03 §50: no network wait
   inside DB write transactions);
3. classifies page validity — invalid classes never reach the parser;
4. under the fenced commit: persists fetch/parse attempt evidence,
   ingests every observation through the provenance spine, records
   coverage seen identities, and advances or terminates the cursor;
5. enqueues the deterministic continuation (next page) when the cursor
   proposes one;
6. drains host-native obligations and aggregates the run (RUN-01).

Destination policy is host-owned: the allowed host set is derived from
the source's own entry URL. A narrow loopback grant is issued ONLY when
the source's entry host is itself a loopback literal — a host rule in
this driver, constructible by neither imported configuration nor
scraped content (04 §5.1).
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobscraper.runtime.clock import ServiceClockGuard

from jobscraper.acquisition.atsendpoints import spec_for_provider
from jobscraper.acquisition.envelope import (
    ExecutionPlanEnvelope,
    RequestPlan,
    policy_snapshot_reference,
    validate_envelope,
)
from jobscraper.acquisition.failures import FailureKind, FailureRecord
from jobscraper.acquisition.origin import resolve_origin
from jobscraper.acquisition.crawler.budget import (
    budget_from_plan,
    check_budget,
    close_unstarted_over_budget,
    load_usage,
)
from jobscraper.acquisition.crawler.cursor import (
    CursorCompatibilityError,
    load_cursor as load_crawl_cursor,
    save_cursor as save_crawl_cursor,
)
from jobscraper.acquisition.crawler.frontier import enqueue_discovered_task
from jobscraper.acquisition.crawler.pagination import (
    PaginationGuardState,
    PaginationSignature,
    PaginationStopKind,
    advance_guard,
)
from jobscraper.acquisition.crawler.robots import (
    RobotsDecisionKind,
    build_robots_request_plan,
    ensure_robots_request,
    evaluate_robots_policy,
    load_robots_gate,
    parse_robots_result,
)
from jobscraper.acquisition.crawler.sitemap import (
    build_sitemap_request_plan,
    discover_sitemaps_from_robots,
    enqueue_robots_sitemaps,
    enqueue_sitemap_result,
    parse_sitemap,
)
from jobscraper.acquisition.crawler.revalidation import (
    RevalidationCompatibilityError,
    RevalidationPreparation,
    bind_fetch_representation,
    prepare_revalidation,
    release_hold,
    resolve_304,
    restore_membership,
    store_representation,
    touch_representation,
)
from jobscraper.acquisition.crawler.scope import check_scope, scope_from_plan
from jobscraper.acquisition.pagevalidity import PageClass, PageClassification, classify_page
from jobscraper.adapters.contract import (
    NORMAL_PARSE_CLASSES,
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    ParseContext,
    ParseOutcomeKind,
    PlanningContext,
    StopPolicy,
    ValidatedResultEnvelope,
    task_kind_for_request_type,
)
from jobscraper.adapters.registry import build_adapter
from jobscraper.ids import new_id
from jobscraper.net.destination import (
    DestinationPolicy,
    InternalGrant,
)
from jobscraper.net.urlnorm import normalize_url
from jobscraper.pipeline.coverage import (
    CoverageFinalizationError,
    degrade_coverage,
    finalize_coverage,
    is_coverage_degraded,
    open_or_resume_coverage,
    record_contributing_request,
    record_seen_identity,
)
from jobscraper.pipeline.evidence import bounded_json
from jobscraper.pipeline.ingest import ingest_observation
from jobscraper.pipeline.normalize import NORMALIZATION_VERSION
from jobscraper.pipeline.obligations import drain_all_obligations
from jobscraper.runtime.cancellation import (
    abandon_request_for_cancellation,
    run_is_cancelled,
)
from jobscraper.runtime.authorization import (
    AuthorizationDenied,
    AuthorizationReason,
    finalize_authorization_denial,
)
from jobscraper.runtime.capacity import CapacityUnavailable
from jobscraper.runtime.claim_control import yield_unstarted_claim
from jobscraper.runtime.dispatch import (
    DispatchDeferred,
    DispatchExecutionError,
    DispatchRejected,
    UnsupportedExecutionClass,
    dispatch_http,
)
from jobscraper.runtime.claims import StaleOwnership, claim_next_request, reclaim_expired
from jobscraper.runtime.fence import FenceTransition, fenced_commit
from jobscraper.runtime.rate import record_failure as record_rate_failure
from jobscraper.runtime.rate import record_success as record_rate_success
from jobscraper.runtime.requests import (
    ACQUISITION_REQUEST_TYPES,
    enqueue_request,
)
from jobscraper.runtime.runs import (
    TERMINAL_GROUP_OUTCOMES,
    aggregate_run,
    mark_run_started,
    plan_actionable_open_work,
    plan_is_active,
    set_group_outcome,
)
from jobscraper.runtime.retry import RetryAction, decide_retry

#: Host-side budget on enumeration pages per plan per run (RUN-09: bounded).
MAX_PAGES_PER_RUN = 50
#: Host-side budget on typed detail child requests per plan per run (ACQ-04).
#: The adapter's own stop policy is the tighter, per-source bound; this is the
#: host's refusal to be talked into unbounded breadth by content or config.
MAX_DETAIL_REQUESTS_PER_RUN = 200

#: Request types that constitute enumeration pages for coverage purposes
#: (03 §40: coverage links the contributing *enumeration* requests/pages).
_ENUMERATION_REQUEST_TYPES = frozenset({"LIST_FETCH", "SOURCE_CRAWL"})
_CURSOR_TASK_KINDS = frozenset({AdapterTaskKind.ENUMERATE, AdapterTaskKind.CRAWL})
_CRAWL_SCOPED_TASK_KINDS = frozenset(
    {AdapterTaskKind.ENUMERATE, AdapterTaskKind.CRAWL, AdapterTaskKind.DETAIL}
)
#: Typed child work (ACQ-02 DETAIL_FETCH): budgeted separately, and part of
#: the absence barrier only when listing identity is NOT sufficient.
_DETAIL_REQUEST_TYPES = frozenset({"DETAIL_FETCH"})
#: Classifier states that, for a DETAIL task, are typed closure/missing
#: evidence rather than failures (ACQ-02): the job is gone at the provider.
_CLOSURE_CLASSES = frozenset({PageClass.NOT_FOUND, PageClass.JOB_CLOSED})
_OPEN_REQUEST_STATUSES = ("PENDING", "RUNNING", "RETRY_WAIT")

#: Provider-native built-ins whose Source entry host may be widened to the
#: provider's reviewed API hosts (04 §5.1).  Host-owned reviewed data: a
#: provider is listed here only once its adapter has graduated into the
#: registry, and the hosts themselves come from the versioned endpoint table.
_PROVIDER_NATIVE_ADAPTERS: dict[str, str] = {
    "greenhouse": "GREENHOUSE",
    "lever": "LEVER",
    "ashby": "ASHBY",
}


def source_policy(
    source_row: sqlite3.Row | dict, *, adapter_id: str | None = None
) -> DestinationPolicy:
    """Host-owned destination policy for one source (04 §5.1).

    A provider adapter may add only hosts from the reviewed, versioned ATS
    endpoint table, and only when the Source entry host itself is a reviewed
    host for that provider. Adapter config is deliberately not an input, so
    imported/scraped data cannot widen network authority.
    """
    try:
        normalized = normalize_url(source_row["entry_url"])
        host = normalized.host or ""
    except Exception:
        host = ""

    allowed_hosts: set[str] = {host} if host else set()
    provider = _PROVIDER_NATIVE_ADAPTERS.get(adapter_id or "")
    if provider is not None and host:
        spec = spec_for_provider(provider)
        if host in {*spec.hosted_hosts, *spec.api_hosts}:
            allowed_hosts.update(spec.api_hosts)

    grant = None
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        is_loopback = literal.is_loopback
    except ValueError:
        is_loopback = False
    if is_loopback:
        # Narrow host rule: only a source whose own entry host is a loopback
        # literal gets a loopback grant for that exact host. Scraped content
        # and imported configuration can never construct a grant.
        grant = InternalGrant(
            purpose="loopback-source-entry", allowed_hosts=frozenset({host})
        )
    return DestinationPolicy(
        # An invalid/missing source host is a fail-closed empty allowlist, not
        # ``None`` (which means unrestricted public hosts to DestinationPolicy).
        allowed_hosts=frozenset(allowed_hosts),
        internal_grant=grant,
        max_redirects=3,
        timeout_s=30.0,
        max_bytes=2_000_000,
    )


def _plan_config(conn: sqlite3.Connection, plan_row: sqlite3.Row) -> dict:
    row = conn.execute(
        "SELECT config_json FROM source_adapter_binding_revisions WHERE id = ?",
        (plan_row["binding_revision_id"],),
    ).fetchone()
    return json.loads(row["config_json"]) if row and row["config_json"] else {}


def execute_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    worker_id: str = "service",
    now: str | None = None,
    guard: "ServiceClockGuard | None" = None,
) -> str:
    """Drive one run to completion (bounded) and return its aggregate status.

    ``guard`` is the service-lifetime wall-clock anomaly guard (§50): the
    service passes its own guard so every claim on the run path observes the
    database clock and performs anomaly recovery inline.
    """
    from jobscraper.runtime.clock import db_utc_now

    ts = now or db_utc_now(conn)
    mark_run_started(conn, run_id, now=ts)
    plan_rows = conn.execute(
        "SELECT * FROM run_source_plans WHERE run_id = ? ORDER BY source_plan_group_id, fallback_rank",
        (run_id,),
    ).fetchall()

    for plan_row in plan_rows:
        _execute_plan(conn, run_id, plan_row, worker_id=worker_id, now=ts, guard=guard)

    # Host-native obligations drain before the run finalizes (a run must
    # not report finished while accepted observations are unprocessed).
    drain_all_obligations(conn, now=now)
    _update_run_counters(conn, run_id)
    return aggregate_run(conn, run_id, now=ts)


def _update_run_counters(conn: sqlite3.Connection, run_id: str) -> None:
    """Honest run accounting derived from persisted evidence (§16.1)."""
    conn.execute(
        """
        UPDATE scrape_runs SET
            jobs_discovered = (SELECT COUNT(*) FROM job_observations WHERE run_id = ?),
            jobs_saved = (
                SELECT COUNT(DISTINCT js.job_id) FROM job_sources js
                JOIN jobs j ON j.id = js.job_id
                WHERE js.last_observation_id IN
                    (SELECT id FROM job_observations WHERE run_id = ?)
                  AND j.created_at >= COALESCE(
                      (SELECT started_at FROM scrape_runs WHERE id = ?), j.created_at)
            ),
            jobs_updated = (
                SELECT COUNT(DISTINCT js.job_id) FROM job_sources js
                JOIN jobs j ON j.id = js.job_id
                WHERE js.last_observation_id IN
                    (SELECT id FROM job_observations WHERE run_id = ?)
                  AND j.created_at < COALESCE(
                      (SELECT started_at FROM scrape_runs WHERE id = ?), j.created_at)
            ),
            requests_total = (SELECT COUNT(*) FROM scrape_requests WHERE run_id = ?),
            requests_failed = (SELECT COUNT(*) FROM scrape_requests
                               WHERE run_id = ? AND status = 'FAILED')
        WHERE id = ?
        """,
        (run_id, run_id, run_id, run_id, run_id, run_id, run_id, run_id),
    )
    conn.commit()


def _claim_target_reference(claim) -> str | None:
    """The durable target reference a claimed request carries.

    ``scrape_requests`` folds the target into ``request_unique_key`` and keeps
    the request payload, so evidence rows name what the host durably recorded
    (typed detail children carry their reference in the payload) instead of
    reconstructing an identity from content.  ``None`` is honest: an
    enumeration continuation records its state in the cursor, not here.
    """
    payload = dict(claim.payload or {})
    value = payload.get("target_reference") or payload.get("url")
    return value if isinstance(value, str) and value else None


def _plan_needs_redrive(
    conn: sqlite3.Connection, plan_row: sqlite3.Row, *, now: str
) -> bool:
    """Whether a non-active plan must still be driven (restart recovery).

    Dormant fallback ranks never start new work, but two accepted shapes
    still need the driver:

    * no recorded outcome: close from durable evidence when nothing is
      open, or repair a plan diagnostic lost after its group verdict
      committed.  Open work owned by a dormant rank cannot proceed
      (claims stay rank-bound), so it does not justify driving.
    * partial verdict: accepted children may still drain, but only while
      this rank still owns a partial group.

    Any other recorded verdict stands.
    """
    outcome = plan_row["group_outcome"]
    if outcome is None:
        return plan_actionable_open_work(conn, plan_row["id"], now=now) == 0
    if outcome != "SATISFIED_PARTIAL":
        return False
    if plan_actionable_open_work(conn, plan_row["id"], now=now) == 0:
        return False
    state = conn.execute(
        "SELECT active_fallback_rank, group_outcome FROM source_plan_group_state"
        " WHERE run_id = ? AND source_plan_group_id = ?",
        (plan_row["run_id"], plan_row["source_plan_group_id"]),
    ).fetchone()
    return (
        state is not None
        and int(state["active_fallback_rank"]) == int(plan_row["fallback_rank"])
        and state["group_outcome"] == "SATISFIED_PARTIAL"
    )


def _open_acquisition_requests(conn: sqlite3.Connection, run_source_plan_id: str) -> int:
    """How much of this plan's own accepted acquisition work is still open.

    ACQ-04/RUN-01: a plan may not be terminalized while child work it accepted
    is still claimable or in flight — and it may not be re-driven once all of
    that work is durably closed.
    """
    placeholders = ",".join("?" for _ in ACQUISITION_REQUEST_TYPES)
    statuses = ",".join("?" for _ in _OPEN_REQUEST_STATUSES)
    return conn.execute(
        "SELECT COUNT(*) FROM scrape_requests"
        f" WHERE run_source_plan_id = ? AND request_type IN ({placeholders})"
        f" AND status IN ({statuses})",
        (run_source_plan_id, *sorted(ACQUISITION_REQUEST_TYPES), *_OPEN_REQUEST_STATUSES),
    ).fetchone()[0]


def _latest_complete_coverage(
    conn: sqlite3.Connection, run_source_plan_id: str
) -> sqlite3.Row | None:
    """Latest durable COMPLETE enumeration proof for this immutable plan."""
    return conn.execute(
        "SELECT * FROM enumeration_coverage"
        " WHERE run_source_plan_id = ? AND completion_state = 'COMPLETE'"
        " AND finalized_at IS NOT NULL AND terminal_enumeration_proven = 1"
        " ORDER BY finalized_at DESC, id DESC LIMIT 1",
        (run_source_plan_id,),
    ).fetchone()


def _dispatch_child_tasks(
    conn: sqlite3.Connection,
    *,
    outcome_obj,
    task_kind: AdapterTaskKind,
    claim,
    plan_row: sqlite3.Row,
    run_id: str,
    ts: str,
    crawl_scope,
    crawl_budget,
    base_url: str | None,
) -> tuple[bool, bool]:
    """Host-validate typed child proposals before durable enqueue (ACQ-04).

    Returns ``(degraded, budget_exhausted)``. Re-load durable usage before
    every child so one parse cannot enqueue N tasks against one stale budget
    snapshot. Opaque DETAIL references remain adapter input; the final planned
    URL is independently scope-checked before dispatch.
    """
    degraded = False
    budget_exhausted = False
    for discovered in outcome_obj.discovered_tasks:
        usage = load_usage(conn, plan_row["id"], now=ts)
        decision = enqueue_discovered_task(
            conn,
            run_id=run_id,
            plan_row=plan_row,
            parent_request_id=claim.request_id,
            discovered=discovered,
            scope=crawl_scope,
            budget=crawl_budget,
            usage=usage,
            base_url=base_url,
            now=ts,
        )
        if decision.accepted:
            if (
                not decision.created
                and decision.request_type in _ENUMERATION_REQUEST_TYPES
            ):
                # Idempotency prevents a duplicate durable request, but a
                # crawler proposing an already-known continuation is also
                # no-progress evidence. Keep accepted DETAIL dedupe benign.
                degraded = True
                _record_evidence(
                    conn, request_id=claim.request_id, attempt_id=claim.attempt_id,
                    fetch_attempt_id=None, kind="REVIEW",
                    ref="crawler://DUPLICATE_FRONTIER",
                    detail={
                        "reason": decision.reason,
                        "request_type": decision.request_type,
                        "normalized_target": decision.normalized_target,
                        "logical_key": discovered.logical_key,
                    },
                    content_hash=None, now=ts,
                )
            continue
        degraded = True
        if decision.reason.startswith("BUDGET_"):
            budget_exhausted = True
        _record_evidence(
            conn,
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            fetch_attempt_id=None,
            kind="REVIEW",
            ref=f"child-task://{discovered.kind}",
            detail={
                "reason": decision.reason,
                "kind": discovered.kind,
                "depth": discovered.depth,
                "logical_key": discovered.logical_key,
                "target_reference": discovered.target_reference,
                "normalized_target": decision.normalized_target,
                "from_task_kind": task_kind.value,
            },
            content_hash=None,
            now=ts,
        )
    return degraded, budget_exhausted


def _outcome_from_durable_state(conn: sqlite3.Connection, plan_id: str) -> str:
    """The honest group outcome when this pass has nothing left to claim.

    Restart recovery can land after an earlier pass closed every request but
    before it recorded the outcome (``finalize_coverage`` and
    ``set_group_outcome`` are separate commits).  Deriving the outcome from
    durable evidence keeps a collection that succeeded from being rewritten as
    a failure just because the resuming pass had no work left to do.
    """
    placeholders = ",".join("?" for _ in ACQUISITION_REQUEST_TYPES)
    closed = conn.execute(
        "SELECT COUNT(*) FROM scrape_requests"
        f" WHERE run_source_plan_id = ? AND request_type IN ({placeholders})"
        " AND status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')",
        (plan_id, *sorted(ACQUISITION_REQUEST_TYPES)),
    ).fetchone()[0]
    if not closed:
        return "FAILED"
    complete = conn.execute(
        "SELECT COUNT(*) FROM enumeration_coverage WHERE run_source_plan_id = ?"
        " AND completion_state = 'COMPLETE' AND finalized_at IS NOT NULL",
        (plan_id,),
    ).fetchone()[0]
    return "SATISFIED" if complete else "SATISFIED_PARTIAL"


def _commit_fenced(
    conn,
    run_id,
    claim,
    *,
    ts,
    mutate,
    transition: FenceTransition | None = None,
    transition_resolver=None,
) -> str:
    """One fenced commit for one claim; report ownership loss honestly.

    Returns ``COMMITTED`` / ``CANCELLED`` / ``DENIED`` / ``STALE``.  The
    fence owns the claim-and-commit contract (RUN-07, 03 §18): a refused late
    commit rolls back every request-owned output.  ``transition_resolver`` may
    refine the terminal transition only after the caller mutation has derived
    parse outcome, while still inside the same serialized fence.
    """
    selected = transition or FenceTransition()
    try:
        # Lease/service-epoch ownership is evaluated at commit time, not at the
        # earlier claim/dispatch timestamp.  A long network request must never
        # revive an ownership token whose lease expired while I/O was in flight.
        with fenced_commit(
            conn,
            claim.request_id,
            claim.attempt_id,
            outcome=selected.outcome,
            retry_delay_s=selected.retry_delay_s,
            failure_kind=selected.failure_kind,
            failure_json=selected.failure_json,
            mutate=mutate,
            transition_resolver=transition_resolver,
        ):
            pass
        return "COMMITTED"
    except AuthorizationDenied as exc:
        if exc.decision.reason is AuthorizationReason.RUN_CANCELLED:
            finalized = abandon_request_for_cancellation(
                conn, claim.request_id, attempt_id=claim.attempt_id
            )
            return "CANCELLED" if finalized else "STALE"
        finalized = finalize_authorization_denial(
            conn,
            claim.request_id,
            claim.attempt_id,
            decision=exc.decision,
        )
        return "DENIED" if finalized else "STALE"
    except StaleOwnership:
        if run_is_cancelled(conn, run_id):
            # §18: the fence refused a late acquisition commit; outputs were
            # rolled back before this exact-attempt cancellation transition.
            finalized = abandon_request_for_cancellation(
                conn, claim.request_id, attempt_id=claim.attempt_id
            )
            return "CANCELLED" if finalized else "STALE"
        # Lease expiry can be reclaimed immediately.  Epoch-stale ownership is
        # recovered by the service-epoch coordinator on the next claim/startup.
        reclaim_expired(conn)
        return "STALE"


def _bind_crawl_cursor_to_plan(proposal: CrawlCursor, plan_row) -> CrawlCursor:
    """Stamp host-owned RunSourcePlan identity onto an adapter cursor proposal.

    Cursor identity (source/binding) is host-owned: the adapter proposes
    opaque pagination *state* under the plan's pins (02 ACQ-09), and accepted
    pre-S3.5 adapters echo the incoming cursor's identity, which is empty on
    the first page.  The driver therefore binds the immutable plan identity
    here.  Adapter/version/schema provenance passes through untouched, so
    ``save_crawl_cursor`` still refuses a state blob produced by different
    code rather than silently restamping it.
    """
    return CrawlCursor(
        source_id=plan_row["source_id"],
        binding_id=plan_row["binding_id"],
        adapter_id=proposal.adapter_id,
        adapter_version=proposal.adapter_version,
        cursor_schema_version=proposal.cursor_schema_version,
        state_json=proposal.state_json,
        checkpoint_at=proposal.checkpoint_at,
    )


def _commit_crawler_pre_dispatch_stop(
    conn: sqlite3.Connection,
    run_id: str,
    claim,
    *,
    reason: str,
    ts: str,
    ref: str,
    failure_kind: str | None = None,
) -> str:
    """Close a claimed unit that policy proves must not perform network I/O."""
    return _commit_fenced(
        conn,
        run_id,
        claim,
        ts=ts,
        mutate=lambda cursor_conn: _record_evidence(
            cursor_conn,
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            kind="REVIEW",
            ref=ref,
            detail={"reason": reason, "network_io_started": False},
            content_hash=None,
            now=ts,
        ),
        transition=FenceTransition(
            outcome="FAILED",
            failure_kind=failure_kind,
            failure_json=bounded_json({"reason": reason, "network_io_started": False}),
        ),
    )


def _execute_plan(
    conn: sqlite3.Connection,
    run_id: str,
    plan_row: sqlite3.Row,
    *,
    worker_id: str,
    now: str,
    guard: "ServiceClockGuard | None" = None,
) -> None:
    """Drive one immutable RunSourcePlan to an honest terminal state.

    S2.5 generalization (ARC-04.3, ACQ-02/ACQ-04, 03 RUN-01/RUN-09, §40):

    * the adapter is whatever the pinned binding revision names, built only
      through the registry — the host special-cases no adapter identity;
    * request type -> task kind is durable data, so enumeration pages and the
      typed detail children they produce are planned by the same adapter;
    * a plan is never terminalized around claimable-or-inflight child work,
      except by an explicitly partial verdict; a partial rank keeps owning
      its accepted children across passes until they drain (completion
      upgrade) or fail, and re-driving a finally-terminal plan is an
      idempotent no-op;
    * host budgets bound enumeration pages and detail requests separately, and
      hitting one yields BUDGET_EXHAUSTED — never absence authority.
    """
    from jobscraper.runtime.clock import db_utc_now

    plan_id = plan_row["id"]
    # S3.9: dormant fallback ranks cannot start new work through
    # direct/internal calls, but restart recovery still re-drives a rank
    # that never recorded an outcome (close/repair) or that still owns
    # open claimable-or-inflight work under a partial verdict.
    if not plan_is_active(conn, plan_id) and not _plan_needs_redrive(
        conn, plan_row, now=now
    ):
        return
    open_work = _open_acquisition_requests(conn, plan_id)
    if plan_row["group_outcome"] in TERMINAL_GROUP_OUTCOMES and open_work == 0:
        return
    if open_work == 0 and plan_row["group_outcome"] is None:
        # Everything this plan accepted is already closed and no outcome was
        # recorded: close the plan from durable evidence.  Opening a coverage
        # generation here would claim to have covered something this pass never
        # fetched.
        set_group_outcome(
            conn, plan_id, _outcome_from_durable_state(conn, plan_id),
            now=db_utc_now(conn),
        )
        return

    source = conn.execute(
        "SELECT * FROM sources WHERE id = ?", (plan_row["source_id"],)
    ).fetchone()
    policy = source_policy(source, adapter_id=plan_row["adapter_id"])
    config = _plan_config(conn, plan_row)
    try:
        adapter = build_adapter(plan_row["adapter_id"], config)
    except KeyError as exc:
        raise ValueError(f"no builtin adapter for {plan_row['adapter_id']!r}") from exc
    stop_policy = getattr(adapter, "stop_policy", StopPolicy())
    crawl_budget = budget_from_plan(plan_row, stop_policy=stop_policy)
    crawl_scope = scope_from_plan(
        plan_row, source, destination_allowed_hosts=policy.allowed_hosts
    )

    # 03 §40: detail completion joins the coverage barrier only when the
    # binding contract does not already declare listing identity sufficient.
    listing_identity_sufficient = bool(
        getattr(adapter, "listing_identity_sufficient", False)
    )

    # If enumeration alone proves stable membership, a COMPLETE generation
    # remains durable truth while accepted DETAIL enrichment is resumed.  A
    # detail-only restart must not invent a second enumeration generation.
    durable_complete = (
        _latest_complete_coverage(conn, plan_id)
        if listing_identity_sufficient
        else None
    )
    coverage_finalized = durable_complete is not None
    if durable_complete is not None:
        coverage_id = durable_complete["id"]
    else:
        coverage_id, _resumed = open_or_resume_coverage(
            conn,
            run_source_plan_id=plan_id,
            source_id=plan_row["source_id"],
            binding_id=plan_row["binding_id"],
            binding_revision_id=plan_row["binding_revision_id"],
            scope_key="full-source",
            generation_key=f"run-{run_id}",
            coverage_authority="AUTHORITATIVE_FULL_SOURCE",
            listing_identity_sufficient=listing_identity_sufficient,
            now=now,
        )

    terminal = durable_complete is not None
    # ACQ-03 / RUN-13: degradation is durable per generation.  A resumed pass
    # that never saw the PARTIAL outcome in memory must still know about it.
    coverage_degraded = (
        False if coverage_finalized else is_coverage_degraded(conn, coverage_id)
    )
    # …and a plan whose open generation is already degraded cannot end this
    # pass SATISFIED: the group outcome is derived from the same durable truth
    # the coverage is, not from what this process happened to witness.
    run_degraded = coverage_degraded
    cancelled = False
    deferred = False
    ownership_lost = False
    state_changed = False
    durable_budget_exhausted = False
    pages = 0
    details = 0
    while True:
        if run_is_cancelled(conn, run_id):
            # §18: no new acquisition driving once cancellation is durable
            cancelled = True
            break
        loop_now = db_utc_now(conn)
        durable_usage = load_usage(conn, plan_id, now=loop_now)
        allowed = set(ACQUISITION_REQUEST_TYPES)
        if (
            durable_usage.elapsed_s >= crawl_budget.max_runtime_s
            or durable_usage.bytes_downloaded >= crawl_budget.max_bytes
        ):
            reason = (
                "MAX_RUNTIME"
                if durable_usage.elapsed_s >= crawl_budget.max_runtime_s
                else "MAX_BYTES"
            )
            close_unstarted_over_budget(
                conn, plan_id, request_types=frozenset(allowed), reason=reason, now=loop_now
            )
            durable_budget_exhausted = True
            state_changed = True
            break
        if durable_usage.pages_completed >= crawl_budget.max_pages:
            close_unstarted_over_budget(
                conn, plan_id, request_types=_ENUMERATION_REQUEST_TYPES,
                reason="MAX_PAGES", now=loop_now,
            )
            allowed -= set(_ENUMERATION_REQUEST_TYPES)
            durable_budget_exhausted = True
            state_changed = True
        if durable_usage.detail_requests_created >= crawl_budget.max_detail_requests:
            close_unstarted_over_budget(
                conn, plan_id, request_types=_DETAIL_REQUEST_TYPES,
                reason="MAX_DETAIL_REQUESTS", now=loop_now,
            )
            allowed -= set(_DETAIL_REQUEST_TYPES)
            durable_budget_exhausted = True
            state_changed = True
        # S3.5 keeps the accepted per-pass claim throttles as fast guards
        # alongside the durable plan-level budgets above: they only narrow
        # what this pass may claim and never terminalize deferred work, so a
        # throttled pass leaves PENDING children drainable by a later pass.
        if pages >= MAX_PAGES_PER_RUN:
            allowed -= set(_ENUMERATION_REQUEST_TYPES)
        if details >= MAX_DETAIL_REQUESTS_PER_RUN:
            allowed -= set(_DETAIL_REQUEST_TYPES)
        if not allowed:
            break
        claim = claim_next_request(
            conn, worker_id, types=frozenset(allowed),
            run_source_plan_id=plan_id, guard=guard,
        )
        if claim is None:
            break
        ts = db_utc_now(conn)
        task_kind = task_kind_for_request_type(claim.request_type)
        target_reference = _claim_target_reference(claim)
        request_meta = conn.execute(
            "SELECT depth, priority FROM scrape_requests WHERE id = ?",
            (claim.request_id,),
        ).fetchone()
        claim_depth = int(request_meta["depth"] if request_meta is not None else 0)
        claim_priority = int(request_meta["priority"] if request_meta is not None else 0)
        claim_payload = dict(claim.payload or {})
        host_revalidation_mode = str(
            claim_payload.pop("_host_revalidation", "") or ""
        ).upper()
        crawl_role = str(claim_payload.get("role") or "").upper()
        is_robots = (
            claim.request_type == "SOURCE_CRAWL" and crawl_role == "ROBOTS"
        )
        is_sitemap = (
            claim.request_type == "SOURCE_CRAWL" and crawl_role == "SITEMAP"
        )
        raw_sitemap_depth = claim_payload.get("sitemap_depth", 0)
        try:
            sitemap_depth = max(0, int(raw_sitemap_depth))
        except (TypeError, ValueError):
            sitemap_depth = 0
        is_enumeration = (
            claim.request_type in _ENUMERATION_REQUEST_TYPES
            and not is_robots
            and not is_sitemap
        )
        if is_enumeration or (
            claim.request_type == "DETAIL_FETCH" and not listing_identity_sufficient
        ):
            # 03 §40: the coverage owner validates that each contributing
            # request belongs to this exact immutable plan/source/binding.
            # Robots/sitemap discovery metadata never becomes a membership
            # contributor merely because detail completion is required.
            record_contributing_request(conn, coverage_id, claim.request_id)

        cursor = None
        pagination_guard = PaginationGuardState()
        plan_refusal: str | None = None
        if task_kind in _CURSOR_TASK_KINDS and not is_robots and not is_sitemap:
            try:
                loaded_cursor = load_crawl_cursor(
                    conn, run_source_plan_id=plan_id, plan_row=plan_row
                )
                if loaded_cursor is not None:
                    cursor = loaded_cursor.cursor
                    pagination_guard = loaded_cursor.guard_state
            except CursorCompatibilityError as exc:
                plan_refusal = f"CursorCompatibilityError: {exc}"
        task = AdapterTask(kind=task_kind, payload=claim_payload)
        # 02 ACQ-09: planning receives the host-resolved pins, never mutable
        # host state.  Snapshots that do not exist durably yet stay None.
        planning_ctx = PlanningContext(
            run_id=run_id,
            run_source_plan_id=plan_id,
            source_snapshot_ref=f"source://{plan_row['source_id']}",
            binding_revision_id=plan_row["binding_revision_id"],
            permission_profile_revision=plan_row["permission_profile_revision"],
            cursor_schema_version=plan_row["cursor_schema_version"],
        )
        request_plan = None
        if plan_refusal is None:
            try:
                request_plan = (
                    build_robots_request_plan(source["entry_url"])
                    if is_robots
                    else build_sitemap_request_plan(target_reference)
                    if is_sitemap
                    else adapter.plan(task, cursor, ctx=planning_ctx)
                )
            except (ValueError, TypeError) as exc:
                plan_refusal = f"{type(exc).__name__}: {exc}"

        signal: dict = {}
        if request_plan is None:
            # A refused plan is a typed failure record, not a fetch and not a
            # parse: no request is issued, no observation is invented, and the
            # generation cannot claim terminal enumeration.
            def mutate(
                cursor_conn,
                *,
                _refusal=plan_refusal,
                _kind=task_kind,
                _target_reference=target_reference,
            ):
                cursor_conn.execute(
                    "UPDATE scrape_requests SET page_class = ?, last_failure_kind = ?"
                    " WHERE id = ?",
                    ("UNKNOWN", FailureKind.INVALID_JOB_RECORD.value, claim.request_id),
                )
                _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    fetch_attempt_id=None,
                    kind="FAILURE",
                    ref="ADAPTER_PLAN_REFUSED",
                    detail={
                        "reason": "ADAPTER_PLAN_REFUSED",
                        "task_kind": _kind.value,
                        "target_reference": _target_reference,
                        "detail": (_refusal or "")[:500],
                    },
                    content_hash=None,
                    now=ts,
                )
                signal["value"] = "REFUSED"

            refusal_status = _commit_fenced(
                conn, run_id, claim, ts=ts, mutate=mutate,
                transition=FenceTransition(
                    outcome="FAILED",
                    failure_kind=FailureKind.INVALID_JOB_RECORD.value,
                    failure_json=bounded_json({
                        "reason": "ADAPTER_PLAN_REFUSED",
                        "detail": (plan_refusal or "")[:500],
                    }),
                ),
            )
            if refusal_status != "COMMITTED":
                if refusal_status == "CANCELLED" or run_is_cancelled(conn, run_id):
                    cancelled = True
                elif refusal_status == "STALE":
                    deferred = True
                break
            # No executor ran; the durable request did reach a terminal
            # policy/plan result, so a later aggregate pass must not stay open.
            state_changed = True
            run_degraded = True
            if is_enumeration or not listing_identity_sufficient:
                coverage_degraded = True
            continue

        # S3.5 final crawl-scope check is against the actual adapter/host
        # RequestPlan URL, so opaque DETAIL references remain supported while
        # no planned URL can escape host-owned destination authority.
        if task_kind in _CRAWL_SCOPED_TASK_KINDS and not is_robots:
            scope_decision = check_scope(
                crawl_scope, request_plan.url, depth=claim_depth
            )
            if not scope_decision.allowed:
                status = _commit_crawler_pre_dispatch_stop(
                    conn, run_id, claim, ts=ts,
                    reason=f"CRAWL_SCOPE_{scope_decision.reason}",
                    ref="crawler://SCOPE_DENIED",
                    failure_kind=FailureKind.POLICY_REJECTED.value,
                )
                if status == "COMMITTED":
                    state_changed = True
                    run_degraded = True
                    if is_enumeration or not listing_identity_sufficient:
                        coverage_degraded = True
                    continue
                if status == "CANCELLED":
                    cancelled = True
                else:
                    deferred = True
                break

        # Robots policy is a host-owned dependency only for generic CRAWL PAGE
        # work. Provider/API ENUMERATE paths are not silently converted into
        # web crawling. The robots fetch itself is durable SOURCE_CRAWL work.
        page_robots_decision = None
        if task_kind is AdapterTaskKind.CRAWL and not is_robots:
            robots_mode = str(source["robots_mode"] or "RESPECT").upper()
            if robots_mode != "RESPECT":
                status = _commit_crawler_pre_dispatch_stop(
                    conn, run_id, claim, ts=ts,
                    reason=f"UNSUPPORTED_ROBOTS_MODE:{robots_mode}",
                    ref="robots://UNSUPPORTED_MODE",
                    failure_kind=FailureKind.POLICY_REJECTED.value,
                )
                if status == "COMMITTED":
                    state_changed = True
                    run_degraded = True
                    coverage_degraded = True
                    continue
                deferred = status != "CANCELLED"
                cancelled = status == "CANCELLED"
                break
            robots_gate = load_robots_gate(conn, plan_id)
            if robots_gate.state == "MISSING":
                usage = load_usage(conn, plan_id, now=ts)
                budget_decision = check_budget(
                    crawl_budget, usage, proposed_request_type=None, proposed_depth=0,
                    proposed_execution_class="HTTP",
                )
                if not budget_decision.allowed:
                    status = _commit_crawler_pre_dispatch_stop(
                        conn, run_id, claim, ts=ts,
                        reason=f"ROBOTS_{budget_decision.reason}",
                        ref="robots://BUDGET_EXHAUSTED",
                    )
                    if status == "COMMITTED":
                        state_changed = True
                        durable_budget_exhausted = True
                        run_degraded = True
                        coverage_degraded = True
                        continue
                    deferred = status != "CANCELLED"
                    cancelled = status == "CANCELLED"
                    break
                ensure_robots_request(
                    conn, run_id=run_id, plan_row=plan_row,
                    source_entry_url=source["entry_url"],
                    parent_request_id=claim.request_id, priority=claim_priority + 1,
                    now=ts, commit=True,
                )
                if not yield_unstarted_claim(
                    conn, claim.request_id, claim.attempt_id, reason="ROBOTS_PENDING"
                ):
                    deferred = True
                    break
                continue
            if robots_gate.state == "PENDING":
                if not yield_unstarted_claim(
                    conn, claim.request_id, claim.attempt_id, reason="ROBOTS_PENDING"
                ):
                    deferred = True
                    break
                continue
            if robots_gate.state != "READY" or robots_gate.policy is None:
                status = _commit_crawler_pre_dispatch_stop(
                    conn, run_id, claim, ts=ts, reason="ROBOTS_UNKNOWN",
                    ref="robots://UNKNOWN",
                    failure_kind=FailureKind.POLICY_REJECTED.value,
                )
                if status == "COMMITTED":
                    state_changed = True
                    run_degraded = True
                    coverage_degraded = True
                    continue
                deferred = status != "CANCELLED"
                cancelled = status == "CANCELLED"
                break
            robots_decision = evaluate_robots_policy(robots_gate.policy, request_plan.url)
            if robots_decision.kind is not RobotsDecisionKind.ALLOW:
                status = _commit_crawler_pre_dispatch_stop(
                    conn, run_id, claim, ts=ts, reason=robots_decision.reason,
                    ref=f"robots://{robots_decision.kind.value}",
                    failure_kind=FailureKind.POLICY_REJECTED.value,
                )
                if status == "COMMITTED":
                    state_changed = True
                    run_degraded = True
                    coverage_degraded = True
                    continue
                deferred = status != "CANCELLED"
                cancelled = status == "CANCELLED"
                break

            page_robots_decision = robots_decision

        # S3.7: conditional validators are host-owned and can be attached
        # only after exact cache compatibility. Robots/sitemaps keep their
        # existing metadata paths and are not revalidated here.
        eligible_revalidation = (
            claim.request_type in {"LIST_FETCH", "DETAIL_FETCH", "SOURCE_CRAWL"}
            and not is_robots
            and not is_sitemap
        )
        revalidation_preparation = RevalidationPreparation(
            request_plan=request_plan,
            reason="NOT_ELIGIBLE",
        )
        if eligible_revalidation:
            expected_cache_classes = (
                ("VALID_JOB",)
                if task_kind is AdapterTaskKind.DETAIL
                else ("VALID_LIST", "EMPTY")
            )
            try:
                revalidation_preparation = prepare_revalidation(
                    conn,
                    plan_row=plan_row,
                    request_plan=request_plan,
                    attempt_id=claim.attempt_id,
                    expected_page_classes=expected_cache_classes,
                    require_membership=is_enumeration,
                    normalization_version=NORMALIZATION_VERSION,
                    now=ts,
                    force_unconditional=(
                        host_revalidation_mode == "UNCONDITIONAL"
                    ),
                )
                request_plan = revalidation_preparation.request_plan
            except RevalidationCompatibilityError as exc:
                status = _commit_fenced(
                    conn, run_id, claim, ts=ts,
                    mutate=lambda cursor_conn, _exc=exc: _record_evidence(
                        cursor_conn,
                        request_id=claim.request_id,
                        attempt_id=claim.attempt_id,
                        kind="SECURITY_POLICY",
                        ref="cache://REVALIDATION_PLAN_REFUSED",
                        detail={"reason": str(_exc)[:500]},
                        content_hash=None,
                        now=ts,
                    ),
                    transition=FenceTransition(
                        outcome="FAILED",
                        failure_kind=FailureKind.POLICY_REJECTED.value,
                        failure_json=bounded_json({
                            "reason": "REVALIDATION_PLAN_REFUSED",
                            "detail": str(exc)[:500],
                        }),
                    ),
                )
                if status == "CANCELLED":
                    cancelled = True
                elif status == "STALE":
                    deferred = True
                else:
                    run_degraded = True
                    if is_enumeration or not listing_identity_sufficient:
                        coverage_degraded = True
                continue

        envelope = ExecutionPlanEnvelope(
            plan_id=new_id("plan"),
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            run_id=run_id,
            run_source_plan_id=plan_id,
            source_id=plan_row["source_id"],
            binding_id=plan_row["binding_id"],
            binding_revision_id=plan_row["binding_revision_id"],
            adapter_id=plan_row["adapter_id"],
            adapter_version=plan_row["adapter_version"],
            strategy=plan_row["strategy"],
            execution_class=plan_row["execution_class"],
            policy_snapshot_ref=policy_snapshot_reference(plan_row),
            permission_profile_id=plan_row["permission_profile_id"],
            permission_profile_revision=plan_row["permission_profile_revision"],
            payload_kind="REQUEST",
            payload=RequestPlan(
                method=request_plan.method,
                url=request_plan.url,
                headers=dict(request_plan.headers),
                expected_content_types=tuple(request_plan.expected_content_types),
                timeout_s=request_plan.timeout_s,
                max_bytes=request_plan.max_bytes,
                purpose=claim.request_type,
                allowed_redirects=policy.max_redirects,
                cache_policy=request_plan.cache_policy,
                revalidation_headers_allowed=(
                    revalidation_preparation.conditional
                    if eligible_revalidation else False
                ),
                auth_scope_ref=(
                    str(plan_row["auth_scope_id"])
                    if plan_row["auth_scope_id"] else None
                ),
                egress_requirement=None,
                expected_operation_class="READ",
            ),
        )
        budget = conn.execute(
            "SELECT attempt_count, max_attempts FROM scrape_requests WHERE id = ?",
            (claim.request_id,),
        ).fetchone()
        try:
            dispatched = dispatch_http(
                conn,
                envelope,
                policy,
                attempt_count=int(budget["attempt_count"]),
                max_attempts=int(budget["max_attempts"]),
                expect=(
                    "SITEMAP"
                    if is_sitemap
                    else "JOB" if task_kind is AdapterTaskKind.DETAIL else "LIST"
                ),
                detail_closure_allowed=(task_kind is AdapterTaskKind.DETAIL),
            )
        except CapacityUnavailable:
            # No executor ran and execution_plan_id was not bound.  Return the
            # durable claim without consuming provider attempt budget.
            yield_unstarted_claim(
                conn, claim.request_id, claim.attempt_id,
                reason="CAPACITY_UNAVAILABLE",
            )
            deferred = True
            break
        except UnsupportedExecutionClass as exc:
            status = _commit_fenced(
                conn, run_id, claim, ts=ts, mutate=lambda _conn: None,
                transition=FenceTransition(
                    outcome="FAILED",
                    failure_kind="POLICY_REJECTED",
                    failure_json=bounded_json({
                        "reason": "UNSUPPORTED_EXECUTION_CLASS",
                        "execution_class": exc.execution_class,
                    }),
                ),
            )
            if status == "STALE":
                deferred = True
            elif status == "CANCELLED":
                cancelled = True
            else:
                run_degraded = True
            break
        except DispatchDeferred as exc:
            if exc.next_retry_at is not None:
                yield_unstarted_claim(
                    conn, claim.request_id, claim.attempt_id,
                    reason="RATE_COOLDOWN", next_retry_at=exc.next_retry_at,
                )
                deferred = True
                break
            status = _commit_fenced(
                conn, run_id, claim, ts=ts, mutate=lambda _conn: None,
                transition=FenceTransition(
                    outcome="FAILED",
                    failure_kind=exc.failure_kind or "BLOCKED",
                    failure_json=bounded_json({"reason": exc.reason}),
                ),
            )
            if status == "STALE":
                deferred = True
            elif status == "CANCELLED":
                cancelled = True
            else:
                run_degraded = True
            break
        except DispatchRejected as exc:
            status = _commit_fenced(
                conn,
                run_id,
                claim,
                ts=ts,
                mutate=lambda cursor_conn: _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    kind="SECURITY_POLICY",
                    ref="DENIED:DISPATCH_PLAN_INVALID",
                    detail={"reason": exc.reason},
                    content_hash=None,
                    now=db_utc_now(cursor_conn),
                ),
                transition=FenceTransition(
                    outcome="FAILED",
                    failure_kind=FailureKind.POLICY_REJECTED.value,
                    failure_json=bounded_json({
                        "kind": FailureKind.POLICY_REJECTED.value,
                        "reason": "DISPATCH_PLAN_INVALID",
                        "error_type": exc.reason,
                    }),
                ),
            )
            if status == "STALE":
                deferred = True
            elif status == "CANCELLED":
                cancelled = True
            else:
                run_degraded = True
            break
        except DispatchExecutionError as exc:
            # Expected source/network failures are normalized by the executor.
            # An escaped exception is local infrastructure and must not poison
            # source health.  It still consumes this dispatch attempt.
            local_failure = FailureRecord(
                kind=FailureKind.WORKER_CRASH,
                retryable=True,
                source_health_impact="NONE",
                source_id=plan_row["source_id"],
                binding_id=plan_row["binding_id"],
                adapter_id=plan_row["adapter_id"],
                adapter_version=plan_row["adapter_version"],
                run_id=run_id,
                request_id=claim.request_id,
                attempt_id=claim.attempt_id,
                details_redacted={"error_type": exc.error_type},
                observed_at=db_utc_now(conn),
            )
            local_retry = decide_retry(
                local_failure,
                page_class=None,
                attempt_count=int(budget["attempt_count"]),
                max_attempts=int(budget["max_attempts"]),
                headers={},
            )
            status = _commit_fenced(
                conn,
                run_id,
                claim,
                ts=ts,
                mutate=lambda cursor_conn: _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    kind="FAILURE",
                    ref="DISPATCH_EXECUTOR_ERROR",
                    detail={"error_type": exc.error_type},
                    content_hash=None,
                    now=db_utc_now(cursor_conn),
                ),
                transition=FenceTransition(
                    outcome=(
                        "RETRY_WAIT"
                        if local_retry.action is RetryAction.RETRY
                        else "FAILED"
                    ),
                    retry_delay_s=local_retry.delay_s,
                    failure_kind=FailureKind.WORKER_CRASH.value,
                    failure_json=bounded_json({
                        "kind": FailureKind.WORKER_CRASH.value,
                        "error_type": exc.error_type,
                        "retry_reason": local_retry.reason,
                        "retry_delay_s": local_retry.delay_s,
                        "budget_exhausted": local_retry.budget_exhausted,
                    }),
                ),
            )
            if status == "STALE":
                deferred = True
            elif status == "CANCELLED":
                cancelled = True
            else:
                run_degraded = True
            break
        except AuthorizationDenied as exc:
            if exc.decision.reason is AuthorizationReason.RUN_CANCELLED:
                finalized = abandon_request_for_cancellation(
                    conn, claim.request_id, attempt_id=claim.attempt_id,
                )
                cancelled = finalized
                deferred = not finalized
            else:
                finalized = finalize_authorization_denial(
                    conn, claim.request_id, claim.attempt_id,
                    decision=exc.decision,
                )
                if finalized:
                    run_degraded = True
                else:
                    deferred = True
            break
        except StaleOwnership:
            # Plan binding is the last ownership checkpoint before I/O.  If it
            # fails, no network request has started.  Reclamation does not
            # necessarily consume ownership (an unexpired lease under a
            # rotated epoch stays live), so inspect durable state instead of
            # assuming: a still-live attempt stays open for redrive, and only
            # a consumed attempt lets the pass terminalize below.
            reclaim_expired(conn)
            deferred = True
            live = conn.execute(
                "SELECT 1 FROM scrape_requests WHERE id = ?"
                " AND status = 'RUNNING' AND current_attempt_id = ?",
                (claim.request_id, claim.attempt_id),
            ).fetchone()
            ownership_lost = live is None
            break
        # Post-I/O durable evidence/cooldown time is sampled after the
        # network wait; claim-time timestamps must not shorten Retry-After or
        # source-protection cooldowns.
        ts = db_utc_now(conn)
        result = dispatched.result
        classification = dispatched.classification
        retry_decision = dispatched.retry
        revalidation_reuse = None
        if result.was_304 and eligible_revalidation:
            revalidation_reuse = resolve_304(
                conn,
                preparation=revalidation_preparation,
                plan_row=plan_row,
                request_plan=request_plan,
                expected_page_classes=expected_cache_classes,
                require_membership=is_enumeration,
                normalization_version=NORMALIZATION_VERSION,
            )
            if revalidation_reuse.accepted:
                # Keep transport ResultEnvelope untouched until its durable
                # fetch/result evidence records the real bodyless 304.
                classification = PageClassification(
                    PageClass(revalidation_reuse.validated_page_class),
                    {
                        "status_code": 304,
                        "revalidated_from_cache": True,
                        "cache_representation_id": revalidation_reuse.representation_id,
                        "retained_membership_count": len(revalidation_reuse.membership),
                    },
                )
        robots_policy = (
            parse_robots_result(result)
            if is_robots and retry_decision.action is RetryAction.SUCCEED
            else None
        )
        robots_sitemap_discovery = (
            discover_sitemaps_from_robots(robots_policy.lines)
            if robots_policy is not None
            else None
        )
        sitemap_parse_result = (
            parse_sitemap(
                result.body or b"",
                sitemap_url=result.final_url or request_plan.url,
                index_depth=sitemap_depth,
            )
            if is_sitemap and retry_decision.action is RetryAction.SUCCEED
            else None
        )
        if page_robots_decision is not None:
            result.robots_decision = page_robots_decision.kind.value
        elif robots_policy is not None:
            result.robots_decision = f"POLICY_{robots_policy.status.value}"
        network_failure_json = (
            bounded_json({
                "kind": retry_decision.failure_kind,
                "reason": retry_decision.reason,
                "retry_after": retry_decision.retry_after_raw,
                "retry_delay_s": retry_decision.delay_s,
                "budget_exhausted": retry_decision.budget_exhausted,
            }) if retry_decision.failure_kind else None
        )
        transition_box = {
            "value": FenceTransition(
                # Normative S3.2/S3.4 mapping: a retryable transient with
                # budget stays open (RETRY_WAIT); a definitive failure is a
                # failed acquisition unit (FAILED), never a false success;
                # only a clean valid result commits SUCCEEDED here — parser
                # PARTIAL/FAILURE kinds refine the transition inside mutate
                # after their accepted outputs are derived (below).
                outcome=(
                    "RETRY_WAIT" if retry_decision.action is RetryAction.RETRY
                    else "FAILED" if retry_decision.action is RetryAction.FAIL
                    else "SUCCEEDED"
                ),
                retry_delay_s=retry_decision.delay_s,
                failure_kind=retry_decision.failure_kind,
                failure_json=network_failure_json,
            )
        }

        def mutate(cursor_conn):
            fetch_attempt_id = _persist_fetch_attempt(
                cursor_conn,
                envelope,
                result,
                ts,
                cache_representation_id=(
                    revalidation_reuse.representation_id
                    if revalidation_reuse is not None and revalidation_reuse.accepted
                    else None
                ),
            )
            _record_evidence(
                cursor_conn,
                request_id=claim.request_id,
                attempt_id=claim.attempt_id,
                fetch_attempt_id=fetch_attempt_id,
                kind="RESULT_ENVELOPE",
                ref=result.body_ref,
                detail=result.as_evidence(),
                content_hash=result.normalized_content_hash,
                now=ts,
            )
            if (
                result.was_304
                and revalidation_reuse is not None
                and revalidation_reuse.accepted
            ):
                # Raw transport evidence above remains a bodyless 304. Only
                # parser-facing state is now restored from the held cache row.
                result.body = revalidation_reuse.body or b""
                result.content_type = revalidation_reuse.content_type
                result.body_hash = revalidation_reuse.body_hash
                result.normalized_content_hash = (
                    revalidation_reuse.normalized_content_hash
                )
                result.cache_representation_ref = (
                    f"cache://{revalidation_reuse.representation_id}"
                )
                result.body_ref = (
                    f"cache://{revalidation_reuse.representation_id}/body"
                )
            if result.security_policy_result != "ALLOWED":
                # the denial itself is durable evidence (04 §5.1): an empty
                # result must always be explainable as a policy outcome
                _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    fetch_attempt_id=fetch_attempt_id,
                    kind="SECURITY_POLICY",
                    ref=result.security_policy_result,
                    detail={"result": result.security_policy_result,
                            "requested_url": result.requested_url},
                    content_hash=None,
                    now=ts,
                )
            cursor_conn.execute(
                "UPDATE scrape_requests SET page_class = ? WHERE id = ?",
                (classification.state.value, claim.request_id),
            )
            _record_evidence(
                cursor_conn,
                request_id=claim.request_id,
                attempt_id=claim.attempt_id,
                fetch_attempt_id=fetch_attempt_id,
                kind="PAGE_VALIDITY",
                ref=f"validity://{classification.state.value}",
                detail=dict(classification.evidence),
                content_hash=result.normalized_content_hash,
                now=ts,
            )
            if retry_decision.action is not RetryAction.SUCCEED:
                cursor_conn.execute(
                    "UPDATE scrape_requests SET last_failure_kind = ?,"
                    " last_failure_json = ? WHERE id = ?",
                    (retry_decision.failure_kind, network_failure_json, claim.request_id),
                )
                rate_delay = retry_decision.delay_s
                if rate_delay is None and retry_decision.failure_kind in {
                    "RATE_LIMIT", "CHALLENGE", "BLOCKED"
                }:
                    rate_delay = 300.0
                record_rate_failure(
                    cursor_conn,
                    dispatched.rate_key,
                    failure_kind=retry_decision.failure_kind or "SOURCE_CHANGED",
                    delay_s=rate_delay,
                    retry_after_raw=retry_decision.retry_after_raw,
                    now=ts,
                    commit=False,
                )
                signal["value"] = (
                    "RETRY" if retry_decision.action is RetryAction.RETRY else "FAILURE"
                )
                return

            clean_rate_success = (
                is_robots
                or is_sitemap
                or classification.state in NORMAL_PARSE_CLASSES
                or (
                    task_kind is AdapterTaskKind.DETAIL
                    and classification.state in _CLOSURE_CLASSES
                )
            )
            if clean_rate_success:
                record_rate_success(
                    cursor_conn, dispatched.rate_key, now=ts, commit=False
                )
            if (
                result.was_304
                and eligible_revalidation
                and (revalidation_reuse is None or not revalidation_reuse.accepted)
            ):
                # Missing/pruned/incompatible 304 never means EMPTY. Queue one
                # ordinary durable unconditional replacement; no second hidden
                # network call occurs inside this attempt.
                usage = load_usage(cursor_conn, plan_id, now=ts)
                budget_decision = check_budget(
                    crawl_budget,
                    usage,
                    proposed_request_type=claim.request_type,
                    proposed_depth=claim_depth,
                    proposed_execution_class=plan_row["execution_class"],
                )
                reason = (
                    revalidation_reuse.reason
                    if revalidation_reuse is not None
                    else "304_WITHOUT_CACHE_REPRESENTATION"
                )
                _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    fetch_attempt_id=fetch_attempt_id,
                    kind="REVIEW",
                    ref="cache://304_REFETCH_REQUIRED",
                    detail={
                        "reason": reason,
                        "absence_authority": False,
                        "treated_as_empty": False,
                    },
                    content_hash=None,
                    now=ts,
                )
                if not budget_decision.allowed:
                    if (
                        (is_enumeration or not listing_identity_sufficient)
                        and not coverage_finalized
                    ):
                        degrade_coverage(
                            cursor_conn,
                            coverage_id,
                            reason=f"304 refetch blocked: {budget_decision.reason}",
                            commit=False,
                        )
                    signal["value"] = "BUDGET_STOP"
                    signal["degraded"] = True
                    signal["budget_exhausted"] = True
                    return
                refetch_payload = dict(claim_payload)
                refetch_payload["_host_revalidation"] = "UNCONDITIONAL"
                enqueue_request(
                    cursor_conn,
                    run_id=run_id,
                    run_source_plan_id=plan_id,
                    source_id=plan_row["source_id"],
                    binding_id=plan_row["binding_id"],
                    request_type=claim.request_type,
                    target_identity=request_plan.url,
                    payload=refetch_payload,
                    strategy=plan_row["strategy"],
                    execution_class=plan_row["execution_class"],
                    priority=claim_priority + 1,
                    depth=claim_depth,
                    parent_request_id=claim.request_id,
                    logical_key=f"unconditional-revalidation:{claim.request_id}",
                    now=ts,
                    commit=False,
                )
                signal["value"] = "REFETCH"
                return

            if is_robots:
                # This is policy evidence, not a source page parse. Even a
                # text/plain robots response classified UNEXPECTED_CONTENT by
                # the job-page classifier never reaches adapter.parse().
                _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    fetch_attempt_id=fetch_attempt_id,
                    kind="REVIEW",
                    ref="robots://POLICY",
                    detail=robots_policy.to_detail(),
                    content_hash=result.normalized_content_hash,
                    now=ts,
                )
                if robots_sitemap_discovery is not None:
                    sitemap_enqueued = enqueue_robots_sitemaps(
                        cursor_conn,
                        discovery=robots_sitemap_discovery,
                        run_id=run_id,
                        plan_row=plan_row,
                        parent_request_id=claim.request_id,
                        scope=crawl_scope,
                        budget=crawl_budget,
                        base_url=result.final_url or request_plan.url,
                        now=ts,
                    )
                    _record_sitemap_diagnostics(
                        cursor_conn,
                        request_id=claim.request_id,
                        attempt_id=claim.attempt_id,
                        fetch_attempt_id=fetch_attempt_id,
                        diagnostics=(
                            *robots_sitemap_discovery.diagnostics,
                            *sitemap_enqueued.diagnostics,
                        ),
                        content_hash=result.normalized_content_hash,
                        now=ts,
                    )
                    if (
                        robots_sitemap_discovery.candidates
                        or robots_sitemap_discovery.diagnostics
                        or sitemap_enqueued.diagnostics
                    ):
                        _record_evidence(
                            cursor_conn,
                            request_id=claim.request_id,
                            attempt_id=claim.attempt_id,
                            fetch_attempt_id=fetch_attempt_id,
                            kind="REVIEW",
                            ref="sitemap://ROBOTS_DISCOVERY",
                            detail={
                                "candidate_count": len(robots_sitemap_discovery.candidates),
                                **sitemap_enqueued.as_dict(),
                            },
                            content_hash=result.normalized_content_hash,
                            now=ts,
                        )
                signal["value"] = "ROBOTS_POLICY"
                return
            if is_sitemap:
                # Sitemap XML is host-owned discovery metadata. It never reaches
                # adapter.parse(), never becomes a coverage-contributing page,
                # and never grants absence authority. Every derived URL still
                # enters the ordinary scoped/budgeted durable frontier.
                parsed = sitemap_parse_result
                if parsed is None:
                    signal["value"] = "SITEMAP_DIAGNOSTIC"
                    return
                sitemap_enqueued = enqueue_sitemap_result(
                    cursor_conn,
                    parsed=parsed,
                    current_sitemap_depth=sitemap_depth,
                    run_id=run_id,
                    plan_row=plan_row,
                    parent_request_id=claim.request_id,
                    scope=crawl_scope,
                    budget=crawl_budget,
                    base_url=result.final_url or request_plan.url,
                    now=ts,
                    page_depth=max(1, claim_depth + 1),
                )
                _record_sitemap_diagnostics(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    fetch_attempt_id=fetch_attempt_id,
                    diagnostics=(*parsed.diagnostics, *sitemap_enqueued.diagnostics),
                    content_hash=result.normalized_content_hash,
                    now=ts,
                )
                _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    fetch_attempt_id=fetch_attempt_id,
                    kind="REVIEW",
                    ref="sitemap://DOCUMENT",
                    detail={**parsed.as_dict(), **sitemap_enqueued.as_dict()},
                    content_hash=result.normalized_content_hash,
                    now=ts,
                )
                signal["value"] = "SITEMAP_PROCESSED"
                return
            if classification.state in NORMAL_PARSE_CLASSES:
                if (
                    result.was_304
                    and revalidation_reuse is not None
                    and revalidation_reuse.accepted
                ):
                    touch_representation(
                        cursor_conn,
                        revalidation_reuse.representation_id,
                        now=ts,
                        commit=False,
                    )
                    if is_enumeration and not coverage_finalized:
                        restore_membership(
                            cursor_conn,
                            representation_id=revalidation_reuse.representation_id,
                            coverage_id=coverage_id,
                            plan_row=plan_row,
                            now=ts,
                            commit=False,
                        )
                    _record_evidence(
                        cursor_conn,
                        request_id=claim.request_id,
                        attempt_id=claim.attempt_id,
                        fetch_attempt_id=fetch_attempt_id,
                        kind="REVIEW",
                        ref="cache://304_REUSE",
                        detail={
                            "cache_representation_id": revalidation_reuse.representation_id,
                            "validated_page_class": revalidation_reuse.validated_page_class,
                            "membership_count": len(revalidation_reuse.membership),
                            "content_revision_increment": False,
                        },
                        content_hash=revalidation_reuse.normalized_content_hash,
                        now=ts,
                    )
                # EMPTY is a recognized non-job outcome (§21): the adapter parse
                # yields SUCCESS_EMPTY, which terminates the enumeration
                # authoritatively (ACQ-02).
                validated = ValidatedResultEnvelope(
                    envelope=result,
                    page_class=classification.state,
                    validation_evidence=classification.evidence,
                    security_policy_result=result.security_policy_result,
                    cache_representation_ref=result.cache_representation_ref,
                )
                parse_ctx = ParseContext(
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    run_source_plan_id=plan_id,
                    parser_version=plan_row["adapter_version"],
                    normalization_version=NORMALIZATION_VERSION,
                    idempotency_namespace=claim.request_id,
                )
                outcome_obj = adapter.parse(task, validated, ctx=parse_ctx)
                parse_attempt_id = _persist_parse_attempt(
                    cursor_conn, envelope, outcome_obj, validated, ts
                )
                if outcome_obj.failure is not None:
                    # A typed adapter failure (02 ACQ-02 FailureRecord) is
                    # durable on the request as well as on the parse attempt.
                    cursor_conn.execute(
                        "UPDATE scrape_requests SET last_failure_kind = ?,"
                        " last_failure_json = ? WHERE id = ?",
                        (
                            outcome_obj.failure.kind.value,
                            bounded_json(outcome_obj.failure.as_dict()),
                            claim.request_id,
                        ),
                    )

                # Parser PARTIAL/FAILURE kinds never default the owning request
                # to SUCCEEDED. This refinement only selects the verdict in
                # transition_box; the fence applies it after mutate completes,
                # so all accepted observations/evidence/children/local
                # obligations persist atomically under the same fence. A
                # retryable typed failure with budget left stays open
                # (RETRY_WAIT), otherwise the unit is terminally failed and
                # degraded. PARTIAL coverage degradation above is never
                # undone here, so authoritative coverage cannot be restored.
                if outcome_obj.kind in {ParseOutcomeKind.PARTIAL, ParseOutcomeKind.FAILURE}:
                    if outcome_obj.failure is not None:
                        parse_retry = decide_retry(
                            outcome_obj.failure,
                            page_class=None,
                            attempt_count=int(budget["attempt_count"]),
                            max_attempts=int(budget["max_attempts"]),
                            headers={},
                        )
                        if parse_retry.action is RetryAction.RETRY:
                            transition_box["value"] = FenceTransition(
                                outcome="RETRY_WAIT",
                                retry_delay_s=parse_retry.delay_s,
                                failure_kind=outcome_obj.failure.kind.value,
                                failure_json=bounded_json({
                                    **outcome_obj.failure.as_dict(),
                                    "retry_reason": parse_retry.reason,
                                    "retry_delay_s": parse_retry.delay_s,
                                    "budget_exhausted": parse_retry.budget_exhausted,
                                }),
                            )
                        else:
                            transition_box["value"] = FenceTransition(
                                outcome="FAILED",
                                failure_kind=outcome_obj.failure.kind.value,
                                failure_json=bounded_json({
                                    **outcome_obj.failure.as_dict(),
                                    "retry_reason": parse_retry.reason,
                                    "budget_exhausted": parse_retry.budget_exhausted,
                                }),
                            )
                    else:
                        transition_box["value"] = FenceTransition(
                            outcome="FAILED",
                            failure_json=bounded_json({
                                "kind": outcome_obj.kind.value,
                                "reason": "non-success parse outcome without typed failure",
                            }),
                        )

                for observation in outcome_obj.observations:
                    # 02 §32 origin resolution — host-owned and network-inert:
                    # it consumes the recorded redirect chain plus the
                    # observation's own link candidates.
                    origin = resolve_origin(
                        discovery_url=source["entry_url"] if source else None,
                        raw_source_url=observation.raw_url,
                        canonical_job_url=observation.canonical_url_candidate,
                        application_url=observation.application_url_candidate,
                        redirect_chain=result.redirect_chain,
                        final_url=result.final_url,
                        observed_at=ts,
                    )
                    ingest_observation(
                        cursor_conn,
                        request_id=claim.request_id,
                        attempt_id=claim.attempt_id,
                        observation=observation,
                        source_id=plan_row["source_id"],
                        binding_id=plan_row["binding_id"],
                        adapter_id=plan_row["adapter_id"],
                        adapter_version=plan_row["adapter_version"],
                        strategy=plan_row["strategy"],
                        execution_class=plan_row["execution_class"],
                        observed_at=ts,
                        now=ts,
                        fetch_attempt_id=fetch_attempt_id,
                        parse_attempt_id=parse_attempt_id,
                        origin=origin,
                        content_kind=result.content_kind,
                        same_host_as_source=posting_host_matches_source(
                            source, observation
                        ),
                        source_family=source["source_family"] if source else None,
                    )
                    if (
                        observation.source_job_id
                        and (is_enumeration or not listing_identity_sufficient)
                        and not coverage_finalized
                    ):
                        record_seen_identity(
                            cursor_conn, coverage_id, observation.source_job_id,
                            evidence_ref=observation.raw_url, commit=False,
                        )
                if (
                    outcome_obj.kind is ParseOutcomeKind.PARTIAL
                    and (is_enumeration or not listing_identity_sufficient)
                    and not coverage_finalized
                ):
                    degrade_coverage(
                        cursor_conn,
                        coverage_id,
                        reason="degraded by PARTIAL acquisition unit",
                        commit=False,
                    )
                    signal["degraded"] = True
                child_degraded, child_budget_exhausted = _dispatch_child_tasks(
                    cursor_conn,
                    outcome_obj=outcome_obj,
                    task_kind=task_kind,
                    claim=claim,
                    plan_row=plan_row,
                    run_id=run_id,
                    ts=ts,
                    crawl_scope=crawl_scope,
                    crawl_budget=crawl_budget,
                    base_url=result.final_url or request_plan.url,
                )
                if child_degraded:
                    signal["degraded"] = True
                    if (is_enumeration or not listing_identity_sufficient) and not coverage_finalized:
                        degrade_coverage(
                            cursor_conn, coverage_id,
                            reason="child frontier proposal refused", commit=False,
                        )
                if child_budget_exhausted:
                    signal["budget_exhausted"] = True

                next_cursor = (
                    adapter.next_cursor(task, outcome_obj, cursor, ctx=parse_ctx)
                    if task_kind in _CURSOR_TASK_KINDS
                    else None
                )
                if (
                    not result.was_304
                    and result.status_code is not None
                    and 200 <= int(result.status_code) < 300
                    and outcome_obj.kind
                    in {ParseOutcomeKind.SUCCESS_WITH_JOBS, ParseOutcomeKind.SUCCESS_EMPTY}
                ):
                    representation_id = store_representation(
                        cursor_conn,
                        plan_row=plan_row,
                        request_plan=request_plan,
                        validated_page_class=classification.state.value,
                        body=result.body,
                        body_hash=result.body_hash,
                        normalized_content_hash=result.normalized_content_hash,
                        content_type=result.content_type,
                        response_headers=result.headers_redacted,
                        observations=outcome_obj.observations,
                        membership_complete=is_enumeration,
                        normalization_version=NORMALIZATION_VERSION,
                        now=ts,
                        commit=False,
                    )
                    if representation_id is not None:
                        result.cache_representation_ref = f"cache://{representation_id}"
                        bind_fetch_representation(
                            cursor_conn,
                            fetch_attempt_id,
                            representation_id,
                            commit=False,
                        )
                        _record_evidence(
                            cursor_conn,
                            request_id=claim.request_id,
                            attempt_id=claim.attempt_id,
                            fetch_attempt_id=fetch_attempt_id,
                            parse_attempt_id=parse_attempt_id,
                            kind="REVIEW",
                            ref=f"cache://{representation_id}",
                            detail={
                                "reason": "REPRESENTATION_STORED",
                                "validated_page_class": classification.state.value,
                                "membership_complete": bool(is_enumeration),
                                "normalized_content_hash": result.normalized_content_hash,
                            },
                            content_hash=result.normalized_content_hash,
                            now=ts,
                        )
                if next_cursor is not None:
                    new_guard, pagination_decision = advance_guard(
                        pagination_guard,
                        PaginationSignature.from_values(
                            next_cursor=next_cursor.state_json,
                            next_url=result.final_url or request_plan.url,
                            page_hash=result.normalized_content_hash,
                            job_ids=tuple(
                                o.source_job_id for o in outcome_obj.observations
                                if o.source_job_id
                            ),
                            observations_added=len(outcome_obj.observations),
                            recognized_empty=(outcome_obj.kind is ParseOutcomeKind.SUCCESS_EMPTY),
                        ),
                        stop_policy,
                    )
                    if pagination_decision.kind is not PaginationStopKind.CONTINUE:
                        if (is_enumeration or not listing_identity_sufficient) and not coverage_finalized:
                            degrade_coverage(
                                cursor_conn, coverage_id,
                                reason=pagination_decision.reason, commit=False,
                            )
                        _record_evidence(
                            cursor_conn,
                            request_id=claim.request_id,
                            attempt_id=claim.attempt_id,
                            fetch_attempt_id=fetch_attempt_id,
                            parse_attempt_id=parse_attempt_id,
                            kind="FAILURE",
                            ref=f"pagination://{pagination_decision.kind.value}",
                            detail={
                                "reason": pagination_decision.reason,
                                "stop_kind": pagination_decision.kind.value,
                                "candidate_cursor_hash": PaginationSignature.from_values(
                                    next_cursor=next_cursor.state_json
                                ).next_cursor_hash,
                            },
                            content_hash=result.normalized_content_hash,
                            now=ts,
                        )
                        transition_box["value"] = FenceTransition(
                            outcome="FAILED",
                            failure_kind=pagination_decision.failure_kind,
                            failure_json=bounded_json({
                                "reason": pagination_decision.reason,
                                "stop_kind": pagination_decision.kind.value,
                            }),
                        )
                        signal["value"] = "PAGINATION_STOP"
                        signal["degraded"] = True
                        return

                    continuation_type = (
                        "SOURCE_CRAWL"
                        if task_kind is AdapterTaskKind.CRAWL
                        else "LIST_FETCH"
                    )
                    usage = load_usage(cursor_conn, plan_id, now=ts)
                    budget_decision = check_budget(
                        crawl_budget, usage, proposed_request_type=continuation_type,
                        proposed_depth=claim_depth,
                        proposed_execution_class=plan_row["execution_class"],
                    )
                    if not budget_decision.allowed:
                        if (is_enumeration or not listing_identity_sufficient) and not coverage_finalized:
                            degrade_coverage(
                                cursor_conn, coverage_id,
                                reason=f"crawler budget exhausted: {budget_decision.reason}",
                                commit=False,
                            )
                        _record_evidence(
                            cursor_conn,
                            request_id=claim.request_id,
                            attempt_id=claim.attempt_id,
                            fetch_attempt_id=fetch_attempt_id,
                            parse_attempt_id=parse_attempt_id,
                            kind="REVIEW",
                            ref="crawler://BUDGET_EXHAUSTED",
                            detail={"reason": budget_decision.reason},
                            content_hash=result.normalized_content_hash,
                            now=ts,
                        )
                        signal["value"] = "BUDGET_STOP"
                        signal["degraded"] = True
                        signal["budget_exhausted"] = True
                        return

                    save_crawl_cursor(
                        cursor_conn,
                        run_source_plan_id=plan_id,
                        plan_row=plan_row,
                        cursor=_bind_crawl_cursor_to_plan(next_cursor, plan_row),
                        guard_state=new_guard,
                        now=ts,
                    )
                    enqueue_request(
                        cursor_conn,
                        run_id=run_id,
                        run_source_plan_id=plan_id,
                        source_id=plan_row["source_id"],
                        binding_id=plan_row["binding_id"],
                        request_type=continuation_type,
                        target_identity=request_plan.url,
                        logical_key=next_cursor.state_json,
                        payload=(
                            {"role": "PAGE", "cursor_state": next_cursor.state_json}
                            if continuation_type == "SOURCE_CRAWL"
                            else {}
                        ),
                        strategy=plan_row["strategy"],
                        execution_class=plan_row["execution_class"],
                        depth=claim_depth,
                        parent_request_id=claim.request_id,
                        now=ts,
                        commit=False,
                    )
                    signal["value"] = "CONTINUE"
                    return
                if (
                    outcome_obj.kind is ParseOutcomeKind.PARTIAL
                    or outcome_obj.continuation_required
                ):
                    signal["value"] = "PARTIAL"
                    return
                if outcome_obj.kind is ParseOutcomeKind.FAILURE:
                    signal["value"] = "FAILURE"
                    return
                # A recognized empty page is terminal only because the adapter
                # produced no continuation cursor; emptiness alone never stops
                # a cursor-bearing crawl page.
                signal["value"] = "TERMINAL" if is_enumeration else "JOBS"
                return
            if task_kind is AdapterTaskKind.DETAIL and classification.state in _CLOSURE_CLASSES:
                # ACQ-02: a typed closure/missing outcome is durable evidence
                # that this identity is gone at the provider.  It is *not* a
                # failure, *not* an observation, and *not* absence authority
                # for any other identity.
                _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    fetch_attempt_id=fetch_attempt_id,
                    kind="REVIEW",
                    ref=f"closure://{classification.state.value}",
                    detail={
                        "reason": "DETAIL_CLOSURE_OR_MISSING",
                        "classification": classification.state.value,
                        "target_reference": target_reference,
                        "absence_authority": False,
                        "final_url": result.final_url,
                        "status_code": result.status_code,
                    },
                    content_hash=result.normalized_content_hash,
                    now=ts,
                )
                signal["value"] = "CLOSURE"
                return
            signal["value"] = "INVALID"

        commit_status = _commit_fenced(
            conn,
            run_id,
            claim,
            ts=ts,
            mutate=mutate,
            transition=transition_box["value"],
            transition_resolver=lambda: transition_box["value"],
        )
        release_hold(
            conn,
            revalidation_preparation.hold_id,
            now=db_utc_now(conn),
            commit=True,
        )
        if commit_status != "COMMITTED":
            if commit_status == "CANCELLED" or run_is_cancelled(conn, run_id):
                cancelled = True
            elif commit_status == "STALE":
                deferred = True
                # STALE conflates two cases: a reclaimed (consumed) attempt
                # versus a still-live one.  Only a consumed attempt lets the
                # pass terminalize; a live attempt stays open for redrive.
                live = conn.execute(
                    "SELECT 1 FROM scrape_requests WHERE id = ?"
                    " AND status = 'RUNNING' AND current_attempt_id = ?",
                    (claim.request_id, claim.attempt_id),
                ).fetchone()
                ownership_lost = live is None
            break

        state_changed = True
        if is_robots:
            pass
        elif is_enumeration:
            pages += 1
        elif claim.request_type in _DETAIL_REQUEST_TYPES:
            details += 1
        value = signal.get("value")
        if signal.get("budget_exhausted"):
            durable_budget_exhausted = True
        if value in (
            "INVALID", "FAILURE", "PARTIAL", "REFUSED", "RETRY",
            "PAGINATION_STOP", "BUDGET_STOP",
        ) and not is_sitemap:
            # Sitemap acquisition is advisory discovery metadata. A failed or
            # malformed sitemap is durable diagnostic evidence, but it cannot
            # invalidate an otherwise complete enumeration or create/restore
            # absence authority.
            run_degraded = True
            if is_enumeration or not listing_identity_sufficient:
                coverage_degraded = True
        if signal.get("degraded"):
            # PARTIAL that also continued (CONTINUE path): the generation was
            # degraded under the fence; the run is degraded too.
            run_degraded = True
            coverage_degraded = True
        if is_enumeration and value in ("EMPTY", "TERMINAL"):
            # 03 §40: only a recognized terminal enumeration — an accepted
            # empty result or a complete listing with no further cursor —
            # proves membership.  Budgets, refusals and parse failures never
            # do.
            terminal = True

    if deferred and not ownership_lost:
        # No provider I/O happened and no attempt was consumed.  Leave the
        # durable frontier/group open for a later service pass rather than
        # manufacturing a terminal outcome.  Ownership loss is excluded: a
        # reclaimed attempt was consumed, so the pass terminalizes below.
        return
    open_child_work = _open_acquisition_requests(conn, plan_id)
    if open_child_work and not (pages or details or terminal or cancelled):
        # A consumed/lost attempt can be reclaimed into RETRY_WAIT.  That is
        # still accepted future work, not a terminal failure.  Leave the plan
        # and coverage generation open so a later pass can claim the retry.
        return
    if not (state_changed or pages or details or terminal or cancelled or ownership_lost):
        # This pass consumed and produced nothing (e.g. only epoch-orphaned
        # RUNNING work remains, still live under an unexpired lease): stay
        # open for redrive rather than manufacturing a terminal outcome.
        # Epoch invalidation alone must never close a plan/group/run.
        return
    if cancelled:
        outcome = "CANCELLED"
    elif terminal and not run_degraded and open_child_work == 0:
        # Run satisfaction is the accepted-work barrier: even a COMPLETE
        # listing cannot terminalize the run while child work is open.
        outcome = "SATISFIED"
    elif pages or details or terminal:
        outcome = "SATISFIED_PARTIAL"
    else:
        outcome = "FAILED"

    if not coverage_finalized:
        # Enumeration completeness is a different truth from run completion.
        # DETAIL work joins this barrier only for adapters whose listing does
        # not itself prove stable membership.
        coverage_barrier_open = (
            open_child_work if not listing_identity_sufficient else 0
        )
        final_usage = load_usage(conn, plan_id, now=db_utc_now(conn))
        coverage_budget_exhausted = (
            durable_budget_exhausted
            or final_usage.pages_completed >= crawl_budget.max_pages
            or final_usage.bytes_downloaded >= crawl_budget.max_bytes
            or final_usage.elapsed_s >= crawl_budget.max_runtime_s
            or (
                not listing_identity_sufficient
                and final_usage.detail_requests_created >= crawl_budget.max_detail_requests
            )
        )
        # A degraded generation holds no terminal-enumeration authority even
        # when its last page happened to be short/empty: "the enumeration
        # ended" is only membership proof when every page in it was whole
        # (ACQ-03, RUN-13).
        terminal_proven = terminal and not coverage_degraded
        if terminal_proven and coverage_barrier_open == 0:
            completion_state = "COMPLETE"
            stop_reason = "terminal cursor"
        elif cancelled:
            completion_state = "CANCELLED"
            stop_reason = "cancelled"
        elif coverage_budget_exhausted and coverage_barrier_open:
            completion_state = "BUDGET_EXHAUSTED"
            stop_reason = "host crawler budget exhausted before complete coverage"
        elif coverage_barrier_open:
            completion_state = "PARTIAL"
            stop_reason = "open contributing work remains"
        elif terminal and coverage_degraded:
            completion_state = "PARTIAL"
            stop_reason = "degraded by PARTIAL acquisition unit"
        else:
            completion_state = "PARTIAL"
            stop_reason = "driver stop"
        try:
            finalize_coverage(
                conn,
                coverage_id,
                completion_state=completion_state,
                stop_reason=stop_reason,
                terminal_enumeration_proven=terminal_proven,
                pages_completed=final_usage.pages_completed,
                now=db_utc_now(conn),
            )
        except CoverageFinalizationError:
            # A refused COMPLETE barrier is conservatively finalized PARTIAL;
            # programming/SQL errors are not swallowed as ordinary coverage.
            finalize_coverage(
                conn,
                coverage_id,
                completion_state="PARTIAL",
                stop_reason="driver stop",
                terminal_enumeration_proven=False,
                pages_completed=final_usage.pages_completed,
                now=db_utc_now(conn),
            )
    set_group_outcome(conn, plan_id, outcome, now=db_utc_now(conn))


def posting_host_matches_source(source_row, observation) -> bool:
    """01 §39 employer-vs-aggregator input: is the posting link *on the host
    this source is about*?  Derived from already-recorded URLs only — no new
    request is made to find out."""
    from urllib.parse import urlsplit

    def host_of(value):
        if not value:
            return ""
        # ``canonical_host`` stores a bare host, while URLs carry a scheme;
        # both spellings describe the same identity
        text = value if "://" in value else f"//{value}"
        try:
            return (urlsplit(text).hostname or "").lower()
        except Exception:
            return ""

    entry = ""
    if source_row is not None:
        keys = list(source_row.keys())
        # an operator-recorded canonical host is the authoritative statement of
        # what this source *is*; the entry URL is the fallback
        if "canonical_host" in keys and source_row["canonical_host"]:
            entry = host_of(source_row["canonical_host"])
        # NOTE: nothing writes sources.canonical_host yet (v3 column, no
        # registration field); it is preferred here so registration data, when
        # it arrives, tightens the match instead of silently being ignored.
        if not entry:
            entry = host_of(source_row["entry_url"])
    if not entry:
        return False
    # the *posting* links only: a feed item's raw_url is usually the page it
    # was found on, which says nothing about where the posting lives
    for candidate in (
        getattr(observation, "canonical_url_candidate", None),
        getattr(observation, "application_url_candidate", None),
    ):
        if candidate and host_of(candidate) == entry:
            return True
    return False


def _record_sitemap_diagnostics(
    conn,
    *,
    request_id: str,
    attempt_id: str | None,
    fetch_attempt_id: str | None,
    diagnostics,
    content_hash: str | None,
    now: str,
) -> None:
    # Persist bounded sitemap diagnostics as explicitly non-authoritative evidence.
    for diagnostic in diagnostics:
        _record_evidence(
            conn,
            request_id=request_id,
            attempt_id=attempt_id,
            fetch_attempt_id=fetch_attempt_id,
            kind="REVIEW",
            ref=f"sitemap://{diagnostic.code}",
            detail=diagnostic.as_dict(),
            content_hash=content_hash,
            now=now,
        )


def _record_evidence(
    conn,
    *,
    request_id: str,
    attempt_id: str | None,
    fetch_attempt_id: str | None = None,
    parse_attempt_id: str | None = None,
    observation_id: str | None = None,
    kind: str,
    ref: str | None,
    detail: dict,
    content_hash: str | None,
    now: str,
) -> str:
    """One durable evidence row (03 §30): hashes, refs and redacted metadata."""
    evidence_id = new_id("ev")
    conn.execute(
        """
        INSERT INTO acquisition_evidence (
            id, request_id, attempt_id, fetch_attempt_id, parse_attempt_id,
            observation_id, kind, ref, detail_json, content_hash, observed_at,
            created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            request_id,
            attempt_id,
            fetch_attempt_id,
            parse_attempt_id,
            observation_id,
            kind,
            ref,
            bounded_json(detail),
            content_hash,
            now,
            now,
        ),
    )
    return evidence_id


def _persist_fetch_attempt(
    conn, envelope, result, ts, *, cache_representation_id: str | None = None
) -> str:
    """Durable fetch attempt for one ResultEnvelope (02 §11.3, 03 §30).

    The attempt's pre-dispatch ``execution_plan_id`` and the returned result
    must describe the same immutable execution identity.  This check runs
    inside the owning fenced transaction, so an executor/caller bug cannot
    attach a result from another plan/request/attempt to the current owner.
    """
    bound = conn.execute(
        "SELECT execution_plan_id FROM request_attempts"
        " WHERE attempt_id = ? AND request_id = ?",
        (envelope.attempt_id, envelope.request_id),
    ).fetchone()
    if bound is None or bound["execution_plan_id"] != envelope.plan_id:
        raise ValueError("fetch result has no matching bound execution plan")
    identity_pairs = (
        ("execution_plan_id", result.execution_plan_id, envelope.plan_id),
        ("request_id", result.request_id, envelope.request_id),
        ("attempt_id", result.attempt_id, envelope.attempt_id),
        ("run_source_plan_id", result.run_source_plan_id, envelope.run_source_plan_id),
        ("source_id", result.source_id, envelope.source_id),
        ("binding_id", result.binding_id, envelope.binding_id),
        ("binding_revision_id", result.binding_revision_id, envelope.binding_revision_id),
        ("adapter_id", result.adapter_id, envelope.adapter_id),
        ("adapter_version", result.adapter_version, envelope.adapter_version),
        ("strategy", result.strategy, envelope.strategy),
        ("execution_class", result.execution_class, envelope.execution_class),
    )
    mismatches = [name for name, actual, expected in identity_pairs if actual != expected]
    if mismatches:
        raise ValueError(
            "ResultEnvelope identity does not match bound execution plan: "
            + ", ".join(mismatches)
        )
    fetch_attempt_id = new_id("fa")
    conn.execute(
        """
        INSERT INTO fetch_attempts (
            id, attempt_id, request_id, requested_url, final_url, status_code,
            content_type, body_hash, normalized_content_hash, body_ref,
            bytes_downloaded, duration_ms, redirect_chain_json, was_304,
            failure_kind, failure_json, fetched_at,
            contract_version, execution_plan_id, headers_redacted_json,
            validators_sent_json, robots_decision, transport, browser_used,
            resource_blocking_applied, security_policy_json,
            structured_payload_ref, cache_representation_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fetch_attempt_id,
            envelope.attempt_id,
            envelope.request_id,
            result.requested_url,
            result.final_url,
            result.status_code,
            result.content_type,
            result.body_hash,
            result.normalized_content_hash,
            result.body_ref,
            result.bytes_downloaded,
            result.duration_ms,
            json.dumps(result.redirect_chain),
            1 if result.was_304 else 0,
            result.failure.kind.value if result.failure else None,
            json.dumps(result.failure.as_dict()) if result.failure else None,
            ts,
            result.contract_version,
            envelope.plan_id,
            json.dumps(dict(result.headers_redacted), sort_keys=True, default=str),
            json.dumps(sorted(result.validators_sent)),
            result.robots_decision,
            result.transport,
            1 if result.browser_used else 0,
            1 if result.resource_blocking_applied else 0,
            json.dumps(
                {
                    "result": result.security_policy_result,
                    "policy_snapshot_ref": envelope.policy_snapshot_ref,
                    "permission_profile_revision": envelope.permission_profile_revision,
                },
                sort_keys=True,
                default=str,
            ),
            result.structured_payload_ref,
            cache_representation_id,
        ),
    )
    return fetch_attempt_id


def _persist_parse_attempt(conn, envelope, outcome, validated, ts) -> str:
    """Durable parse attempt under the ACQ-09 contract (02 §11.3, 03 §30)."""
    parse_attempt_id = new_id("pa")
    conn.execute(
        """
        INSERT INTO parse_attempts (
            id, attempt_id, request_id, parser_kind, parser_version,
            outcome_kind, observation_count, child_task_count, failure_kind,
            failure_json, parsed_at, contract_version, validated_page_class,
            validation_evidence_json, result_envelope_ref,
            cursor_proposal_json, coverage_proposal_json,
            continuation_required, closure_evidence_json, review_evidence_json,
            evidence_refs_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            parse_attempt_id,
            envelope.attempt_id,
            envelope.request_id,
            envelope.adapter_id,
            envelope.adapter_version,
            outcome.kind.value,
            len(outcome.observations),
            len(outcome.discovered_tasks),
            outcome.failure.kind.value if outcome.failure else None,
            json.dumps(outcome.failure.as_dict()) if outcome.failure else None,
            ts,
            outcome.contract_version,
            validated.validated_page_class.value,
            json.dumps(dict(validated.validation_evidence), sort_keys=True, default=str),
            validated.result_envelope_ref,
            # already serialized JSON text (CrawlCursor.state_json)
            outcome.cursor_proposal.state_json
            if outcome.cursor_proposal is not None
            else None,
            json.dumps(outcome.coverage_proposal, sort_keys=True, default=str)
            if outcome.coverage_proposal
            else None,
            1 if outcome.continuation_required else 0,
            json.dumps([dict(e) for e in outcome.closure_or_missing_evidence], default=str),
            json.dumps([dict(e) for e in outcome.review_evidence], default=str),
            json.dumps(list(outcome.evidence_refs), default=str),
        ),
    )
    return parse_attempt_id


__all__ = ["execute_run", "posting_host_matches_source", "source_policy"]
