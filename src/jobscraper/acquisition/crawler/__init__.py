"""Bounded generic HTTP crawler policy/state helpers (Slice 3 S3.5).

The service-owned runtime remains the only claim/dispatch/fence owner.  This
package is deliberately network-inert: it normalizes crawl identity, evaluates
scope/budgets, persists compatible cursor state, detects bounded pagination
traps, converts typed child proposals to durable requests, and plans/evaluates
robots work through the ordinary host HTTP execution seam.
"""

from .budget import BudgetDecision, CrawlBudget, CrawlUsage, budget_from_plan, check_budget, close_unstarted_over_budget, load_usage
from .canonicalize import CrawlUrl, crawl_identity, crawl_url
from .cursor import CursorCompatibilityError, LoadedCursor, load_cursor, save_cursor
from .frontier import FrontierDecision, enqueue_discovered_task
from .pagination import (
    PaginationDecision,
    PaginationGuardState,
    PaginationSignature,
    PaginationStopKind,
    advance_guard,
)
from .robots import (
    RobotsDecision,
    RobotsDecisionKind,
    RobotsGate,
    RobotsPolicy,
    RobotsPolicyStatus,
    build_robots_request_plan,
    ensure_robots_request,
    evaluate_robots_policy,
    evaluate_robots_result,
    load_robots_gate,
    parse_robots_result,
)
from .scope import CrawlScope, ScopeDecision, check_scope, scope_from_plan

__all__ = [
    "BudgetDecision", "CrawlBudget", "CrawlUsage", "budget_from_plan", "check_budget", "close_unstarted_over_budget", "load_usage",
    "CrawlUrl", "crawl_identity", "crawl_url",
    "CursorCompatibilityError", "LoadedCursor", "load_cursor", "save_cursor",
    "FrontierDecision", "enqueue_discovered_task",
    "PaginationDecision", "PaginationGuardState", "PaginationSignature", "PaginationStopKind", "advance_guard",
    "RobotsDecision", "RobotsDecisionKind", "RobotsGate", "RobotsPolicy", "RobotsPolicyStatus",
    "build_robots_request_plan", "ensure_robots_request", "evaluate_robots_policy", "evaluate_robots_result",
    "load_robots_gate", "parse_robots_result",
    "CrawlScope", "ScopeDecision", "check_scope", "scope_from_plan",
]
