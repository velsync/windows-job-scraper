"""Typed discovered-task conversion into the existing durable frontier."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping

from jobscraper.adapters.contract import DiscoveredTask
from jobscraper.runtime.requests import enqueue_request
from .budget import CrawlBudget, CrawlUsage, check_budget
from .scope import CrawlScope, check_scope

_TASK_REQUEST_TYPE = {
    "DETAIL": "DETAIL_FETCH",
    "ENUMERATE": "LIST_FETCH",
    "CRAWL": "SOURCE_CRAWL",
}


@dataclass(frozen=True)
class FrontierDecision:
    request_id: str | None
    created: bool
    accepted: bool
    reason: str
    request_type: str | None = None
    normalized_target: str | None = None


def enqueue_discovered_task(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    plan_row: Mapping[str, object],
    parent_request_id: str,
    discovered: DiscoveredTask,
    scope: CrawlScope,
    budget: CrawlBudget,
    usage: CrawlUsage,
    base_url: str | None,
    now: str,
    role: str | None = None,
    payload_extra: Mapping[str, object] | None = None,
) -> FrontierDecision:
    request_type = _TASK_REQUEST_TYPE.get(str(discovered.kind).upper())
    if request_type is None:
        return FrontierDecision(None, False, False, "UNKNOWN_TASK_KIND")
    target_reference = str(discovered.target_reference)
    # ACQ-04 deliberately calls this a target *reference*: DETAIL tasks may
    # carry a provider-native opaque id (for example a Greenhouse job id).
    # The adapter later turns that id into a reviewed RequestPlan URL, which
    # the driver scope-checks before dispatch. CRAWL/ENUMERATE references are
    # frontier URLs and must be scoped before they become durable work.
    url_like_detail = (
        request_type == "DETAIL_FETCH"
        and ("://" in target_reference or (base_url is not None and target_reference.startswith(("/", "./", "../"))))
    )
    if request_type != "DETAIL_FETCH" or url_like_detail:
        scope_decision = check_scope(
            scope, target_reference, base=base_url, depth=int(discovered.depth)
        )
        if not scope_decision.allowed:
            return FrontierDecision(
                None, False, False, "SCOPE_DENIED", request_type, scope_decision.normalized_url
            )
        target_identity = scope_decision.normalized_url or target_reference
    else:
        if not target_reference.strip():
            return FrontierDecision(None, False, False, "EMPTY_TARGET_REFERENCE", request_type)
        target_identity = target_reference
    budget_decision = check_budget(
        budget,
        usage,
        proposed_request_type=request_type,
        proposed_depth=int(discovered.depth),
        proposed_execution_class=str(plan_row["execution_class"]),
    )
    if not budget_decision.allowed:
        return FrontierDecision(
            None, False, False, f"BUDGET_{budget_decision.reason}", request_type,
            target_identity,
        )
    if role is not None and request_type != "SOURCE_CRAWL":
        raise ValueError("frontier role is valid only for SOURCE_CRAWL work")
    payload = {
        "kind": str(discovered.kind).upper(),
        "target_reference": target_identity,
        "logical_key": discovered.logical_key,
        "depth": int(discovered.depth),
        "parent_reference": discovered.parent_reference,
        "role": (str(role).upper() if role is not None else "PAGE")
        if request_type == "SOURCE_CRAWL"
        else None,
    }
    if payload_extra:
        collisions = sorted(set(payload).intersection(payload_extra))
        if collisions:
            raise ValueError(f"frontier payload_extra may not replace reserved keys: {collisions}")
        payload.update(dict(payload_extra))

    request_id, created = enqueue_request(
        conn,
        run_id=run_id,
        run_source_plan_id=str(plan_row["id"]),
        source_id=str(plan_row["source_id"]),
        binding_id=str(plan_row["binding_id"]),
        request_type=request_type,
        target_identity=target_identity,
        payload=payload,
        strategy=str(plan_row["strategy"]),
        execution_class=str(plan_row["execution_class"]),
        priority=int(discovered.priority),
        depth=int(discovered.depth),
        parent_request_id=parent_request_id,
        logical_key=discovered.logical_key,
        now=now,
        commit=False,
    )
    return FrontierDecision(
        request_id, created, True, "ENQUEUED" if created else "DUPLICATE_SUPPRESSED",
        request_type, target_identity,
    )


__all__ = ["FrontierDecision", "enqueue_discovered_task"]
