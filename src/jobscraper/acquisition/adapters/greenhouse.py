"""Greenhouse adapter (PROVIDER_NATIVE public boards API).

Authority: module 02 section 12.3 (initial provider priorities).

Uses the public boards API: ``https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true``.
The endpoint enumerates the full board — the binding contract therefore
declares authoritative full-source enumeration (absence inference allowed for
COMPLETE generations when source policy permits).
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

from jobscraper.acquisition.adapters.base import content_fingerprint, stable_observation_key
from jobscraper.acquisition.contracts import (
    AdapterManifest,
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    ObservationDraft,
    ParseOutcome,
    ParseOutcomeKind,
    RequestPlan,
    ValidatedResultEnvelope,
)

ADAPTER_ID = "greenhouse"
ADAPTER_VERSION = "1.0.0"

MANIFEST = AdapterManifest(
    id=ADAPTER_ID,
    version=ADAPTER_VERSION,
    adapter_api_version="1",
    capabilities=("discover", "listing_parse", "detail_parse", "incremental"),
    supported_execution_classes=("HTTP",),
    supported_auth_modes=("NONE",),
    cost_class="LIGHT",
    cursor_schema_version=1,
    supports_absence_authority=True,
    listing_identity_sufficient=True,
)

BASE = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


def jobs_url(board: str, base_url: str | None = None) -> str:
    if base_url:
        return f"{base_url.rstrip('/')}/v1/boards/{quote(board, safe='')}/jobs?content=true"
    return BASE.format(board=quote(board, safe=""))


def parse_entry_url(entry_url: str) -> str | None:
    """Extract the board token from a Greenhouse careers URL."""
    parts = urlsplit(entry_url)
    host = (parts.netloc or "").lower()
    if host.endswith("greenhouse.io"):
        # job-boards.greenhouse.io/<board> or boards.greenhouse.io/<board>
        segs = [s for s in parts.path.split("/") if s]
        if segs:
            return segs[0]
    return None


class GreenhouseAdapter:
    manifest = MANIFEST

    def __init__(self, board: str, base_url: str | None = None) -> None:
        self.board = board
        self.base_url = base_url

    def plan(self, task, cursor, ctx) -> RequestPlan:
        url = jobs_url(self.board, self.base_url)
        validators = {}
        if cursor is not None and cursor.state_json:
            state = json.loads(cursor.state_json)
            if state.get("etag"):
                validators["If-None-Match"] = state["etag"]
        return RequestPlan(
            method="GET",
            url=url,
            expected_content_types=("application/json",),
            allowed_redirects=False,
            timeout_s=30.0,
            max_bytes=10 * 1024 * 1024,
            revalidation_headers_allowed=True,
            purpose=f"enumerate board {self.board}",
            expected_operation_class="READ_ONLY",
            validators=validators,
        )

    def parse(self, task, result: ValidatedResultEnvelope, ctx) -> ParseOutcome:
        body = (result.result.body or b"").decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except ValueError:
            return ParseOutcome(
                kind=ParseOutcomeKind.FAILURE,
                failure_kind="PARSE_EMPTY",
                failure_detail="invalid JSON from greenhouse board",
            )
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            return ParseOutcome(
                kind=ParseOutcomeKind.FAILURE,
                failure_kind="UNEXPECTED_CONTENT" if isinstance(data, dict) and "error" in data else "PARSE_EMPTY",
                failure_detail="no jobs array in response",
            )
        meta = data.get("meta", {}) or {}
        drafts: list[ObservationDraft] = []
        for order, job in enumerate(jobs):
            if not isinstance(job, dict) or not job.get("id"):
                continue
            source_job_id = str(job["id"])
            content = self._normalize_job(job)
            drafts.append(
                ObservationDraft(
                    source_job_id=source_job_id,
                    raw_url=job.get("absolute_url"),
                    canonical_url_candidate=job.get("absolute_url"),
                    application_url_candidate=(job.get("absolute_url") or "").rstrip("/") or None,
                    content=content,
                    source_rank_or_order=order,
                    enumeration_scope_key=self.scope_key(),
                    observation_unique_key=stable_observation_key(
                        self.board, source_job_id, self.scope_key(), source_job_id
                    ),
                )
            )
        return ParseOutcome(
            kind=(
                ParseOutcomeKind.SUCCESS_WITH_JOBS
                if drafts
                else ParseOutcomeKind.SUCCESS_EMPTY
            ),
            observations=tuple(drafts),
            coverage_proposal=self._coverage(task, terminal=True, meta=meta),
            continuation_required=False,
        )

    def next_cursor(self, task, outcome, current_cursor, ctx) -> CrawlCursor | None:
        # Single-response full enumeration; cursor carries only validators state.
        return None

    def scope_key(self) -> str:
        return f"greenhouse:{self.board}"

    def _coverage(self, task, *, terminal: bool, meta: dict):
        from jobscraper.acquisition.contracts import CoverageProposal

        return CoverageProposal(
            scope_key=self.scope_key(),
            enumeration_scope_key=self.scope_key(),
            page_cursor=None,
            is_terminal=terminal,
            stop_reason="full_board_enumerated" if terminal else None,
        )

    def _normalize_job(self, job: dict) -> dict:
        location = job.get("location") or {}
        metadata = {m.get("name"): m.get("value") for m in (job.get("metadata") or []) if isinstance(m, dict)}
        return {
            "title": job.get("title"),
            "company": self._company_name(),
            "description_html": job.get("content"),
            "location_raw": location.get("name"),
            "updated_at": job.get("updated_at"),
            "first_published": job.get("first_published"),
            "offices": [
                {"name": o.get("name"), "location": o.get("location")}
                for o in (job.get("offices") or [])
                if isinstance(o, dict)
            ],
            "departments": [d.get("name") for d in (job.get("departments") or []) if isinstance(d, dict)],
            "metadata": metadata,
            "employment_type": metadata.get("Employment Type") or metadata.get("employment_type"),
            "fingerprint": content_fingerprint(
                str(job.get("id") or ""), job.get("title") or "", job.get("content") or ""
            ),
        }

    def _company_name(self) -> str:
        return self.board.replace("-", " ").replace("_", " ").title()
