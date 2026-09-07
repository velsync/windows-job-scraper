"""Lever adapter (FEED_OR_PUBLIC_STRUCTURED_ENDPOINT).

Public postings API: ``https://api.lever.co/v0/postings/{company}?mode=json``.
Enumerates the full postings list — authoritative full-source.
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

from jobscraper.acquisition.adapters.base import content_fingerprint, stable_observation_key
from jobscraper.acquisition.contracts import (
    AdapterManifest,
    AdapterTaskKind,
    CrawlCursor,
    ObservationDraft,
    ParseOutcome,
    ParseOutcomeKind,
    RequestPlan,
    ValidatedResultEnvelope,
)

ADAPTER_ID = "lever"
ADAPTER_VERSION = "1.0.0"

MANIFEST = AdapterManifest(
    id=ADAPTER_ID,
    version=ADAPTER_VERSION,
    adapter_api_version="1",
    capabilities=("discover", "listing_parse", "incremental"),
    supported_execution_classes=("HTTP",),
    supported_auth_modes=("NONE",),
    cost_class="LIGHT",
    cursor_schema_version=1,
    supports_absence_authority=True,
    listing_identity_sufficient=True,
)


def postings_url(company: str, base_url: str | None = None) -> str:
    if base_url:
        return f"{base_url.rstrip('/')}/v0/postings/{quote(company, safe='')}?mode=json"
    return f"https://api.lever.co/v0/postings/{quote(company, safe='')}?mode=json"


def parse_entry_url(entry_url: str) -> str | None:
    parts = urlsplit(entry_url)
    host = (parts.netloc or "").lower()
    if "lever.co" in host:
        segs = [s for s in parts.path.split("/") if s]
        if segs:
            return segs[0]
    return None


class LeverAdapter:
    manifest = MANIFEST

    def __init__(self, company: str, base_url: str | None = None) -> None:
        self.company = company
        self.base_url = base_url

    def plan(self, task, cursor, ctx) -> RequestPlan:
        return RequestPlan(
            method="GET",
            url=postings_url(self.company, self.base_url),
            expected_content_types=("application/json",),
            allowed_redirects=False,
            timeout_s=30.0,
            purpose=f"enumerate lever postings {self.company}",
            expected_operation_class="READ_ONLY",
        )

    def parse(self, task, result: ValidatedResultEnvelope, ctx) -> ParseOutcome:
        body = (result.result.body or b"").decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except ValueError:
            return ParseOutcome(kind=ParseOutcomeKind.FAILURE, failure_kind="PARSE_EMPTY", failure_detail="invalid JSON")
        if not isinstance(data, list):
            return ParseOutcome(
                kind=ParseOutcomeKind.FAILURE,
                failure_kind="UNEXPECTED_CONTENT",
                failure_detail="expected postings array",
            )
        drafts: list[ObservationDraft] = []
        for order, posting in enumerate(data):
            if not isinstance(posting, dict) or not posting.get("id"):
                continue
            source_job_id = str(posting["id"])
            drafts.append(
                ObservationDraft(
                    source_job_id=source_job_id,
                    raw_url=posting.get("hostedUrl"),
                    canonical_url_candidate=posting.get("hostedUrl"),
                    application_url_candidate=posting.get("applyUrl") or posting.get("hostedUrl"),
                    content=self._normalize(posting),
                    source_rank_or_order=order,
                    enumeration_scope_key=self.scope_key(),
                    observation_unique_key=stable_observation_key(
                        self.company, source_job_id, self.scope_key(), source_job_id
                    ),
                )
            )
        return ParseOutcome(
            kind=ParseOutcomeKind.SUCCESS_WITH_JOBS if drafts else ParseOutcomeKind.SUCCESS_EMPTY,
            observations=tuple(drafts),
            coverage_proposal=self._coverage(),
            continuation_required=False,
        )

    def next_cursor(self, task, outcome, current_cursor, ctx):
        return None

    def scope_key(self) -> str:
        return f"lever:{self.company}"

    def _coverage(self):
        from jobscraper.acquisition.contracts import CoverageProposal

        return CoverageProposal(
            scope_key=self.scope_key(),
            enumeration_scope_key=self.scope_key(),
            page_cursor=None,
            is_terminal=True,
            stop_reason="full_postings_enumerated",
        )

    def _normalize(self, p: dict) -> dict:
        categories = p.get("categories") or {}
        return {
            "title": p.get("text"),
            "company": self.company.replace("-", " ").title(),
            "description_html": p.get("descriptionPlain") or p.get("description"),
            "description_plain": p.get("descriptionPlain"),
            "location_raw": categories.get("location"),
            "department": categories.get("department"),
            "team": categories.get("team"),
            "workplace_type": categories.get("workplaceType"),
            "commitment": categories.get("commitment"),
            "employment_type": categories.get("commitment"),
            "created_at": p.get("createdAt"),
            "salary_range": self._salary(p.get("salaryRange")),
            "lists": p.get("lists") or [],
            "fingerprint": content_fingerprint(str(p.get("id") or ""), p.get("text") or "", p.get("descriptionPlain") or ""),
        }

    def _salary(self, s) -> dict | None:
        if isinstance(s, dict) and (s.get("min") or s.get("max")):
            return {
                "min": s.get("min"),
                "max": s.get("max"),
                "currency": s.get("currency"),
                "period": "year",
                "raw": json.dumps(s),
            }
        return None
