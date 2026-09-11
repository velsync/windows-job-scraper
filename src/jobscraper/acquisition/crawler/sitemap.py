"""Bounded, network-inert sitemap discovery and frontier integration (S3.6).

Sitemaps are advisory discovery evidence.  They can prioritize ordinary durable
crawler work, but they never create I/O authority, prove content equality, or
grant absence authority.  Every derived URL is re-checked by the existing
scope/budget/frontier path before it becomes durable work.
"""
from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.adapters.contract import DiscoveredTask

from .budget import CrawlBudget, load_usage
from .canonicalize import crawl_identity, crawl_url
from .frontier import enqueue_discovered_task
from .scope import CrawlScope

SITEMAP_MAX_BYTES = 2_000_000
SITEMAP_MAX_ITEMS = 10_000
SITEMAP_MAX_INDEX_DEPTH = 3
SITEMAP_MAX_LOC_CHARS = 4096
SITEMAP_MAX_LASTMOD_CHARS = 128
SITEMAP_TIMEOUT_S = 20.0

_FORBIDDEN_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_CAREER_TOKENS = frozenset(
    {
        "career", "careers", "employment", "job", "jobs", "opening", "openings",
        "opportunities", "position", "positions", "vacancies", "vacancy",
    }
)


class SitemapDocumentKind(Enum):
    URLSET = "URLSET"
    INDEX = "INDEX"


@dataclass(frozen=True)
class SitemapLimits:
    max_bytes: int = SITEMAP_MAX_BYTES
    max_items: int = SITEMAP_MAX_ITEMS
    max_index_depth: int = SITEMAP_MAX_INDEX_DEPTH
    max_loc_chars: int = SITEMAP_MAX_LOC_CHARS

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_items", "max_index_depth", "max_loc_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_bytes == 0 or self.max_items == 0 or self.max_loc_chars == 0:
            raise ValueError("sitemap byte/item/location bounds must be positive")


@dataclass(frozen=True)
class SitemapCandidate:
    """One normalized sitemap-discovered reference.

    ``lastmod`` is retained only as advisory scheduling evidence.
    """

    url: str
    raw_url: str
    lastmod: str | None = None


@dataclass(frozen=True)
class SitemapDiagnostic:
    code: str
    reason: str
    url: str | None = None
    lastmod: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "reason": self.reason,
            "url": self.url,
            "lastmod": self.lastmod,
            "absence_authority": False,
            "content_equality_proven": False,
            "lastmod_is_advisory": True,
        }


@dataclass(frozen=True)
class SitemapDiscovery:
    candidates: tuple[SitemapCandidate, ...] = ()
    diagnostics: tuple[SitemapDiagnostic, ...] = ()


@dataclass(frozen=True)
class SitemapParseResult:
    kind: SitemapDocumentKind | None
    candidates: tuple[SitemapCandidate, ...] = ()
    diagnostics: tuple[SitemapDiagnostic, ...] = ()
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value if self.kind is not None else None,
            "candidate_count": len(self.candidates),
            "diagnostic_count": len(self.diagnostics),
            "truncated": self.truncated,
            "absence_authority": False,
            "lastmod_is_advisory": True,
        }


@dataclass(frozen=True)
class SitemapEnqueueResult:
    created: int = 0
    reused: int = 0
    diagnostics: tuple[SitemapDiagnostic, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "created": self.created,
            "reused": self.reused,
            "refused": len(self.diagnostics),
            "absence_authority": False,
            "lastmod_is_advisory": True,
        }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _direct_child_text(node: ET.Element, name: str) -> str | None:
    for child in list(node):
        if _local_name(str(child.tag)) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def _normalized_candidate(
    raw_url: str,
    *,
    lastmod: str | None,
    limits: SitemapLimits,
) -> tuple[SitemapCandidate | None, tuple[SitemapDiagnostic, ...]]:
    diagnostics: list[SitemapDiagnostic] = []
    raw = raw_url.strip()
    if not raw:
        return None, (
            SitemapDiagnostic("MISSING_LOC", "sitemap entry has no usable <loc>"),
        )
    if len(raw) > limits.max_loc_chars:
        return None, (
            SitemapDiagnostic(
                "LOC_TOO_LONG", "sitemap <loc> exceeds the configured bound",
                url=raw[:256],
            ),
        )
    try:
        normalized = crawl_url(raw).normalized
    except (TypeError, ValueError) as exc:
        return None, (
            SitemapDiagnostic("INVALID_LOC", f"invalid sitemap location: {exc}", url=raw[:256]),
        )

    cleaned_lastmod = None
    if lastmod is not None:
        candidate = lastmod.strip()
        if len(candidate) > SITEMAP_MAX_LASTMOD_CHARS:
            diagnostics.append(
                SitemapDiagnostic(
                    "INVALID_LASTMOD", "lastmod exceeds the bounded evidence length",
                    url=normalized, lastmod=candidate[:SITEMAP_MAX_LASTMOD_CHARS],
                )
            )
        elif candidate:
            cleaned_lastmod = candidate
            if _lastmod_timestamp(candidate) is None:
                diagnostics.append(
                    SitemapDiagnostic(
                        "INVALID_LASTMOD", "lastmod is not a recognized ISO date/time",
                        url=normalized, lastmod=candidate,
                    )
                )

    return SitemapCandidate(normalized, raw, cleaned_lastmod), tuple(diagnostics)


def _lastmod_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _career_like(url: str) -> bool:
    path = urlsplit(url).path.lower()
    segments = {segment for segment in re.split(r"[^a-z0-9]+", path) if segment}
    return bool(segments & _CAREER_TOKENS)


def prioritized_candidates(
    candidates: Iterable[SitemapCandidate],
    *,
    priority_base: int = 1000,
) -> tuple[tuple[SitemapCandidate, int], ...]:
    """Deterministically prioritize likely career URLs and newer advisory lastmod.

    The order is scheduling only.  It is never interpreted as proof that an
    older URL is unchanged or that a missing URL has disappeared.
    """

    values = list(candidates)
    def sort_key(item: SitemapCandidate):
        stamp = _lastmod_timestamp(item.lastmod)
        return (
            0 if _career_like(item.url) else 1,
            -(stamp if stamp is not None else float("-inf")),
            item.url,
        )

    values.sort(key=sort_key)
    total = len(values)
    return tuple(
        (
            item,
            int(priority_base)
            + (10_000 if _career_like(item.url) else 0)
            + (total - index),
        )
        for index, item in enumerate(values)
    )


def parse_sitemap(
    body: bytes,
    *,
    sitemap_url: str,
    index_depth: int = 0,
    limits: SitemapLimits = SitemapLimits(),
) -> SitemapParseResult:
    """Parse one already-fetched sitemap document without performing I/O."""

    if not isinstance(body, (bytes, bytearray)):
        return SitemapParseResult(
            None,
            diagnostics=(SitemapDiagnostic("INVALID_BODY", "sitemap body must be bytes"),),
        )
    payload = bytes(body)
    if len(payload) > limits.max_bytes:
        return SitemapParseResult(
            None,
            diagnostics=(
                SitemapDiagnostic(
                    "TOO_LARGE",
                    f"sitemap exceeds {limits.max_bytes} byte bound",
                    url=sitemap_url,
                ),
            ),
            truncated=True,
        )
    if _FORBIDDEN_XML.search(payload):
        return SitemapParseResult(
            None,
            diagnostics=(
                SitemapDiagnostic(
                    "UNSAFE_XML",
                    "DOCTYPE/ENTITY declarations are forbidden in sitemap XML",
                    url=sitemap_url,
                ),
            ),
        )
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        return SitemapParseResult(
            None,
            diagnostics=(
                SitemapDiagnostic(
                    "MALFORMED_XML", f"malformed sitemap XML: {exc}", url=sitemap_url
                ),
            ),
        )

    root_name = _local_name(str(root.tag))
    if root_name == "urlset":
        kind = SitemapDocumentKind.URLSET
        child_name = "url"
    elif root_name == "sitemapindex":
        kind = SitemapDocumentKind.INDEX
        child_name = "sitemap"
    else:
        return SitemapParseResult(
            None,
            diagnostics=(
                SitemapDiagnostic(
                    "UNKNOWN_ROOT",
                    f"unsupported sitemap root element {root_name!r}",
                    url=sitemap_url,
                ),
            ),
        )

    if kind is SitemapDocumentKind.INDEX and index_depth >= limits.max_index_depth:
        return SitemapParseResult(
            kind,
            diagnostics=(
                SitemapDiagnostic(
                    "INDEX_DEPTH_LIMIT",
                    f"sitemap index depth {index_depth} reached bound {limits.max_index_depth}",
                    url=sitemap_url,
                ),
            ),
            truncated=True,
        )

    diagnostics: list[SitemapDiagnostic] = []
    candidates: list[SitemapCandidate] = []
    seen: set[str] = set()
    matching_children = [node for node in list(root) if _local_name(str(node.tag)) == child_name]
    truncated = len(matching_children) > limits.max_items
    if truncated:
        diagnostics.append(
            SitemapDiagnostic(
                "ITEM_LIMIT",
                f"sitemap contains more than {limits.max_items} bounded entries",
                url=sitemap_url,
            )
        )

    for node in matching_children[: limits.max_items]:
        raw_loc = _direct_child_text(node, "loc")
        if raw_loc is None:
            diagnostics.append(
                SitemapDiagnostic("MISSING_LOC", "sitemap entry has no <loc>", url=sitemap_url)
            )
            continue
        entry, entry_diags = _normalized_candidate(
            raw_loc,
            lastmod=_direct_child_text(node, "lastmod"),
            limits=limits,
        )
        diagnostics.extend(entry_diags)
        if entry is None:
            continue
        identity = crawl_identity(entry.url)
        if identity in seen:
            diagnostics.append(
                SitemapDiagnostic(
                    "DUPLICATE_URL",
                    "duplicate normalized sitemap URL suppressed",
                    url=entry.url,
                    lastmod=entry.lastmod,
                )
            )
            continue
        seen.add(identity)
        candidates.append(entry)

    return SitemapParseResult(kind, tuple(candidates), tuple(diagnostics), truncated)


def discover_sitemaps_from_robots(
    lines: Iterable[str],
    *,
    limits: SitemapLimits = SitemapLimits(),
) -> SitemapDiscovery:
    """Extract absolute ``Sitemap:`` locations from an already parsed robots file."""

    candidates: list[SitemapCandidate] = []
    diagnostics: list[SitemapDiagnostic] = []
    seen: set[str] = set()
    for raw_line in lines:
        line = str(raw_line).split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        if field.lower() != "sitemap":
            continue
        entry, entry_diags = _normalized_candidate(value, lastmod=None, limits=limits)
        diagnostics.extend(entry_diags)
        if entry is None:
            continue
        identity = crawl_identity(entry.url)
        if identity in seen:
            diagnostics.append(
                SitemapDiagnostic(
                    "DUPLICATE_URL",
                    "duplicate normalized robots Sitemap directive suppressed",
                    url=entry.url,
                )
            )
            continue
        seen.add(identity)
        candidates.append(entry)
        if len(candidates) >= limits.max_items:
            diagnostics.append(
                SitemapDiagnostic(
                    "ITEM_LIMIT",
                    f"robots sitemap discovery reached {limits.max_items} item bound",
                )
            )
            break
    return SitemapDiscovery(tuple(candidates), tuple(diagnostics))


def build_sitemap_request_plan(url: str) -> RequestPlan:
    """Plan one sitemap fetch through the ordinary host HTTP execution seam."""

    target = crawl_url(url).normalized
    return RequestPlan(
        method="GET",
        url=target,
        headers={
            "Accept": "application/xml,text/xml,application/xhtml+xml;q=0.8,text/plain;q=0.5"
        },
        expected_content_types=(
            "application/xml",
            "text/xml",
            "application/xhtml+xml",
            "text/plain",
        ),
        timeout_s=SITEMAP_TIMEOUT_S,
        max_bytes=SITEMAP_MAX_BYTES,
        purpose="SOURCE_CRAWL",
        expected_operation_class="READ",
    )


def _enqueue_candidates(
    conn: sqlite3.Connection,
    *,
    candidates: Iterable[SitemapCandidate],
    role: str,
    run_id: str,
    plan_row: Mapping[str, object],
    parent_request_id: str | None,
    scope: CrawlScope,
    budget: CrawlBudget,
    base_url: str,
    now: str,
    page_depth: int,
    sitemap_depth: int,
) -> SitemapEnqueueResult:
    normalized_role = role.upper()
    if normalized_role not in {"PAGE", "SITEMAP"}:
        raise ValueError("sitemap frontier role must be PAGE or SITEMAP")

    created = reused = 0
    diagnostics: list[SitemapDiagnostic] = []
    plan_id = str(plan_row["id"])

    for candidate, priority in prioritized_candidates(candidates):
        usage = load_usage(conn, plan_id, now=now)
        task = DiscoveredTask(
            kind="CRAWL",
            # PAGE uses the ordinary URL identity so duplicate discoveries from
            # multiple sitemaps collapse onto the same frontier unit. SITEMAP
            # keeps an explicit metadata role so the same URL can still be
            # fetched as content later without conflating semantics.
            logical_key=(
                "" if normalized_role == "PAGE"
                else f"SITEMAP:{crawl_identity(candidate.url)}"
            ),
            target_reference=candidate.url,
            priority=priority,
            depth=(0 if normalized_role == "SITEMAP" else max(0, int(page_depth))),
            parent_reference=base_url,
        )
        decision = enqueue_discovered_task(
            conn,
            run_id=run_id,
            plan_row=plan_row,
            parent_request_id=parent_request_id,
            discovered=task,
            scope=scope,
            budget=budget,
            usage=usage,
            base_url=base_url,
            now=now,
            role=normalized_role,
            payload_extra={
                "sitemap_parent": base_url,
                "sitemap_lastmod": candidate.lastmod,
                "sitemap_depth": int(sitemap_depth),
                "sitemap_lastmod_advisory": True,
            },
        )
        if decision.accepted:
            if decision.created:
                created += 1
            else:
                reused += 1
            continue
        diagnostics.append(
            SitemapDiagnostic(
                decision.reason,
                f"sitemap candidate refused by ordinary frontier: {decision.reason}",
                url=decision.normalized_target or candidate.url,
                lastmod=candidate.lastmod,
            )
        )
    return SitemapEnqueueResult(created, reused, tuple(diagnostics))


def enqueue_robots_sitemaps(
    conn: sqlite3.Connection,
    *,
    discovery: SitemapDiscovery,
    run_id: str,
    plan_row: Mapping[str, object],
    parent_request_id: str | None,
    scope: CrawlScope,
    budget: CrawlBudget,
    base_url: str,
    now: str,
) -> SitemapEnqueueResult:
    return _enqueue_candidates(
        conn,
        candidates=discovery.candidates,
        role="SITEMAP",
        run_id=run_id,
        plan_row=plan_row,
        parent_request_id=parent_request_id,
        scope=scope,
        budget=budget,
        base_url=base_url,
        now=now,
        page_depth=0,
        sitemap_depth=0,
    )


def enqueue_sitemap_result(
    conn: sqlite3.Connection,
    *,
    parsed: SitemapParseResult,
    current_sitemap_depth: int,
    run_id: str,
    plan_row: Mapping[str, object],
    parent_request_id: str | None,
    scope: CrawlScope,
    budget: CrawlBudget,
    base_url: str,
    now: str,
    page_depth: int,
) -> SitemapEnqueueResult:
    if parsed.kind is None or not parsed.candidates:
        return SitemapEnqueueResult()
    if parsed.kind is SitemapDocumentKind.INDEX:
        role = "SITEMAP"
        next_sitemap_depth = int(current_sitemap_depth) + 1
        next_page_depth = 0
    else:
        role = "PAGE"
        next_sitemap_depth = int(current_sitemap_depth)
        next_page_depth = max(1, int(page_depth))
    return _enqueue_candidates(
        conn,
        candidates=parsed.candidates,
        role=role,
        run_id=run_id,
        plan_row=plan_row,
        parent_request_id=parent_request_id,
        scope=scope,
        budget=budget,
        base_url=base_url,
        now=now,
        page_depth=next_page_depth,
        sitemap_depth=next_sitemap_depth,
    )


__all__ = [
    "SITEMAP_MAX_BYTES",
    "SITEMAP_MAX_INDEX_DEPTH",
    "SITEMAP_MAX_ITEMS",
    "SitemapCandidate",
    "SitemapDiagnostic",
    "SitemapDiscovery",
    "SitemapDocumentKind",
    "SitemapEnqueueResult",
    "SitemapLimits",
    "SitemapParseResult",
    "build_sitemap_request_plan",
    "discover_sitemaps_from_robots",
    "enqueue_robots_sitemaps",
    "enqueue_sitemap_result",
    "parse_sitemap",
    "prioritized_candidates",
]
