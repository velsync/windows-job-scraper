"""Fail-closed generic HTTP crawl scope (02 §20, 04 §5.1)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from .canonicalize import crawl_url

DEFAULT_MAX_DEPTH = 5
HARD_MAX_DEPTH = 10


@dataclass(frozen=True)
class CrawlScope:
    allowed_hosts: frozenset[str]
    allowed_path_prefixes: tuple[str, ...]
    deny_path_prefixes: tuple[str, ...]
    max_depth: int


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    reason: str
    normalized_url: str | None = None


def _row_get(row: Mapping[str, object], key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _str_tuple(value, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError("crawl path/host policy must be a list of strings")
    return tuple(v.strip() for v in value if v.strip())


def _canonical_host(value: str) -> str:
    # Build a harmless URL only to reuse the crawl normalizer's host rules.
    if "://" in value:
        return crawl_url(value).host
    return crawl_url(f"https://{value}").host


def scope_from_plan(
    plan_row: Mapping[str, object],
    source_row: Mapping[str, object],
    *,
    destination_allowed_hosts: Iterable[str] | None = None,
) -> CrawlScope:
    """Resolve immutable crawl scope without widening host destination policy.

    ``destination_allowed_hosts`` should be the already host-owned
    ``DestinationPolicy.allowed_hosts``.  A snapshot may only narrow that set.
    When not supplied, the Source entry host is the fail-closed authority.
    """
    source_entry = _row_get(source_row, "entry_url")
    if not isinstance(source_entry, str) or not source_entry.strip():
        raise ValueError("source entry_url is required for crawl scope")
    source_host = crawl_url(source_entry).host

    if destination_allowed_hosts is None:
        host_authority = frozenset({source_host})
    else:
        host_authority = frozenset(
            _canonical_host(str(host)) for host in destination_allowed_hosts if str(host).strip()
        )
    if not host_authority:
        raise ValueError("crawl destination host authority is empty")

    raw = _row_get(plan_row, "crawl_policy_snapshot_json", "{}") or "{}"
    try:
        policy = json.loads(str(raw)) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid crawl_policy_snapshot_json") from exc
    if not isinstance(policy, dict):
        raise ValueError("crawl policy snapshot must be an object")

    requested_hosts = _str_tuple(policy.get("allowed_hosts"), default=())
    if requested_hosts:
        normalized_requested = frozenset(_canonical_host(v) for v in requested_hosts)
        allowed_hosts = host_authority.intersection(normalized_requested)
    else:
        allowed_hosts = host_authority
    if not allowed_hosts:
        raise ValueError("crawl policy leaves no host authorized")

    allow_paths = _str_tuple(
        policy.get("allowed_path_prefixes", policy.get("allow_path_prefixes")),
        default=("/",),
    ) or ("/",)
    deny_paths = _str_tuple(
        policy.get("deny_path_prefixes", policy.get("denied_path_prefixes")),
        default=(),
    )
    if any(not p.startswith("/") for p in (*allow_paths, *deny_paths)):
        raise ValueError("crawl path prefixes must start with '/'")

    raw_depth = policy.get("max_depth", DEFAULT_MAX_DEPTH)
    if isinstance(raw_depth, bool) or not isinstance(raw_depth, int) or raw_depth <= 0:
        raise ValueError("max_depth must be a positive integer")
    max_depth = min(raw_depth, HARD_MAX_DEPTH)
    return CrawlScope(allowed_hosts, allow_paths, deny_paths, max_depth)


def check_scope(
    scope: CrawlScope,
    target: str,
    *,
    base: str | None = None,
    depth: int,
) -> ScopeDecision:
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        return ScopeDecision(False, "INVALID_DEPTH")
    if depth > scope.max_depth:
        return ScopeDecision(False, "MAX_DEPTH")
    try:
        target_url = crawl_url(target, base=base)
    except (TypeError, ValueError):
        return ScopeDecision(False, "INVALID_URL")
    if target_url.host not in scope.allowed_hosts:
        return ScopeDecision(False, "HOST_DENIED", target_url.normalized)
    if any(target_url.path.startswith(prefix) for prefix in scope.deny_path_prefixes):
        return ScopeDecision(False, "PATH_DENIED", target_url.normalized)
    if not any(target_url.path.startswith(prefix) for prefix in scope.allowed_path_prefixes):
        return ScopeDecision(False, "PATH_OUT_OF_SCOPE", target_url.normalized)
    return ScopeDecision(True, "ALLOWED", target_url.normalized)


__all__ = [
    "DEFAULT_MAX_DEPTH", "HARD_MAX_DEPTH", "CrawlScope", "ScopeDecision",
    "check_scope", "scope_from_plan",
]
