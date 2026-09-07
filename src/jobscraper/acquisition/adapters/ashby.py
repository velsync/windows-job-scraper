"""Ashby adapter (FEED_OR_PUBLIC_STRUCTURED_ENDPOINT).

Public job-board API: ``https://api.ashbyhq.com/posting-api/job-board/{org}``.
Paginated via ``pageNo``/``limit`` — cursor carries the page state; the final
page declares terminal enumeration.
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

from jobscraper.acquisition.adapters.base import content_fingerprint, stable_observation_key
from jobscraper.acquisition.contracts import (
    AdapterManifest,
    CrawlCursor,
    ObservationDraft,
    ParseOutcome,
    ParseOutcomeKind,
    RequestPlan,
    ValidatedResultEnvelope,
)

ADAPTER_ID = "ashby"
ADAPTER_VERSION = "1.0.0"

MANIFEST = AdapterManifest(
    id=ADAPTER_ID,
    version=ADAPTER_VERSION,
    adapter_api_version="1",
    capabilities=("discover", "listing_parse", "incremental"),
    supported_execution_classes=("HTTP",),
    supported_auth_modes=("NONE",),
    cost_class="LIGHT",
    cursor_schema_version=2,
    supports_absence_authority=True,
    listing_identity_sufficient=True,
)

PAGE_LIMIT = 100


def board_url(org: str, base_url: str | None = None) -> str:
    if base_url:
        return f"{base_url.rstrip('/')}/posting-api/job-board/{quote(org, safe='')}"
    return f"https://api.ashbyhq.com/posting-api/job-board/{quote(org, safe='')}"


def parse_entry_url(entry_url: str) -> str | None:
    parts = urlsplit(entry_url)
    host = (parts.netloc or "").lower()
    if "ashbyhq.com" in host:
        segs = [s for s in parts.path.split("/") if s]
        if segs:
            return segs[0]
    return None


class AshbyAdapter:
    manifest = MANIFEST

    def __init__(self, org: str, base_url: str | None = None) -> None:
        self.org = org
        self.base_url = base_url

    def plan(self, task, cursor, ctx) -> RequestPlan:
        page = 0
        if cursor is not None and cursor.state_json:
            try:
                state = json.loads(cursor.state_json)
                page = int(state.get("page", 0))
            except ValueError:
                page = 0
        url = f"{board_url(self.org, self.base_url)}?pageNo={page}&limit={PAGE_LIMIT}"
        return RequestPlan(
            method="GET",
            url=url,
            expected_content_types=("application/json",),
            allowed_redirects=False,
            timeout_s=30.0,
            purpose=f"enumerate ashby board page {page}",
            expected_operation_class="READ_ONLY",
        )

    def parse(self, task, result: ValidatedResultEnvelope, ctx) -> ParseOutcome:
        body = (result.result.body or b"").decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except ValueError:
            return ParseOutcome(kind=ParseOutcomeKind.FAILURE, failure_kind="PARSE_EMPTY", failure_detail="invalid JSON")
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            return ParseOutcome(
                kind=ParseOutcomeKind.FAILURE,
                failure_kind="UNEXPECTED_CONTENT",
                failure_detail="no jobs array",
            )
        page = self._current_page(result.result.requested_url or "")
        drafts = []
        for order, job in enumerate(jobs):
            if not isinstance(job, dict) or not job.get("id"):
                continue
            source_job_id = str(job["id"])
            drafts.append(
                ObservationDraft(
                    source_job_id=source_job_id,
                    raw_url=job.get("jobUrl"),
                    canonical_url_candidate=job.get("jobUrl"),
                    application_url_candidate=job.get("applyUrl") or job.get("jobUrl"),
                    content=self._normalize(job),
                    source_rank_or_order=order,
                    enumeration_scope_key=self.scope_key(),
                    observation_unique_key=stable_observation_key(
                        self.org, source_job_id, f"{self.scope_key()}#p{page}", source_job_id
                    ),
                )
            )
        terminal = len(jobs) < PAGE_LIMIT
        from jobscraper.acquisition.contracts import CoverageProposal, DiscoveredTask, DiscoveredTaskKind

        discovered: tuple = ()
        if not terminal:
            discovered = (
                DiscoveredTask(
                    kind=DiscoveredTaskKind.ENUMERATE,
                    logical_key=f"{self.scope_key()}#p{page + 1}",
                    target_reference=f"page:{page + 1}",
                    priority=100,
                    depth=1,
                ),
            )
        return ParseOutcome(
            kind=ParseOutcomeKind.SUCCESS_WITH_JOBS if drafts else ParseOutcomeKind.SUCCESS_EMPTY,
            observations=tuple(drafts),
            discovered_tasks=discovered,
            cursor_proposal={"page": 0} if terminal else {"page": page + 1},
            coverage_proposal=CoverageProposal(
                scope_key=self.scope_key(),
                enumeration_scope_key=self.scope_key(),
                page_cursor=f"p{page}",
                is_terminal=terminal,
                stop_reason="final_page_reached" if terminal else None,
            ),
            continuation_required=not terminal,
        )

    def next_cursor(self, task, outcome, current_cursor, ctx):
        if outcome.coverage_proposal and outcome.coverage_proposal.is_terminal:
            return None
        page = 0
        if current_cursor is not None and current_cursor.state_json:
            try:
                page = int(json.loads(current_cursor.state_json).get("page", 0))
            except ValueError:
                page = 0
        return CrawlCursor(
            source_id=ctx.source_id if hasattr(ctx, "source_id") else self.org,
            binding_id="ashby",
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            cursor_schema_version=2,
            state_json=json.dumps({"page": page + 1}),
            checkpoint_at="",
        )

    def _current_page(self, url: str) -> int:
        parts = urlsplit(url)
        for pair in (parts.query or "").split("&"):
            if pair.startswith("pageNo="):
                try:
                    return int(pair.split("=", 1)[1])
                except ValueError:
                    return 0
        return 0

    def scope_key(self) -> str:
        return f"ashby:{self.org}"

    def _normalize(self, job: dict) -> dict:
        loc = job.get("location") or ""
        secondary = [l.get("location") for l in (job.get("secondaryLocations") or []) if isinstance(l, dict)]
        return {
            "title": job.get("title"),
            "company": (job.get("company") or self.org).replace("-", " ").title(),
            "description_html": job.get("description"),
            "location_raw": loc,
            "secondary_locations": [s for s in secondary if s],
            "department": job.get("department"),
            "team": job.get("team"),
            "employment_type": job.get("employmentType"),
            "is_remote": bool(job.get("isRemote")),
            "published_at": job.get("publishedAt"),
            "updated_at": job.get("updatedAt"),
            "salary": self._salary(job.get("salaryRangeDescription") or ""),
            "fingerprint": content_fingerprint(str(job.get("id") or ""), job.get("title") or "", job.get("description") or ""),
        }

    def _salary(self, text: str) -> dict | None:
        if not text:
            return None
        return {"raw": text, "source": "ashby"}
