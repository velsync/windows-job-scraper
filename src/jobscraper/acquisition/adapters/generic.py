"""Generic HTML / JSON-LD / structured-data adapter.

Authority: module 02 sections 12.1 (fingerprint before specialized route) and
22 (ExtractionRecipe). Used for ordinary careers pages and as the built-in
immutable generic discovery binding for unknown URLs.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlsplit

from jobscraper.acquisition.adapters.base import content_fingerprint, stable_observation_key
from jobscraper.acquisition.contracts import (
    AdapterManifest,
    CoverageProposal,
    CrawlCursor,
    DiscoveredTask,
    DiscoveredTaskKind,
    ObservationDraft,
    ParseOutcome,
    ParseOutcomeKind,
    ParseOutcomeKind as _POK,
    RequestPlan,
    ValidatedResultEnvelope,
)
from jobscraper.recipes import htmlutil
from jobscraper.recipes.models import ExtractionRecipe

ADAPTER_ID = "generic"
ADAPTER_VERSION = "1.0.0"

MANIFEST = AdapterManifest(
    id=ADAPTER_ID,
    version=ADAPTER_VERSION,
    adapter_api_version="1",
    capabilities=("discover", "listing_parse", "detail_parse"),
    supported_execution_classes=("HTTP", "BROWSER"),
    supported_auth_modes=("NONE",),
    cost_class="MEDIUM",
    cursor_schema_version=1,
    supports_absence_authority=False,  # generic pages prove page scope only
    listing_identity_sufficient=False,
)


def _jsonld_job_postings(root: htmlutil.Element) -> list[dict]:
    out: list[dict] = []
    for script in htmlutil.find_all(root, "script"):
        ctype = (script.attr("type") or "").lower()
        if ctype != "application/ld+json":
            continue
        raw = script.text(recursive=False)
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("JobPosting", "jobPosting"):
                out.append(item)
    return out


class GenericAdapter:
    """Recipe-driven generic listing adapter (HTML or JSON-LD)."""

    manifest = MANIFEST

    def __init__(self, entry_url: str, recipe: ExtractionRecipe | None = None) -> None:
        self.entry_url = entry_url
        self.recipe = recipe
        self._seen_page_hashes: set[str] = set()

    # -------------------------------------------------------------- planning
    def plan(self, task, cursor, ctx) -> RequestPlan:
        url = self.entry_url
        if cursor is not None and cursor.state_json:
            try:
                state = json.loads(cursor.state_json)
                if state.get("next_url"):
                    url = state["next_url"]
            except ValueError:
                pass
        return RequestPlan(
            method="GET",
            url=url,
            expected_content_types=("text/html", "application/json"),
            allowed_redirects=False,
            timeout_s=30.0,
            purpose="generic listing fetch",
            expected_operation_class="READ_ONLY",
        )

    # -------------------------------------------------------------- parsing
    def parse(self, task, result: ValidatedResultEnvelope, ctx) -> ParseOutcome:
        body = (result.result.body or b"").decode("utf-8", errors="replace")
        base_url = result.result.final_url or self.entry_url
        if task.kind.value == "DISCOVER":
            return self._parse_discovery(body, base_url)
        postings = _jsonld_job_postings(htmlutil.parse_html(body))
        if postings:
            return self._outcome_from_jsonld(postings, base_url)
        if self.recipe is not None:
            from jobscraper.recipes.extractor import extract_records

            records = extract_records(self.recipe, body, base_url)
            if records:
                return self._outcome_from_recipe(records, base_url)
            return ParseOutcome(
                kind=_POK.FAILURE,
                failure_kind="PARSE_EMPTY",
                failure_detail="recipe produced no records from valid page",
            )
        return ParseOutcome(kind=_POK.FAILURE, failure_kind="PARSE_EMPTY", failure_detail="no structured data or recipe")

    def _parse_discovery(self, body: str, base_url: str) -> ParseOutcome:
        from jobscraper.acquisition.fingerprint import fingerprint_html

        fp = fingerprint_html(body, base_url)
        content = {
            "title": fp.title,
            "fingerprint": content_fingerprint(base_url, fp.family or ""),
        }
        draft = ObservationDraft(
            source_job_id=None,
            raw_url=base_url,
            canonical_url_candidate=None,
            application_url_candidate=None,
            content=content,
            enumeration_scope_key=f"discovery:{base_url}",
            observation_unique_key=stable_observation_key(base_url, None, "discovery", base_url),
        )
        return ParseOutcome(
            kind=_POK.SUCCESS_WITH_JOBS,
            observations=(draft,),
            coverage_proposal=CoverageProposal(
                scope_key=f"discovery:{base_url}",
                enumeration_scope_key=f"discovery:{base_url}",
                page_cursor=None,
                is_terminal=True,
                stop_reason="discovery_probe_complete",
            ),
            continuation_required=False,
        )

    def _outcome_from_jsonld(self, postings: list[dict], base_url: str) -> ParseOutcome:
        drafts = []
        for order, posting in enumerate(postings):
            source_job_id = None
            url = posting.get("url") or posting.get("sameAs") or base_url
            # Job-specific URL-derived identity.
            m = re.search(r"/(?:jobs?|careers?|positions?)/(?:[^/]+/)?(\d{3,})", url or "")
            if m:
                source_job_id = m.group(1)
            content = self._normalize_jsonld(posting)
            drafts.append(
                ObservationDraft(
                    source_job_id=source_job_id,
                    raw_url=url,
                    canonical_url_candidate=url,
                    application_url_candidate=posting.get("url"),
                    content=content,
                    source_rank_or_order=order,
                    enumeration_scope_key=self.scope_key(),
                    observation_unique_key=stable_observation_key(
                        base_url, source_job_id or content["fingerprint"], self.scope_key(),
                        source_job_id or str(order),
                    ),
                )
            )
        return ParseOutcome(
            kind=_POK.SUCCESS_WITH_JOBS if drafts else _POK.SUCCESS_EMPTY,
            observations=tuple(drafts),
            coverage_proposal=CoverageProposal(
                scope_key=self.scope_key(),
                enumeration_scope_key=self.scope_key(),
                page_cursor=None,
                is_terminal=True,
                stop_reason="jsonld_enumerated",
            ),
        )

    def _outcome_from_recipe(self, records: list[dict], base_url: str) -> ParseOutcome:
        drafts = []
        for order, record in enumerate(records):
            source_job_id = record.get("source_job_id")
            drafts.append(
                ObservationDraft(
                    source_job_id=source_job_id,
                    raw_url=record.get("job_url"),
                    canonical_url_candidate=record.get("job_url"),
                    application_url_candidate=record.get("job_url"),
                    content=record,
                    source_rank_or_order=order,
                    enumeration_scope_key=self.scope_key(),
                    observation_unique_key=stable_observation_key(
                        base_url, source_job_id or record.get("fingerprint") or "", self.scope_key(),
                        source_job_id or str(order),
                    ),
                )
            )
        return ParseOutcome(
            kind=_POK.SUCCESS_WITH_JOBS if drafts else _POK.SUCCESS_EMPTY,
            observations=tuple(drafts),
            coverage_proposal=CoverageProposal(
                scope_key=self.scope_key(),
                enumeration_scope_key=self.scope_key(),
                page_cursor=None,
                is_terminal=True,
                stop_reason="recipe_enumerated",
            ),
        )

    def _normalize_jsonld(self, p: dict) -> dict:
        org = p.get("hiringOrganization") or {}
        loc = p.get("jobLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = loc.get("address") or {}
        salary = p.get("baseSalary") or {}
        return {
            "title": p.get("title"),
            "company": org.get("name"),
            "company_url": org.get("sameAs"),
            "description_html": p.get("description"),
            "date_posted": p.get("datePosted"),
            "valid_through": p.get("validThrough"),
            "employment_type": p.get("employmentType"),
            "location_raw": ", ".join(
                str(x) for x in [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")] if x
            ),
            "remote": (p.get("jobLocationType") or "").lower() == "TELECOMMUTE".lower(),
            "applicant_location_requirements": p.get("applicantLocationRequirements"),
            "salary": self._jsonld_salary(salary),
            "fingerprint": content_fingerprint(str(p.get("title") or ""), str(org.get("name") or ""), str(p.get("datePosted") or "")),
        }

    def _jsonld_salary(self, s: dict) -> dict | None:
        if not s:
            return None
        value = s.get("value") or {}
        currency = s.get("currency") or value.get("currency")
        if value.get("minValue") or value.get("maxValue") or value.get("value"):
            return {
                "min": value.get("minValue", value.get("value")),
                "max": value.get("maxValue", value.get("value")),
                "currency": currency,
                "period": (value.get("unitText") or "").lower() or None,
                "raw": json.dumps(s),
            }
        return None

    def next_cursor(self, task, outcome, current_cursor, ctx):
        return None

    def scope_key(self) -> str:
        return f"generic:{self.entry_url}"


class GenericDiscoveryAdapter(GenericAdapter):
    """The built-in immutable generic discovery binding used before any
    specialized routing. Its authority is limited to the user-supplied target
    plus host-approved redirects and normal security policy."""

    manifest = AdapterManifest(
        id="generic_discovery",
        version=ADAPTER_VERSION,
        adapter_api_version="1",
        capabilities=("discover",),
        supported_execution_classes=("HTTP",),
        supported_auth_modes=("NONE",),
        cost_class="LIGHT",
        cursor_schema_version=1,
        supports_absence_authority=False,
        listing_identity_sufficient=False,
    )
