"""Durable source-entry discovery seam (S2.8 corrective).

Authority: v0.3.1.3 acquisition §12.1 and durable runtime §16/§30.

The first careers-page probe is ordinary durable acquisition work: a Source,
immutable generic-discovery binding revision, RunSourcePlan, SOURCE_DISCOVERY
request and claimed attempt all exist before the executor is called.  The
network result, fingerprint and route decision are committed under the same
ownership fence.  This module deliberately does not open enumeration coverage
or implement ROAD-04 crawl/frontier breadth.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

from jobscraper.acquisition.envelope import ExecutionPlanEnvelope, RequestPlan
from jobscraper.acquisition.failures import FailureKind
from jobscraper.acquisition.httpexec import execute_request
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import AdapterTask, AdapterTaskKind, PlanningContext
from jobscraper.adapters.fingerprint import AtsFingerprint, classify_content
from jobscraper.adapters.registry import build_adapter
from jobscraper.adapters.router import RouteDecision, plan_routes
from jobscraper.ids import new_id
from jobscraper.pipeline.driver import _persist_fetch_attempt, _record_evidence, source_policy
from jobscraper.pipeline.evidence import bounded_json
from jobscraper.runtime.claims import claim_next_request
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

GENERIC_DISCOVERY_ADAPTER_ID = "generic_discovery"
GENERIC_DISCOVERY_STRATEGY = "GENERIC_DISCOVERY"


@dataclass(frozen=True)
class DiscoveryResult:
    source_id: str
    binding_id: str
    binding_revision_id: str
    run_id: str
    run_source_plan_id: str
    request_id: str
    attempt_id: str
    fingerprint_id: str | None
    route_decision_id: str | None
    fingerprint: AtsFingerprint | None
    decision: RouteDecision | None
    result: ResultEnvelope


def _revision_row(conn: sqlite3.Connection, revision_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT r.*, d.adapter_api_version
        FROM source_adapter_binding_revisions r
        JOIN adapter_definitions d
          ON d.adapter_id = r.adapter_id
         AND d.adapter_version = r.adapter_version
        WHERE r.id = ?
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"binding revision disappeared: {revision_id}")
    return row


def discover_source(
    conn: sqlite3.Connection,
    *,
    display_name: str,
    source_family: str,
    entry_url: str,
    worker_id: str = "service",
    now: str | None = None,
    timeout_s: float = 10.0,
    max_bytes: int = 1_000_000,
    executor: Callable[[ExecutionPlanEnvelope, object], ResultEnvelope] = execute_request,
) -> DiscoveryResult:
    """Acquire one operator-recorded careers URL with durable identity first.

    This is the typed DiscoveryPlan seam permitted by acquisition §12.1.  It
    intentionally stops after evidence-first fingerprinting/routing; generic
    crawl breadth remains a later-slice capability.
    """
    ts = now or db_utc_now(conn)

    definition = ensure_builtin_adapter_definition(
        conn, GENERIC_DISCOVERY_ADAPTER_ID, now=ts
    )
    provisioned = provision_source_and_binding(
        conn,
        display_name=display_name,
        source_family=source_family,
        entry_url=entry_url,
        canonical_host=None,
        adapter_id=GENERIC_DISCOVERY_ADAPTER_ID,
        adapter_version=definition.adapter_version,
        strategy=GENERIC_DISCOVERY_STRATEGY,
        execution_class="HTTP",
        config={
            "entry_url": entry_url,
            "timeout_s": timeout_s,
            "max_bytes": max_bytes,
        },
        now=ts,
    )
    revision = _revision_row(conn, provisioned.binding_revision_id)

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
        "cursor_schema_version": 1,
        "permission_profile_id": revision["permission_profile_id"],
        "permission_profile_revision": revision["permission_profile_revision"],
    }
    run_id, plan_ids = create_run(conn, profile_id=None, plans=[plan], now=ts)
    run_source_plan_id = plan_ids[0]
    request_id, _created = enqueue_request(
        conn,
        run_id=run_id,
        run_source_plan_id=run_source_plan_id,
        source_id=provisioned.source_id,
        binding_id=provisioned.binding_id,
        request_type="SOURCE_DISCOVERY",
        target_identity=entry_url,
        payload={"url": entry_url},
        strategy=GENERIC_DISCOVERY_STRATEGY,
        execution_class="HTTP",
        logical_key="source-entry-probe-v1",
        now=ts,
    )
    mark_run_started(conn, run_id, now=ts)

    # claim_next_request creates request_attempts and marks the request RUNNING
    # before returning.  No network-capable function has run before this point.
    claim = claim_next_request(
        conn,
        worker_id,
        now=ts,
        types=frozenset({"SOURCE_DISCOVERY"}),
        run_source_plan_id=run_source_plan_id,
    )
    if claim is None or claim.request_id != request_id:
        raise RuntimeError("durable SOURCE_DISCOVERY request could not be claimed")

    adapter = build_adapter(
        revision["adapter_id"], json.loads(revision["config_json"] or "{}")
    )
    task = AdapterTask(kind=AdapterTaskKind.DISCOVER, payload=dict(claim.payload or {}))
    planning_ctx = PlanningContext(
        run_id=run_id,
        run_source_plan_id=run_source_plan_id,
        source_snapshot_ref=f"source://{provisioned.source_id}",
        binding_revision_id=provisioned.binding_revision_id,
        permission_profile_revision=revision["permission_profile_revision"],
        cursor_schema_version=1,
    )
    request_plan = adapter.plan(task, None, ctx=planning_ctx)
    envelope = ExecutionPlanEnvelope(
        plan_id=new_id("plan"),
        request_id=request_id,
        attempt_id=claim.attempt_id,
        run_id=run_id,
        run_source_plan_id=run_source_plan_id,
        source_id=provisioned.source_id,
        binding_id=provisioned.binding_id,
        binding_revision_id=provisioned.binding_revision_id,
        adapter_id=revision["adapter_id"],
        adapter_version=revision["adapter_version"],
        strategy=revision["strategy"],
        execution_class=revision["execution_class"],
        policy_snapshot_ref=None,
        permission_profile_id=revision["permission_profile_id"],
        permission_profile_revision=revision["permission_profile_revision"],
        payload_kind="REQUEST",
        payload=RequestPlan(
            method=request_plan.method,
            url=request_plan.url,
            headers=dict(request_plan.headers),
            expected_content_types=tuple(request_plan.expected_content_types),
            timeout_s=request_plan.timeout_s,
            max_bytes=request_plan.max_bytes,
            purpose="SOURCE_DISCOVERY",
        ),
    )

    source = conn.execute(
        "SELECT * FROM sources WHERE id = ?", (provisioned.source_id,)
    ).fetchone()
    policy = source_policy(source, adapter_id=GENERIC_DISCOVERY_ADAPTER_ID)

    # First I/O boundary.  Everything referenced by the envelope is durable.
    result = executor(envelope, policy).finalize()
    usable = (
        result.failure is None
        and result.status_code is not None
        and 200 <= result.status_code < 300
        and bool(result.body)
    )
    fingerprint: AtsFingerprint | None = None
    decision: RouteDecision | None = None
    if usable:
        fingerprint = classify_content(
            url=entry_url,
            body=result.body,
            content_type=result.content_type or "",
        )
        decision = plan_routes(
            fingerprint=fingerprint,
            supported_execution_classes=frozenset({"HTTP"}),
        )

    ids: dict[str, str | None] = {"fingerprint": None, "decision": None}

    def mutate(cursor_conn: sqlite3.Connection) -> None:
        fetch_attempt_id = _persist_fetch_attempt(cursor_conn, envelope, result, ts)
        _record_evidence(
            cursor_conn,
            request_id=request_id,
            attempt_id=claim.attempt_id,
            fetch_attempt_id=fetch_attempt_id,
            kind="RESULT_ENVELOPE",
            ref=result.body_ref,
            detail=result.as_evidence(),
            content_hash=result.normalized_content_hash,
            now=ts,
        )
        if result.security_policy_result != "ALLOWED":
            _record_evidence(
                cursor_conn,
                request_id=request_id,
                attempt_id=claim.attempt_id,
                fetch_attempt_id=fetch_attempt_id,
                kind="SECURITY_POLICY",
                ref=result.security_policy_result,
                detail={
                    "result": result.security_policy_result,
                    "requested_url": result.requested_url,
                },
                content_hash=None,
                now=ts,
            )
        if result.failure is not None:
            cursor_conn.execute(
                "UPDATE scrape_requests SET last_failure_kind = ?, last_failure_json = ?"
                " WHERE id = ?",
                (
                    result.failure.kind.value,
                    bounded_json(result.failure.as_dict()),
                    request_id,
                ),
            )
        if fingerprint is not None and decision is not None:
            ids["fingerprint"] = record_fingerprint(
                cursor_conn,
                source_id=provisioned.source_id,
                url=entry_url,
                fingerprint=fingerprint,
                now=ts,
                commit=False,
            )
            ids["decision"] = record_route_decision(
                cursor_conn,
                source_id=provisioned.source_id,
                fingerprint=fingerprint,
                decision=decision,
                now=ts,
                commit=False,
            )

    with fenced_commit(
        conn,
        request_id,
        claim.attempt_id,
        now=ts,
        mutate=mutate,
    ):
        pass

    if usable:
        group_outcome = "SATISFIED"
    elif result.failure is not None and result.failure.kind is FailureKind.POLICY_REJECTED:
        group_outcome = "POLICY_DENIED"
    else:
        group_outcome = "FAILED"
    set_group_outcome(conn, run_source_plan_id, group_outcome, now=ts)
    aggregate_run(conn, run_id, now=ts)

    return DiscoveryResult(
        source_id=provisioned.source_id,
        binding_id=provisioned.binding_id,
        binding_revision_id=provisioned.binding_revision_id,
        run_id=run_id,
        run_source_plan_id=run_source_plan_id,
        request_id=request_id,
        attempt_id=claim.attempt_id,
        fingerprint_id=ids["fingerprint"],
        route_decision_id=ids["decision"],
        fingerprint=fingerprint,
        decision=decision,
        result=result,
    )


__all__ = [
    "DiscoveryResult",
    "GENERIC_DISCOVERY_ADAPTER_ID",
    "GENERIC_DISCOVERY_STRATEGY",
    "discover_source",
]
