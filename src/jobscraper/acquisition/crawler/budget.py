"""Durable plan-level crawler budgets (02 §20, 03 §15/RUN-09)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Mapping

DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_REQUESTS = 200
DEFAULT_MAX_BYTES = 100_000_000
DEFAULT_MAX_RUNTIME_S = 600.0
DEFAULT_MAX_DETAIL_REQUESTS = 200
DEFAULT_MAX_DEPTH = 5

HARD_MAX_PAGES = 50
HARD_MAX_REQUESTS = 200
HARD_MAX_BYTES = 100_000_000
HARD_MAX_RUNTIME_S = 600.0
HARD_MAX_DETAIL_REQUESTS = 200
HARD_MAX_DEPTH = 10

_ENUMERATION_TYPES = frozenset({"LIST_FETCH", "SOURCE_CRAWL"})
_DETAIL_TYPE = "DETAIL_FETCH"
_ACQUISITION_TYPES = frozenset(
    {"SOURCE_HEALTH_CHECK", "SOURCE_DISCOVERY", "LIST_FETCH", "DETAIL_FETCH", "SOURCE_CRAWL", "ADAPTER_SMOKE"}
)


@dataclass(frozen=True)
class CrawlBudget:
    max_pages: int
    max_requests: int
    max_bytes: int
    max_runtime_s: float
    max_detail_requests: int
    max_depth: int
    max_http_requests: int = DEFAULT_MAX_REQUESTS


@dataclass(frozen=True)
class CrawlUsage:
    pages_completed: int
    requests_created: int
    detail_requests_created: int
    bytes_downloaded: int
    max_depth_seen: int
    elapsed_s: float
    http_requests_created: int = 0


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str


def _row_get(row: Mapping[str, object], key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _positive_int(policy: dict, names: tuple[str, ...], default: int, hard: int) -> int:
    value = next((policy[name] for name in names if name in policy), default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{names[0]} must be a positive integer")
    return min(value, hard)


def _positive_float(policy: dict, names: tuple[str, ...], default: float, hard: float) -> float:
    value = next((policy[name] for name in names if name in policy), default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValueError(f"{names[0]} must be positive")
    return min(float(value), hard)


def budget_from_plan(plan_row: Mapping[str, object], *, stop_policy=None) -> CrawlBudget:
    raw = _row_get(plan_row, "crawl_policy_snapshot_json", "{}") or "{}"
    try:
        policy = json.loads(str(raw)) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid crawl_policy_snapshot_json") from exc
    if not isinstance(policy, dict):
        raise ValueError("crawl policy snapshot must be an object")
    total_requests = _positive_int(
        policy, ("max_requests", "max_request_count"), DEFAULT_MAX_REQUESTS, HARD_MAX_REQUESTS
    )
    class_budgets = policy.get("execution_class_budgets", {})
    if class_budgets is None:
        class_budgets = {}
    if not isinstance(class_budgets, dict):
        raise ValueError("execution_class_budgets must be an object")
    http_policy = class_budgets.get("HTTP", class_budgets.get("http", {}))
    if http_policy is None:
        http_policy = {}
    if not isinstance(http_policy, dict):
        raise ValueError("HTTP execution-class budget must be an object")
    http_max = http_policy.get("max_requests", policy.get("max_http_requests", total_requests))
    if isinstance(http_max, bool) or not isinstance(http_max, int) or http_max <= 0:
        raise ValueError("HTTP max_requests must be a positive integer")
    budget = CrawlBudget(
        max_pages=_positive_int(policy, ("max_pages",), DEFAULT_MAX_PAGES, HARD_MAX_PAGES),
        max_requests=total_requests,
        max_bytes=_positive_int(policy, ("max_bytes",), DEFAULT_MAX_BYTES, HARD_MAX_BYTES),
        max_runtime_s=_positive_float(policy, ("max_runtime_s", "max_runtime_seconds", "max_runtime"), DEFAULT_MAX_RUNTIME_S, HARD_MAX_RUNTIME_S),
        max_detail_requests=_positive_int(policy, ("max_detail_requests", "max_detail_fetches"), DEFAULT_MAX_DETAIL_REQUESTS, HARD_MAX_DETAIL_REQUESTS),
        max_depth=_positive_int(policy, ("max_depth",), DEFAULT_MAX_DEPTH, HARD_MAX_DEPTH),
        max_http_requests=min(total_requests, http_max, HARD_MAX_REQUESTS),
    )
    if stop_policy is None:
        return budget
    # The adapter's declared stop policy can only tighten host/run policy.
    # Detail/depth/byte limits remain host-owned because StopPolicy does not
    # carry those dimensions in the frozen v3 contract.
    return CrawlBudget(
        max_pages=min(budget.max_pages, int(stop_policy.max_pages)),
        max_requests=min(budget.max_requests, int(stop_policy.max_requests)),
        max_bytes=budget.max_bytes,
        max_runtime_s=min(budget.max_runtime_s, float(stop_policy.max_runtime_s)),
        max_detail_requests=budget.max_detail_requests,
        max_depth=budget.max_depth,
        max_http_requests=min(budget.max_http_requests, int(stop_policy.max_requests)),
    )


def load_usage(conn: sqlite3.Connection, run_source_plan_id: str, *, now: str) -> CrawlUsage:
    acq = ",".join("?" for _ in sorted(_ACQUISITION_TYPES))
    enum = ",".join("?" for _ in sorted(_ENUMERATION_TYPES))
    row = conn.execute(
        f"""
        SELECT
          (SELECT COUNT(DISTINCT r.id)
             FROM scrape_requests r
             JOIN fetch_attempts f ON f.request_id = r.id
            WHERE r.run_source_plan_id = ? AND r.request_type IN ({enum})
              AND NOT (r.request_type = 'SOURCE_CRAWL'
                       AND r.payload_json LIKE '%\"role\":\"ROBOTS\"%')) AS pages_completed,
          (SELECT COUNT(*) FROM scrape_requests r
            WHERE r.run_source_plan_id = ? AND r.request_type IN ({acq})) AS requests_created,
          (SELECT COUNT(*) FROM scrape_requests r
            WHERE r.run_source_plan_id = ? AND r.request_type = ?) AS detail_requests_created,
          (SELECT COUNT(*) FROM scrape_requests r
            WHERE r.run_source_plan_id = ? AND r.execution_class = 'HTTP') AS http_requests_created,
          COALESCE((SELECT SUM(COALESCE(f.bytes_downloaded, 0))
             FROM fetch_attempts f JOIN scrape_requests r ON r.id = f.request_id
            WHERE r.run_source_plan_id = ?), 0) AS bytes_downloaded,
          COALESCE((SELECT MAX(r.depth) FROM scrape_requests r
            WHERE r.run_source_plan_id = ?), 0) AS max_depth_seen,
          COALESCE((SELECT MAX(0.0,
             (julianday(?) - julianday(COALESCE(run.started_at, rsp.created_at))) * 86400.0)
             FROM run_source_plans rsp JOIN scrape_runs run ON run.id = rsp.run_id
            WHERE rsp.id = ?), 0.0) AS elapsed_s
        """,
        (
            run_source_plan_id, *sorted(_ENUMERATION_TYPES),
            run_source_plan_id, *sorted(_ACQUISITION_TYPES),
            run_source_plan_id, _DETAIL_TYPE,
            run_source_plan_id,
            run_source_plan_id,
            run_source_plan_id,
            now, run_source_plan_id,
        ),
    ).fetchone()
    if row is None:
        return CrawlUsage(0, 0, 0, 0, 0, 0.0)
    return CrawlUsage(
        int(row["pages_completed"] or 0),
        int(row["requests_created"] or 0),
        int(row["detail_requests_created"] or 0),
        int(row["bytes_downloaded"] or 0),
        int(row["max_depth_seen"] or 0),
        float(row["elapsed_s"] or 0.0),
        int(row["http_requests_created"] or 0),
    )



def close_unstarted_over_budget(
    conn: sqlite3.Connection,
    run_source_plan_id: str,
    *,
    request_types: frozenset[str],
    reason: str,
    now: str,
) -> int:
    """Terminalize only unstarted work that a durable budget can no longer run.

    RUNNING work retains its lease/fence and is deliberately untouched. This
    prevents a permanent open-run tail after bytes/runtime/type budgets become
    exhausted while previously accepted child work is still PENDING.
    """
    if not request_types:
        return 0
    placeholders = ",".join("?" for _ in sorted(request_types))
    detail = json.dumps(
        {"kind": "BUDGET_EXHAUSTED", "reason": str(reason)},
        sort_keys=True, separators=(",", ":"),
    )
    # Join the caller's transaction when one is already open (the owning
    # request fence or the driver loop owns atomicity there).  Only the
    # standalone path takes its own BEGIN IMMEDIATE ... COMMIT so budget
    # terminalization stays durable even when no fence is active.  An
    # unconditional BEGIN would raise inside an open fence and, worse, an
    # unconditional ROLLBACK would discard the caller's fenced work.
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE scrape_requests"
            " SET status = 'FAILED', finished_at = COALESCE(finished_at, ?),"
            " updated_at = ?, last_failure_kind = 'BUDGET_EXHAUSTED',"
            " last_failure_json = ?"
            f" WHERE run_source_plan_id = ? AND request_type IN ({placeholders})"
            " AND status IN ('PENDING','RETRY_WAIT')",
            (now, now, detail, run_source_plan_id, *sorted(request_types)),
        )
        if owns_transaction:
            conn.execute("COMMIT")
        return int(cur.rowcount)
    except BaseException:
        if owns_transaction and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

def check_budget(
    budget: CrawlBudget,
    usage: CrawlUsage,
    *,
    proposed_request_type: str | None = None,
    proposed_depth: int | None = None,
    proposed_execution_class: str | None = None,
) -> BudgetDecision:
    if usage.elapsed_s >= budget.max_runtime_s:
        return BudgetDecision(False, "MAX_RUNTIME")
    if usage.bytes_downloaded >= budget.max_bytes:
        return BudgetDecision(False, "MAX_BYTES")
    if usage.requests_created >= budget.max_requests:
        return BudgetDecision(False, "MAX_REQUESTS")
    if (
        (proposed_execution_class or "").upper() == "HTTP"
        and usage.http_requests_created >= budget.max_http_requests
    ):
        return BudgetDecision(False, "MAX_HTTP_REQUESTS")
    if proposed_depth is not None and proposed_depth > budget.max_depth:
        return BudgetDecision(False, "MAX_DEPTH")
    if proposed_request_type in _ENUMERATION_TYPES and usage.pages_completed >= budget.max_pages:
        return BudgetDecision(False, "MAX_PAGES")
    if proposed_request_type == _DETAIL_TYPE and usage.detail_requests_created >= budget.max_detail_requests:
        return BudgetDecision(False, "MAX_DETAIL_REQUESTS")
    return BudgetDecision(True, "ALLOWED")


__all__ = [
    "BudgetDecision", "CrawlBudget", "CrawlUsage", "budget_from_plan", "check_budget", "close_unstarted_over_budget", "load_usage",
]
