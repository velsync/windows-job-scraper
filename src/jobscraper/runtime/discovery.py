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
* atomically fence fetch/result/page-validity evidence together with the pure
  fingerprint + route decision when the page is valid.

This is source classification only. It deliberately creates no enumeration
coverage, parse attempt, job observation or generic HTML crawl.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from jobscraper.acquisition.envelope import ExecutionPlanEnvelope, RequestPlan
from jobscraper.acquisition.httpexec import execute_request
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
    _update_run_counters,
    source_policy,
)
from jobscraper.runtime.claims import StaleOwnership, claim_next_request
from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.provisioning import (
    ensure_builtin_adapter_definition,
    provision_source_and_binding,
    record_fingerprint,
    record_route_decision,
)
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import (
    aggregate_run,
    create_run,
    mark_run_started,
    set_group_outcome,
)

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
    already committed. Invalid URLs/configuration fail during provisioning;
    this function never calls the network executor.
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
    run_id, plan_ids = create_run(conn, profile_id=None, plans=[plan], now=now)
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
    )
    if not created:
        raise DiscoveryError("new discovery run unexpectedly reused a request")
    return QueuedDiscovery(
        run_id=run_id,
        run_source_plan_id=plan_id,
        request_id=request_id,
        source_id=provisioned.source_id,
        binding_id=provisioned.binding_id,
        binding_revision_id=provisioned.binding_revision_id,
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
        policy_snapshot_ref=None,
        permission_profile_id=plan_row["permission_profile_id"],
        permission_profile_revision=plan_row["permission_profile_revision"],
        payload_kind="REQUEST",
        payload=request_plan,
    )
    return plan_row, source, request_plan, envelope


def execute_source_discovery(
    conn: sqlite3.Connection,
    queued: QueuedDiscovery,
    *,
    worker_id: str = "source-discovery",
    now: str | None = None,
) -> DiscoveryOutcome:
    """Claim and execute one already-durable first probe.

    Network I/O occurs only after the request claim exists. All outputs that
    assert what the probe saw — fetch attempt, result/page evidence, fingerprint
    and route decision — commit under the same ownership fence. If ownership
    is stale, none of those request-owned outputs are committed.
    """
    claim_ts = now or db_utc_now(conn)
    mark_run_started(conn, queued.run_id, now=claim_ts)
    claim = claim_next_request(
        conn,
        worker_id,
        now=claim_ts,
        types=frozenset({"SOURCE_DISCOVERY"}),
        run_source_plan_id=queued.run_source_plan_id,
    )
    if claim is None or claim.request_id != queued.request_id:
        raise DiscoveryError("durable SOURCE_DISCOVERY request is not claimable")

    _plan_row, source, _request_plan, envelope = _plan_request(conn, queued, claim)
    result = execute_request(
        envelope, source_policy(source, adapter_id=_DISCOVERY_ADAPTER_ID)
    )
    classification = classify_page(result, expect="LIST")
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

    try:
        with fenced_commit(
            conn,
            claim.request_id,
            claim.attempt_id,
            now=commit_ts,
            mutate=mutate,
        ):
            pass
    except StaleOwnership as exc:
        raise DiscoveryError("discovery ownership was lost before evidence commit") from exc

    group_outcome = "SATISFIED" if fingerprint is not None else "SATISFIED_PARTIAL"
    set_group_outcome(conn, queued.run_source_plan_id, group_outcome, now=commit_ts)
    _update_run_counters(conn, queued.run_id)
    run_status = aggregate_run(conn, queued.run_id, now=commit_ts)
    if run_status is None:
        raise DiscoveryError("discovery run remained open after its only request completed")

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
    "queue_source_discovery",
]
