"""Source adapter base contracts.

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md ACQ-02
(adapter protocol), section 8 (Source/adapter/binding model).

Adapters plan and parse; executors perform I/O. A parser never sees an
unchecked response — only a :class:`ValidatedResultEnvelope`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from jobscraper.acquisition.contracts import (
    AdapterManifest,
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    DiscoveredTask,
    DiscoveredTaskKind,
    ObservationDraft,
    ParseContext,
    ParseOutcome,
    ParseOutcomeKind,
    PlanningContext,
    RequestPlan,
    ValidatedResultEnvelope,
)


@runtime_checkable
class SourceAdapter(Protocol):
    manifest: AdapterManifest

    def plan(
        self, task: AdapterTask, cursor: CrawlCursor | None, ctx: PlanningContext
    ) -> RequestPlan: ...

    def parse(
        self, task: AdapterTask, result: ValidatedResultEnvelope, ctx: ParseContext
    ) -> ParseOutcome: ...

    def next_cursor(
        self,
        task: AdapterTask,
        outcome: ParseOutcome,
        current_cursor: CrawlCursor | None,
        ctx: ParseContext,
    ) -> CrawlCursor | None: ...


def content_fingerprint(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            continue
        h.update(str(p).encode("utf-8", errors="replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def stable_observation_key(source_id: str, source_job_id: str | None, cursor_key: str, record_key: str) -> str:
    """Deterministic per-request observation identity.

    Uniqueness within the request prevents retry/PARTIAL duplicates while
    later independent runs may re-observe.
    """
    return hashlib.sha256(
        f"{source_id}|{cursor_key}|{record_key}|{source_job_id or ''}".encode("utf-8")
    ).hexdigest()[:40]


def scope_key_for_binding(source_id: str, binding_id: str, scope_hint: str | None = None) -> str:
    return f"{source_id}:{binding_id}" + (f":{scope_hint}" if scope_hint else "")


__all__ = [
    "AdapterTaskKind",
    "CrawlCursor",
    "DiscoveredTask",
    "DiscoveredTaskKind",
    "ObservationDraft",
    "ParseOutcomeKind",
    "SourceAdapter",
    "content_fingerprint",
    "stable_observation_key",
    "scope_key_for_binding",
]
