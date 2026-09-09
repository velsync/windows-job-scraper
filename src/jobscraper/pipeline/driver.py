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

from jobscraper.acquisition.atsendpoints import spec_for_provider
from jobscraper.acquisition.envelope import (
    ExecutionPlanEnvelope,
    RequestPlan,
    validate_envelope,
)
from jobscraper.acquisition.failures import FailureKind
from jobscraper.acquisition.httpexec import execute_request
from jobscraper.acquisition.origin import resolve_origin
from jobscraper.acquisition.pagevalidity import PageClass, classify_page
from jobscraper.adapters.contract import (
    NORMAL_PARSE_CLASSES,
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    ParseContext,
    ParseOutcomeKind,
    PlanningContext,
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
    finalize_coverage,
    open_or_resume_coverage,
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
from jobscraper.runtime.claims import StaleOwnership, claim_next_request, reclaim_expired
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.requests import (
    ACQUISITION_REQUEST_TYPES,
    enqueue_request,
)
from jobscraper.runtime.runs import (
    TERMINAL_GROUP_OUTCOMES,
    aggregate_run,
    mark_run_started,
    set_group_outcome,
)

#: Host-side budget on enumeration pages per plan per run (RUN-09: bounded).
MAX_PAGES_PER_RUN = 50
#: Host-side budget on typed detail child requests per plan per run (ACQ-04).
#: The adapter's own stop policy is the tighter, per-source bound; this is the
#: host's refusal to be talked into unbounded breadth by content or config.
MAX_DETAIL_REQUESTS_PER_RUN = 200

#: Request types that constitute enumeration pages for coverage purposes
#: (03 §40: coverage links the contributing *enumeration* requests/pages).
_ENUMERATION_REQUEST_TYPES = frozenset({"LIST_FETCH", "SOURCE_CRAWL"})
#: Typed child work (ACQ-02 DETAIL_FETCH): budgeted separately, and part of
#: the absence barrier only when listing identity is NOT sufficient.
_DETAIL_REQUEST_TYPES = frozenset({"DETAIL_FETCH"})
#: Classifier states that, for a DETAIL task, are typed closure/missing
#: evidence rather than failures (ACQ-02): the job is gone at the provider.
_CLOSURE_CLASSES = frozenset({PageClass.NOT_FOUND, PageClass.JOB_CLOSED})
_OPEN_REQUEST_STATUSES = ("PENDING", "RUNNING", "RETRY_WAIT")


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
    if adapter_id == "greenhouse" and host:
        spec = spec_for_provider("GREENHOUSE")
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
        allowed_hosts=frozenset(allowed_hosts) if allowed_hosts else None,
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


def _load_cursor(conn: sqlite3.Connection, plan_row: sqlite3.Row) -> CrawlCursor | None:
    row = conn.execute(
        """
        SELECT * FROM crawl_cursors
        WHERE binding_id = ? AND adapter_id = ? AND adapter_version = ?
          AND cursor_schema_version = ?
        """,
        (
            plan_row["binding_id"],
            plan_row["adapter_id"],
            plan_row["adapter_version"],
            plan_row["cursor_schema_version"],
        ),
    ).fetchone()
    if row is None:
        return None
    return CrawlCursor(
        source_id=row["source_id"],
        binding_id=row["binding_id"],
        adapter_id=row["adapter_id"],
        adapter_version=row["adapter_version"],
        cursor_schema_version=row["cursor_schema_version"],
        state_json=row["state_json"],
        checkpoint_at=row["checkpoint_at"],
    )


def _save_cursor(
    conn: sqlite3.Connection, plan_row: sqlite3.Row, cursor: CrawlCursor, now: str
) -> None:
    conn.execute(
        """
        INSERT INTO crawl_cursors (id, source_id, binding_id, adapter_id,
            adapter_version, cursor_schema_version, state_json, checkpoint_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (binding_id, adapter_id, adapter_version, cursor_schema_version)
        DO UPDATE SET state_json = excluded.state_json,
                      checkpoint_at = excluded.checkpoint_at
        """,
        (
            new_id("cur"),
            cursor.source_id or plan_row["source_id"],
            cursor.binding_id or plan_row["binding_id"],
            cursor.adapter_id,
            cursor.adapter_version,
            cursor.cursor_schema_version,
            cursor.state_json,
            now,
        ),
    )


def execute_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    worker_id: str = "service",
    now: str | None = None,
) -> str:
    """Drive one run to completion (bounded) and return its aggregate status."""
    from jobscraper.runtime.clock import db_utc_now

    ts = now or db_utc_now(conn)
    mark_run_started(conn, run_id, now=ts)
    plan_rows = conn.execute(
        "SELECT * FROM run_source_plans WHERE run_id = ? ORDER BY source_plan_group_id, fallback_rank",
        (run_id,),
    ).fetchall()

    for plan_row in plan_rows:
        _execute_plan(conn, run_id, plan_row, worker_id=worker_id, now=ts)

    # Host-native obligations drain before the run finalizes (a run must
    # not report finished while accepted observations are unprocessed).
    drain_all_obligations(conn, now=ts)
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
) -> None:
    """Turn an adapter's proposed child tasks into durable typed requests.

    ACQ-02/ACQ-04: the adapter *proposes*; the host decides what exists.  In
    Slice 2 the only dispatchable child kind is DETAIL, and only from an
    enumeration pass — crawl breadth is ROAD-04 work, so anything else is
    recorded as durable review evidence instead of being silently dropped.
    """
    for discovered in outcome_obj.discovered_tasks:
        if task_kind is not AdapterTaskKind.ENUMERATE or discovered.kind != "DETAIL":
            _record_evidence(
                conn,
                request_id=claim.request_id,
                attempt_id=claim.attempt_id,
                fetch_attempt_id=None,
                kind="REVIEW",
                ref=f"child-task://{discovered.kind}",
                detail={
                    "reason": "CHILD_TASK_NOT_DISPATCHED",
                    "kind": discovered.kind,
                    "depth": discovered.depth,
                    "logical_key": discovered.logical_key,
                    "target_reference": discovered.target_reference,
                    "from_task_kind": task_kind.value,
                },
                content_hash=None,
                now=ts,
            )
            continue
        enqueue_request(
            conn,
            run_id=run_id,
            run_source_plan_id=plan_row["id"],
            source_id=plan_row["source_id"],
            binding_id=plan_row["binding_id"],
            request_type="DETAIL_FETCH",
            target_identity=discovered.target_reference,
            logical_key=discovered.logical_key,
            payload={
                "kind": discovered.kind,
                "target_reference": discovered.target_reference,
                "logical_key": discovered.logical_key,
                "depth": discovered.depth,
            },
            priority=discovered.priority,
            depth=discovered.depth,
            parent_request_id=claim.request_id,
            execution_class=plan_row["execution_class"],
            strategy=plan_row["strategy"],
            now=ts,
            commit=False,
        )


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


def _commit_fenced(conn, run_id, claim, *, ts, mutate) -> str:
    """One fenced commit for one claim; report ownership loss honestly.

    Returns ``COMMITTED`` / ``CANCELLED`` / ``STALE``.  The fence owns the
    claim-and-commit contract (RUN-07, 03 §18): a refused late commit rolls
    back everything this attempt wrote, and the driver must not count it as a
    completed page.
    """
    from jobscraper.runtime.clock import db_utc_now

    try:
        with fenced_commit(conn, claim.request_id, claim.attempt_id, now=ts, mutate=mutate):
            pass
        return "COMMITTED"
    except StaleOwnership:
        if run_is_cancelled(conn, run_id):
            # §18: the fence refused a late acquisition commit; this page's
            # outputs were rolled back, the worker cooperatively abandons the
            # request, and the plan ends CANCELLED.
            abandon_request_for_cancellation(conn, claim.request_id, now=db_utc_now(conn))
            return "CANCELLED"
        # Ownership lost without cancellation (expired lease): an expired lease
        # is already lost ownership (RUN-07), so the expired request is
        # reclaimed for retry and the driver stops claiming this plan rather
        # than reviving the dead lease.
        reclaim_expired(conn)
        return "STALE"


def _execute_plan(
    conn: sqlite3.Connection,
    run_id: str,
    plan_row: sqlite3.Row,
    *,
    worker_id: str,
    now: str,
) -> None:
    """Drive one immutable RunSourcePlan to an honest terminal state.

    S2.5 generalization (ARC-04.3, ACQ-02/ACQ-04, 03 RUN-01/RUN-09, §40):

    * the adapter is whatever the pinned binding revision names, built only
      through the registry — the host special-cases no adapter identity;
    * request type -> task kind is durable data, so enumeration pages and the
      typed detail children they produce are planned by the same adapter;
    * a plan is never terminalized while its own accepted child work is open,
      and re-driving a finished plan is an idempotent no-op;
    * host budgets bound enumeration pages and detail requests separately, and
      hitting one yields BUDGET_EXHAUSTED — never absence authority.
    """
    from jobscraper.runtime.clock import db_utc_now

    plan_id = plan_row["id"]
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
            scope_key="full-source",
            generation_key=f"run-{run_id}",
            coverage_authority="AUTHORITATIVE_FULL_SOURCE",
            now=now,
        )

    terminal = durable_complete is not None
    coverage_degraded = False
    run_degraded = False
    cancelled = False
    pages = 0
    details = 0
    while True:
        if run_is_cancelled(conn, run_id):
            # §18: no new acquisition driving once cancellation is durable
            cancelled = True
            break
        allowed = set(ACQUISITION_REQUEST_TYPES)
        if pages >= MAX_PAGES_PER_RUN:
            allowed -= set(_ENUMERATION_REQUEST_TYPES)
        if details >= MAX_DETAIL_REQUESTS_PER_RUN:
            allowed -= set(_DETAIL_REQUEST_TYPES)
        if not allowed:
            # RUN-09: the host stops itself rather than asking the provider for
            # more.  Nothing is terminalized from a stopped enumeration.
            break
        claim = claim_next_request(
            conn, worker_id, now=db_utc_now(conn), types=frozenset(allowed),
            run_source_plan_id=plan_id,
        )
        if claim is None:
            break
        ts = db_utc_now(conn)
        task_kind = task_kind_for_request_type(claim.request_type)
        target_reference = _claim_target_reference(claim)
        is_enumeration = claim.request_type in _ENUMERATION_REQUEST_TYPES
        if is_enumeration or not listing_identity_sufficient:
            # 03 §40: coverage links the requests/pages that contributed to
            # this generation's enumeration proof.
            conn.execute(
                "INSERT INTO coverage_contributing_request (coverage_id, request_id)"
                " VALUES (?, ?) ON CONFLICT DO NOTHING",
                (coverage_id, claim.request_id),
            )
            conn.commit()

        cursor = (
            _load_cursor(conn, plan_row)
            if task_kind is AdapterTaskKind.ENUMERATE
            else None
        )
        task = AdapterTask(kind=task_kind, payload=dict(claim.payload or {}))
        # 02 ACQ-09: planning receives the host-resolved pins, never mutable
        # host state.  Snapshots that do not exist durably yet stay None —
        # the driver does not fabricate references (Slice 3 frontier/budgets).
        planning_ctx = PlanningContext(
            run_id=run_id,
            run_source_plan_id=plan_id,
            source_snapshot_ref=f"source://{plan_row['source_id']}",
            binding_revision_id=plan_row["binding_revision_id"],
            permission_profile_revision=plan_row["permission_profile_revision"],
            cursor_schema_version=plan_row["cursor_schema_version"],
        )
        plan_refusal: str | None = None
        try:
            request_plan = adapter.plan(task, cursor, ctx=planning_ctx)
        except (ValueError, TypeError) as exc:
            request_plan = None
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

            if _commit_fenced(conn, run_id, claim, ts=ts, mutate=mutate) != "COMMITTED":
                if run_is_cancelled(conn, run_id):
                    cancelled = True
                break
            if is_enumeration:
                pages += 1
            else:
                details += 1
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
            policy_snapshot_ref=None,
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
            ),
        )

        result = execute_request(envelope, policy)  # NO transaction held
        classification = classify_page(
            result, expect="JOB" if task_kind is AdapterTaskKind.DETAIL else "LIST"
        )

        def mutate(cursor_conn):
            fetch_attempt_id = _persist_fetch_attempt(cursor_conn, envelope, result, ts)
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
            if classification.state in NORMAL_PARSE_CLASSES:
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
                    # durable on the request as well as on the parse attempt:
                    # an operator scanning scrape_requests must see *why* a
                    # completed request produced nothing, without a join.
                    cursor_conn.execute(
                        "UPDATE scrape_requests SET last_failure_kind = ?,"
                        " last_failure_json = ? WHERE id = ?",
                        (
                            outcome_obj.failure.kind.value,
                            bounded_json(outcome_obj.failure.as_dict()),
                            claim.request_id,
                        ),
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
                if outcome_obj.kind.value == "SUCCESS_EMPTY":
                    signal["value"] = "EMPTY" if is_enumeration else "JOBS"
                    return
                _dispatch_child_tasks(
                    cursor_conn,
                    outcome_obj=outcome_obj,
                    task_kind=task_kind,
                    claim=claim,
                    plan_row=plan_row,
                    run_id=run_id,
                    ts=ts,
                )
                next_cursor = (
                    adapter.next_cursor(task, outcome_obj, cursor, ctx=parse_ctx)
                    if task_kind is AdapterTaskKind.ENUMERATE
                    else None
                )
                if next_cursor is not None:
                    _save_cursor(cursor_conn, plan_row, next_cursor, ts)
                    enqueue_request(
                        cursor_conn,
                        run_id=run_id,
                        run_source_plan_id=plan_id,
                        source_id=plan_row["source_id"],
                        binding_id=plan_row["binding_id"],
                        request_type="LIST_FETCH",
                        target_identity=request_plan.url,
                        logical_key=next_cursor.state_json,
                        commit=False,
                    )
                    signal["value"] = "CONTINUE"
                    return
                if (
                    outcome_obj.kind is ParseOutcomeKind.PARTIAL
                    or outcome_obj.continuation_required
                ):
                    # Degraded but recognized: the adapter says more exists and
                    # the host could not plan it now.  Never absence authority.
                    signal["value"] = "PARTIAL"
                    return
                if outcome_obj.kind is ParseOutcomeKind.FAILURE:
                    # A failed parse proves nothing about membership: no
                    # terminal enumeration may be derived from it (RUN-13).
                    signal["value"] = "FAILURE"
                    return
                # Complete page with no further cursor proposed: for an
                # enumeration task that is terminal membership proof.
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

        if _commit_fenced(conn, run_id, claim, ts=ts, mutate=mutate) != "COMMITTED":
            if run_is_cancelled(conn, run_id):
                cancelled = True
            break

        if is_enumeration:
            pages += 1
        else:
            details += 1
        value = signal.get("value")
        if value in ("INVALID", "FAILURE", "PARTIAL", "REFUSED"):
            run_degraded = True
            if is_enumeration or not listing_identity_sufficient:
                coverage_degraded = True
        if is_enumeration and value in ("EMPTY", "TERMINAL"):
            # 03 §40: only a recognized terminal enumeration — an accepted
            # empty result or a complete listing with no further cursor —
            # proves membership.  Budgets, refusals and parse failures never
            # do.
            terminal = True

    open_child_work = _open_acquisition_requests(conn, plan_id)
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
        coverage_budget_exhausted = (
            pages >= MAX_PAGES_PER_RUN
            or (
                not listing_identity_sufficient
                and details >= MAX_DETAIL_REQUESTS_PER_RUN
            )
        )
        if terminal and not coverage_degraded and coverage_barrier_open == 0:
            completion_state = "COMPLETE"
            stop_reason = "terminal cursor"
        elif cancelled:
            completion_state = "PARTIAL"
            stop_reason = "cancelled"
        elif coverage_budget_exhausted and coverage_barrier_open:
            completion_state = "BUDGET_EXHAUSTED"
            stop_reason = "host coverage budget exhausted with open contributing work"
        elif coverage_barrier_open:
            completion_state = "PARTIAL"
            stop_reason = "open contributing work remains"
        else:
            completion_state = "PARTIAL"
            stop_reason = "driver stop"
        try:
            finalize_coverage(
                conn,
                coverage_id,
                completion_state=completion_state,
                stop_reason=stop_reason,
                terminal_enumeration_proven=terminal,
                pages_completed=pages,
                now=db_utc_now(conn),
            )
        except Exception:
            # Coverage finalization must never block the run outcome.
            finalize_coverage(
                conn,
                coverage_id,
                completion_state="PARTIAL",
                stop_reason="driver stop",
                terminal_enumeration_proven=False,
                pages_completed=pages,
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


def _persist_fetch_attempt(conn, envelope, result, ts) -> str:
    """Durable fetch attempt for one ResultEnvelope (02 §11.3, 03 §30)."""
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
            structured_payload_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 ?, ?, ?, ?, ?, ?)
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
