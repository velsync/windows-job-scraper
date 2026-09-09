"""S2.7 unit tests: the built-in Ashby Job Posting API code adapter.

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md
(§9 manifest/permissions, §12.3 provider priorities, §19 pagination/stop
policy, §22 required-field discipline, §26 fixture corpus, §27 typed
failures, §31/§32 URL + origin evidence, ACQ-02 protocol and request-type
mapping, ACQ-03 parse outcomes, ACQ-04 detail-child planning, ACQ-09
versioned contracts); docs/plans/slice-2-worker-implementation-plan-v0313.md
S2.7 ("same shape as S2.5": provider-native adapter + deterministic
fixtures + the same contract path, no new I/O surface, no new write path).

Provider facts these tests encode (researched 2026-09-09, recorded in
``tests/fixtures/ashby/README.md`` and the S2.7 review):

* the public posting API is one endpoint,
  ``GET /posting-api/job-board/{org}?includeCompensation=true``, returning a
  **single full document** ``{"jobs": [...], "apiVersion": "1"}`` — no
  pagination, no declared total — so one recognized response is the whole
  board membership (AUTHORITATIVE_FULL_SOURCE), there is never a cursor,
  and ``listing_identity_sufficient`` is true;
* there is **no public per-job detail endpoint** (``/job/{id}`` answers
  401), so the adapter supports no DETAIL task and proposes no child work;
* ``isListed: false`` means "direct link only — not part of the public
  board": excluded from membership with typed review evidence;
* ``publishedAt`` is the provider-stated publication time (it maps to
  ``posted_at``); ``compensation.scrapeableCompensationSalarySummary`` is
  the provider-stated scrapeable salary text;
* ``jobUrl``/``applyUrl`` are accepted only when the versioned endpoint
  table reads them as **this board's, this posting's** hosted URL
  (``applyUrl`` is the posting URL plus ``/application``).

Proves, against the deterministic sanitized corpus in
``tests/fixtures/ashby/``:

* planning is built only from the **pinned** board token and the versioned
  endpoint table — never from a URL found in page content, and never
  towards a detail endpoint that does not exist;
* a changed template, a malformed body, or an all-rejected document is a
  typed ``PARSE_MARKER_MISSING`` failure with ACQ-09 evidence references,
  never ``SUCCESS_EMPTY``;
* a rejected listed member or an unrecognized ``apiVersion`` is ``PARTIAL``:
  good observations persist but no absence authority may be inferred;
* the adapter stays pure: no I/O, no database, no policy authority, no
  provider host literal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pytest

from jobscraper.acquisition.failures import FailureKind
from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    ParseOutcomeKind,
    PlanningContext,
    StopPolicy,
    ValidatedResultEnvelope,
    task_kind_for_request_type,
    validate_manifest,
)
from jobscraper.adapters.ashby import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    MANIFEST,
    STOP_POLICY,
    AshbyAdapter,
    AshbyConfig,
)
from jobscraper.adapters.registry import BUILTIN_ADAPTERS, build_adapter, get_adapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ashby"

BOARD = "acme"
API = "https://api.ashbyhq.com"
LIST_PATH = f"/posting-api/job-board/{BOARD}"
LIST_URL = f"{API}{LIST_PATH}?includeCompensation=true"
CONFIG = {"board": BOARD, "company_name": "Acme Fixtures"}

ID1 = "5a1b2c3d-0000-4000-8000-000000005001"
ID2 = "5a1b2c3d-0000-4000-8000-000000005002"
ID3 = "5a1b2c3d-0000-4000-8000-000000005003"
UNLISTED = "5a1b2c3d-0000-4000-8000-000000005009"
OTHER = "9e9f9a9b-0000-4000-8000-000000009999"
HOSTED = "https://jobs.ashbyhq.com/acme"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_json(name: str):
    return json.loads(_fixture(name).decode("utf-8"))


def _adapter(**overrides) -> AshbyAdapter:
    return AshbyAdapter(AshbyConfig(**{**CONFIG, **overrides}))


def _ctx(plan_id: str = "rsp-1") -> PlanningContext:
    return PlanningContext(run_id="run-1", run_source_plan_id=plan_id)


def _task(kind: AdapterTaskKind = AdapterTaskKind.ENUMERATE, payload=None) -> AdapterTask:
    return AdapterTask(kind=kind, payload=payload or {})


def _envelope(body: bytes, *, url: str = LIST_URL, status: int = 200,
              content_type: str = "application/json") -> ResultEnvelope:
    return ResultEnvelope(
        execution_plan_id="plan-1",
        request_id="req-1",
        attempt_id="att-1",
        run_source_plan_id="rsp-1",
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        requested_url=url,
        final_url=url,
        status_code=status,
        content_type=content_type,
        body=body,
    ).finalize()


def _validated(
    fixture: str,
    *,
    page_class: PageClass = PageClass.VALID_LIST,
    status: int = 200,
    content_type: str = "application/json",
    url: str = LIST_URL,
) -> ValidatedResultEnvelope:
    return ValidatedResultEnvelope(
        envelope=_envelope(_fixture(fixture), url=url, status=status, content_type=content_type),
        page_class=page_class,
        validation_evidence={"fixture": fixture},
    )


def _validated_payload(payload, *, url: str = LIST_URL,
                       page_class: PageClass = PageClass.VALID_LIST) -> ValidatedResultEnvelope:
    return ValidatedResultEnvelope(
        envelope=_envelope(json.dumps(payload).encode("utf-8"), url=url),
        page_class=page_class,
        validation_evidence={"shape": "inline"},
    )


def _parse(adapter: AshbyAdapter, fixture: str, *, kind=AdapterTaskKind.ENUMERATE, **kwargs):
    return adapter.parse(_task(kind), _validated(fixture, **kwargs), ctx=None)


def _parse_payload(adapter: AshbyAdapter, payload, *, kind=AdapterTaskKind.ENUMERATE, **kwargs):
    return adapter.parse(_task(kind), _validated_payload(payload, **kwargs), ctx=None)


def _fields_of(outcome, source_job_id: str) -> dict:
    for observation in outcome.observations:
        if observation.source_job_id == source_job_id:
            return observation.fields
    raise AssertionError(f"no observation for {source_job_id}")


def _job(payload: Mapping | dict | None = None, **overrides) -> dict:
    """One minimal valid inline board member (overridable per test)."""
    member = {
        "id": ID1,
        "title": "Backend Engineer",
        "location": "Berlin, Germany",
        "isListed": True,
        "isRemote": False,
        "workplaceType": "OnSite",
        "jobUrl": f"{HOSTED}/{ID1}",
        "applyUrl": f"{HOSTED}/{ID1}/application",
        "descriptionHtml": "<p>Body</p>",
        "descriptionPlain": "Body",
    }
    if payload:
        member.update(payload)
    member.update(overrides)
    return member


# --------------------------------------------------------------------------
# Manifest, registry, stop policy
# --------------------------------------------------------------------------


class TestManifestAndRegistry:
    def test_manifest_is_schema_valid_and_http_only(self):
        from dataclasses import asdict

        manifest = validate_manifest(asdict(MANIFEST))
        assert manifest.id == "ashby"
        assert manifest.version == "1.0.0"
        assert manifest.supported_execution_classes == ("HTTP",)
        assert manifest.supported_auth_modes == ("NONE",)
        assert manifest.cost_class == "LIGHT"
        assert {"listing_parse", "health", "smoke"} <= set(manifest.capabilities)
        # discovery is the generic binding's job; there is no incremental
        # feed; and the public posting API has no per-job detail endpoint,
        # so claiming detail parse would be a lie.
        assert "discover" not in manifest.capabilities
        assert "incremental" not in manifest.capabilities
        assert "detail_parse" not in manifest.capabilities

    def test_registered_as_a_builtin_and_constructible_from_config(self):
        assert BUILTIN_ADAPTERS["ashby"] is AshbyAdapter
        assert get_adapter("ashby") is AshbyAdapter
        adapter = build_adapter("ashby", CONFIG)
        assert isinstance(adapter, AshbyAdapter)
        assert adapter.config.board == BOARD
        assert adapter.manifest.id == "ashby"

    def test_registry_refuses_unknown_config_keys(self):
        with pytest.raises(ValueError):
            build_adapter("ashby", {**CONFIG, "follow_redirect_hosts": ["evil.test"]})
        with pytest.raises(ValueError):
            build_adapter("ashby", {**CONFIG, "api_hosts": ["evil.test"]})
        # Lever/Greenhouse knobs the Ashby contract does not have are
        # unknown keys, not silently accepted no-ops
        with pytest.raises(ValueError):
            build_adapter("ashby", {**CONFIG, "detail_fetch": True})
        with pytest.raises(ValueError):
            build_adapter("ashby", {**CONFIG, "page_size": 10})

    def test_registry_refuses_a_non_mapping_config(self):
        with pytest.raises((ValueError, TypeError)):
            AshbyConfig.from_mapping("board=acme")  # type: ignore[arg-type]

    def test_router_advertises_only_provider_native_for_ashby(self):
        from jobscraper.adapters.router import _IMPLEMENTED_STRATEGIES

        assert _IMPLEMENTED_STRATEGIES["ashby"] == frozenset({"PROVIDER_NATIVE"})

    def test_stop_policy_is_declared_and_bounded(self):
        policy = AshbyAdapter.stop_policy
        assert isinstance(policy, StopPolicy)
        assert policy is STOP_POLICY
        # one document is the whole board: a single page, a single request
        assert policy.max_pages == 1
        assert policy.max_requests == 1
        assert policy.max_duplicate_pages == 1
        assert policy.max_runtime_s > 0

    def test_listing_identity_is_declared_sufficient(self):
        assert AshbyAdapter.listing_identity_sufficient is True

    def test_the_adapter_cannot_exceed_its_own_declared_request_budget(self):
        """One enumeration task plans exactly one request and proposes no
        child work, so the declared budget is honest by construction."""
        adapter = _adapter()
        plan = adapter.plan(_task(), None, ctx=_ctx())
        assert plan.url == LIST_URL
        outcome = _parse(adapter, "board_jobs.json")
        assert outcome.discovered_tasks == ()
        assert STOP_POLICY.max_pages * 1 <= STOP_POLICY.max_requests


# --------------------------------------------------------------------------
# Config validation (typed, fail closed)
# --------------------------------------------------------------------------


class TestConfig:
    def test_board_is_required(self):
        with pytest.raises(TypeError):
            AshbyConfig(company_name="Acme")  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            AshbyConfig(board="")
        with pytest.raises(ValueError):
            AshbyConfig(board="   ")

    def test_board_is_lowercased(self):
        # the provider's API answered the same document for /job-board/Ashby
        # and /job-board/ashby and echoed the request's spelling, so the
        # pinned token is canonicalized exactly like the other providers
        assert AshbyConfig(board="Acme").board == "acme"
        assert AshbyConfig(board="ACME-Corp_9").board == "acme-corp_9"

    @pytest.mark.parametrize(
        "token",
        ["a" * 65, "-acme", "ac me", "ac.me", "acme/x", "acme?x",
         "acme#x", "acme%", " Café"],
    )
    def test_board_token_shape_is_strict(self, token):
        with pytest.raises(ValueError):
            AshbyConfig(board=token)

    def test_board_token_bounds_match_the_sibling_providers(self):
        # same slug grammar as the reviewed Greenhouse/Lever adapters
        assert AshbyConfig(board="a" * 64).board == "a" * 64
        assert AshbyConfig(board="acme-").board == "acme-"

    def test_board_token_rejects_control_characters(self):
        with pytest.raises(ValueError):
            AshbyConfig(board="acme\nsett")
        with pytest.raises(ValueError):
            AshbyConfig(board="acme\x7f")

    @pytest.mark.parametrize(
        "word",
        ["jobs", "job", "board", "boards", "api", "v0", "v1", "job-board",
         "posting-api", "search", "apply", "application", "careers"],
    )
    def test_route_words_are_never_a_board_token(self, word):
        with pytest.raises(ValueError):
            AshbyConfig(board=word)

    def test_api_base_must_be_a_bare_http_origin(self):
        assert AshbyConfig(board=BOARD).api_base_url == API
        assert AshbyConfig(board=BOARD, api_base_url="HTTP://127.0.0.1:8080").api_base_url == "http://127.0.0.1:8080"
        for bad in ("https://user:pw@api.ashbyhq.com", "ftp://api.ashbyhq.com",
                    "https://api.ashbyhq.com/posting-api", "https://api.ashbyhq.com/?x=1",
                    "https://api.ashbyhq.com/#frag", "", "not a url"):
            with pytest.raises(ValueError):
                AshbyConfig(board=BOARD, api_base_url=bad)

    def test_company_name_is_never_invented(self):
        assert AshbyConfig(board=BOARD).company_name is None
        with pytest.raises(ValueError):
            AshbyConfig(board=BOARD, company_name="   ")
        with pytest.raises(ValueError):
            AshbyConfig(board=BOARD, company_name="x" * 201)

    def test_careers_url_must_be_safe_http(self):
        assert AshbyConfig(board=BOARD, careers_url="https://acme.example/careers").careers_url
        for bad in ("javascript:alert(1)", "data:text/html,x", "https://user:pw@acme.example/"):
            with pytest.raises(ValueError):
                AshbyConfig(board=BOARD, careers_url=bad)

    def test_timeout_and_body_caps_are_host_policy(self):
        with pytest.raises(ValueError):
            AshbyConfig(board=BOARD, timeout_s=31)
        with pytest.raises(ValueError):
            AshbyConfig(board=BOARD, timeout_s=0)
        with pytest.raises(ValueError):
            AshbyConfig(board=BOARD, timeout_s="30")
        with pytest.raises(ValueError):
            AshbyConfig(board=BOARD, max_bytes=2_000_001)
        with pytest.raises(ValueError):
            AshbyConfig(board=BOARD, max_bytes=0)
        with pytest.raises(ValueError):
            AshbyConfig(board=BOARD, max_bytes=True)

    def test_from_mapping_round_trip_and_unknown_keys(self):
        config = AshbyConfig.from_mapping({"board": "Acme"})
        assert config.board == "acme"
        with pytest.raises(ValueError):
            AshbyConfig.from_mapping({"board": "acme", "stealth": 1})


# --------------------------------------------------------------------------
# Planning (ACQ-02/ACQ-04): one endpoint, no child fetch surface
# --------------------------------------------------------------------------


class TestPlanning:
    def test_enumerate_plans_the_board_document_with_compensation(self):
        plan = _adapter().plan(_task(), None, ctx=_ctx())
        assert plan.method == "GET"
        assert plan.url == LIST_URL
        assert plan.headers["Accept"] == "application/json"
        assert plan.expected_content_types == ("application/json",)
        assert plan.purpose == "ENUMERATE"

    def test_pinned_api_base_is_used_verbatim(self):
        adapter = _adapter(api_base_url="http://127.0.0.1:8123")
        assert adapter.plan(_task(), None, ctx=_ctx()).url == (
            f"http://127.0.0.1:8123{LIST_PATH}?includeCompensation=true"
        )

    def test_health_and_smoke_plan_the_board_document(self):
        adapter = _adapter()
        for kind in (AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE):
            plan = adapter.plan(_task(kind), None, ctx=None)
            assert plan.url == LIST_URL
            assert plan.purpose in {"HEALTH", "SMOKE"}

    @pytest.mark.parametrize(
        "kind", [AdapterTaskKind.DETAIL, AdapterTaskKind.CRAWL, AdapterTaskKind.DISCOVER]
    )
    def test_unsupported_tasks_are_refused_explicitly(self, kind):
        """There is no public per-job endpoint (verified 2026-09-09), crawl
        breadth is ROAD-04 and discovery belongs to the generic binding:
        every one is a typed refusal, never a planned URL (ACQ-02)."""
        with pytest.raises(ValueError):
            _adapter().plan(_task(kind, {"target_reference": ID1}), None, ctx=None)
        with pytest.raises(ValueError):
            _adapter().parse(_task(kind, {"target_reference": ID1}), _validated("board_jobs.json"), ctx=None)

    def test_a_cursor_is_never_consulted_because_there_is_one_document(self):
        """A stale cursor of any provenance can never narrow the full board."""
        cursor = CrawlCursor(
            source_id="src-1", binding_id="bnd-1", adapter_id="ashby",
            adapter_version="1.0.0", cursor_schema_version=1,
            state_json=json.dumps({"plan": "rsp-1", "offset": 100}), checkpoint_at="",
        )
        assert _adapter().plan(_task(), cursor, ctx=_ctx("rsp-1")).url == LIST_URL

    def test_planning_uses_the_planning_context_read_only(self):
        ctx = _ctx()
        plan = _adapter().plan(_task(), None, ctx=ctx)
        assert plan.url == LIST_URL
        assert ctx == _ctx()

    def test_timeout_and_bytes_come_from_the_pinned_config(self):
        adapter = _adapter(timeout_s=10, max_bytes=500_000)
        plan = adapter.plan(_task(), None, ctx=None)
        assert plan.timeout_s == 10
        assert plan.max_bytes == 500_000


# --------------------------------------------------------------------------
# ACQ-02 request-type ↔ task mapping
# --------------------------------------------------------------------------


class TestRequestTypeMapping:
    @pytest.mark.parametrize(
        "request_type,kind",
        [
            ("SOURCE_HEALTH_CHECK", AdapterTaskKind.HEALTH),
            ("SOURCE_DISCOVERY", AdapterTaskKind.DISCOVER),
            ("LIST_FETCH", AdapterTaskKind.ENUMERATE),
            ("SOURCE_CRAWL", AdapterTaskKind.CRAWL),
            ("DETAIL_FETCH", AdapterTaskKind.DETAIL),
            ("ADAPTER_SMOKE", AdapterTaskKind.SMOKE),
        ],
    )
    def test_acquisition_request_types_map_explicitly(self, request_type, kind):
        assert task_kind_for_request_type(request_type) is kind

    def test_every_mapped_task_kind_has_an_explicit_plan_or_refusal(self):
        adapter = _adapter()
        for kind in AdapterTaskKind:
            payload = {"target_reference": ID1} if kind is AdapterTaskKind.DETAIL else {}
            if kind in (AdapterTaskKind.CRAWL, AdapterTaskKind.DISCOVER, AdapterTaskKind.DETAIL):
                with pytest.raises(ValueError):
                    adapter.plan(_task(kind, payload), None, ctx=None)
            else:
                assert adapter.plan(_task(kind, payload), None, ctx=None).url


# --------------------------------------------------------------------------
# ENUMERATE parsing
# --------------------------------------------------------------------------


class TestListParse:
    def test_valid_document_produces_one_observation_per_listed_posting(self):
        outcome = _parse(_adapter(), "board_jobs.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert [o.source_job_id for o in outcome.observations] == [ID1, ID2, ID3]
        assert [o.fields["title"] for o in outcome.observations] == [
            "Backend Engineer",
            "Platform Engineer",
            "Senior Data Engineer",
        ]
        assert [o.source_rank_or_order for o in outcome.observations] == [0, 1, 2]
        assert outcome.failure is None

    def test_one_document_is_terminal_and_proposes_no_child_work(self):
        outcome = _parse(_adapter(), "board_jobs.json")
        assert outcome.continuation_required is False
        assert outcome.cursor_proposal is None
        assert outcome.discovered_tasks == ()
        assert outcome.coverage_proposal == {
            "declared_total": None,
            "api_version": "1",
            "observed": 3,
            "rejected_members": 0,
            "unlisted_excluded": 0,
        }

    def test_every_outcome_carries_acq09_evidence_refs(self):
        outcome = _parse(_adapter(), "board_jobs.json")
        assert len(outcome.evidence_refs) == 2

    def test_reviewed_job_links_become_url_candidates(self):
        outcome = _parse(_adapter(), "board_jobs.json")
        for posting_id in (ID1, ID2):
            observation = next(o for o in outcome.observations if o.source_job_id == posting_id)
            assert observation.canonical_url_candidate == f"{HOSTED}/{posting_id}"
            # the application link the product shows comes from the endpoint
            # table + pinned board + validated id, not from content
            assert observation.application_url_candidate == f"{HOSTED}/{posting_id}"
            assert observation.fields["hosted_url"] == f"{HOSTED}/{posting_id}"
            assert observation.fields["apply_url"] == f"{HOSTED}/{posting_id}/application"

    def test_mapped_fields_come_from_the_document(self):
        fields = _fields_of(_parse(_adapter(), "board_jobs.json"), ID1)
        assert fields["team"] == "Infrastructure"
        assert fields["department"] == "Engineering"
        assert fields["employment_type"] == "FullTime"
        assert fields["workplace_type"] == "OnSite"
        assert fields["is_remote"] is False
        assert fields["locations"] == ["Berlin, Germany"]
        assert fields["address"] == {
            "addressRegion": "Berlin",
            "addressCountry": "Germany",
            "addressLocality": "Berlin",
        }
        assert fields["description"].startswith("<div><p>Own the ingestion pipeline")
        assert "job_location_type" not in fields

    def test_published_at_is_the_stated_publication_time(self):
        """Ashby states publishedAt as the last *publication* time, so unlike
        Lever's createdAt it is a legitimate posted_at (02 §22)."""
        fields = _fields_of(_parse(_adapter(), "board_jobs.json"), ID1)
        assert fields["posted_at"] == "2026-06-01T09:30:00.000000Z"
        assert _fields_of(_parse(_adapter(), "board_jobs.json"), ID2)["posted_at"] == (
            "2026-06-03T14:05:00.000000Z"
        )

    def test_an_unusable_published_at_is_dropped_never_guessed(self):
        outcome = _parse_payload(_adapter(), {"jobs": [_job(publishedAt="not a date")], "apiVersion": "1"})
        assert "posted_at" not in outcome.observations[0].fields

    def test_multi_location_keeps_primary_then_secondaries_as_stated(self):
        fields = _fields_of(_parse(_adapter(), "board_jobs.json"), ID2)
        assert fields["locations"] == ["Berlin, Germany", "Paris, France", "Lisbon, Portugal"]

    def test_compensation_summary_is_the_salary_signal(self):
        fields = _fields_of(_parse(_adapter(), "board_jobs.json"), ID1)
        assert fields["salary"] == "$150K - $210K"
        assert fields["compensation_summary"] == "$150K – $210K • Offers Equity"

    def test_missing_compensation_stays_unknown_never_zero(self):
        fields = _fields_of(_parse(_adapter(), "board_jobs.json"), ID2)
        assert "salary" not in fields
        assert "compensation_summary" not in fields

    def test_remote_posts_flag_the_documented_signals(self):
        fields = _fields_of(_parse(_adapter(), "board_jobs.json"), ID3)
        assert fields["is_remote"] is True
        assert fields["workplace_type"] == "Remote"
        assert fields["job_location_type"] == "REMOTE"

    def test_workplace_remote_alone_flags_remote(self):
        outcome = _parse_payload(
            _adapter(), {"jobs": [_job(isRemote=False, workplaceType="Remote")], "apiVersion": "1"}
        )
        assert outcome.observations[0].fields["job_location_type"] == "REMOTE"

    def test_is_remote_true_alone_flags_remote(self):
        outcome = _parse_payload(
            _adapter(), {"jobs": [_job(isRemote=True, workplaceType="Hybrid")], "apiVersion": "1"}
        )
        fields = outcome.observations[0].fields
        assert fields["job_location_type"] == "REMOTE"
        assert fields["workplace_type"] == "Hybrid"

    def test_unrecognized_optional_values_are_evidence_never_silenced(self):
        outcome = _parse_payload(
            _adapter(),
            {"jobs": [_job(isRemote="yes", workplaceType="Anywhere")], "apiVersion": "1"},
        )
        fields = outcome.observations[0].fields
        assert "job_location_type" not in fields
        assert "workplace_type" not in fields
        assert "is_remote" not in fields
        refused = [item for item in outcome.review_evidence if item["reason"] == "UNRECOGNIZED_VALUE_REFUSED"]
        assert refused
        assert {entry["field"] for entry in refused[0]["fields"]} == {"isRemote", "workplaceType"}

    def test_employment_type_is_passed_through_unmapped(self):
        outcome = _parse_payload(
            _adapter(), {"jobs": [_job(employmentType="Seasonal")], "apiVersion": "1"}
        )
        assert outcome.observations[0].fields["employment_type"] == "Seasonal"

    def test_description_falls_back_to_plain_text(self):
        outcome = _parse_payload(
            _adapter(),
            {"jobs": [_job(descriptionHtml="", descriptionPlain="Plain body")], "apiVersion": "1"},
        )
        assert outcome.observations[0].fields["description"] == "Plain body"

    def test_a_posting_without_description_is_still_an_observation(self):
        member = _job(descriptionHtml="", descriptionPlain="")
        outcome = _parse_payload(_adapter(), {"jobs": [member], "apiVersion": "1"})
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert "description" not in outcome.observations[0].fields

    def test_empty_board_is_success_empty(self):
        outcome = _parse(_adapter(), "board_jobs_empty.json", page_class=PageClass.EMPTY)
        assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
        assert outcome.observations == ()
        assert outcome.coverage_proposal == {
            "declared_total": None,
            "api_version": "1",
            "observed": 0,
            "rejected_members": 0,
            "unlisted_excluded": 0,
        }
        assert outcome.evidence_refs

    def test_company_and_careers_url_come_from_binding_config_only(self):
        outcome = _parse(_adapter(careers_url="https://acme.example/careers"), "board_jobs.json")
        fields = _fields_of(outcome, ID1)
        assert fields["company"] == "Acme Fixtures"
        assert fields["careers_url"] == "https://acme.example/careers"

    def test_company_is_absent_when_the_binding_does_not_declare_it(self):
        outcome = _parse(_adapter(company_name=None), "board_jobs.json")
        assert "company" not in _fields_of(outcome, ID1)

    # ------------------------------------------------ isListed discipline

    def test_unlisted_postings_are_excluded_with_evidence(self):
        outcome = _parse(_adapter(), "board_jobs_unlisted.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert [o.source_job_id for o in outcome.observations] == [ID1]
        excluded = [item for item in outcome.review_evidence if item["reason"] == "UNLISTED_POSTING_EXCLUDED"]
        assert len(excluded) == 1
        assert excluded[0]["posting_id"] == UNLISTED
        assert excluded[0]["order"] == 1
        assert outcome.coverage_proposal["unlisted_excluded"] == 1

    def test_a_board_of_only_unlisted_postings_is_success_empty(self):
        outcome = _parse(_adapter(), "board_jobs_only_unlisted.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
        assert outcome.observations == ()
        assert outcome.coverage_proposal["unlisted_excluded"] == 1
        assert any(item["reason"] == "UNLISTED_POSTING_EXCLUDED" for item in outcome.review_evidence)

    def test_is_listed_is_excluded_only_on_literal_false(self):
        member = _job(isListed=None)
        member.pop("isListed")
        outcome = _parse_payload(_adapter(), {"jobs": [member], "apiVersion": "1"})
        assert [o.source_job_id for o in outcome.observations] == [ID1]

    def test_a_non_boolean_is_listed_is_evidence_and_keeps_membership(self):
        outcome = _parse_payload(
            _adapter(), {"jobs": [_job(isListed="no")], "apiVersion": "1"}
        )
        assert [o.source_job_id for o in outcome.observations] == [ID1]
        refused = [item for item in outcome.review_evidence if item["reason"] == "UNRECOGNIZED_VALUE_REFUSED"]
        assert refused and refused[0]["fields"][0]["field"] == "isListed"

    # ------------------------------------------- membership-proof failures

    def test_a_rejected_listed_member_is_partial_never_terminal(self):
        outcome = _parse(_adapter(), "board_jobs_rejected_member.json")
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        # PARTIAL is not FAILURE: the good observations stay persisted
        assert [o.source_job_id for o in outcome.observations] == [ID1, ID2]
        rejected = [item for item in outcome.review_evidence if item["reason"] == "REQUIRED_FIELD_MISSING"]
        assert len(rejected) == 1
        assert rejected[0]["order"] == 1
        assert rejected[0]["missing"] == ["id"]
        assert outcome.coverage_proposal["rejected_members"] == 1
        assert outcome.continuation_required is False
        assert outcome.evidence_refs

    def test_a_document_whose_members_all_fail_is_a_typed_failure(self):
        outcome = _parse(_adapter(), "board_jobs_missing_required_fields.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.failure.retryable is False
        review = [item for item in outcome.review_evidence if item["reason"] == "REQUIRED_FIELD_MISSING"]
        assert len(review) == 3
        # the traversal id survives only as bounded evidence, never identity
        assert any("passwd" in json.dumps(item) for item in review)
        assert outcome.evidence_refs

    @pytest.mark.parametrize(
        "member",
        [
            _job(id=None),
            _job(id="../../etc/passwd"),
            _job(id=ID1.upper()),
            _job(id="short"),
            _job(id=f"{ID1}?x=1"),
            _job(id=5001),
            _job(title=None),
            _job(title="   "),
            _job(title=42),
            "not-an-object",
            42,
        ],
    )
    def test_a_member_failing_required_fields_is_rejected(self, member):
        outcome = _parse_payload(_adapter(), {"jobs": [member, _job(id=OTHER)], "apiVersion": "1"})
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        assert [o.source_job_id for o in outcome.observations] == [OTHER]

    def test_changed_template_is_a_failure_not_an_empty_board(self):
        outcome = _parse(_adapter(), "board_jobs_changed_template.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        review = [item for item in outcome.review_evidence if item["reason"] == "ITEMS_MARKER_MISSING"]
        assert review and review[0]["top_level_keys"] == ["postings", "version"]
        assert outcome.observations == ()
        assert outcome.evidence_refs

    def test_malformed_json_is_a_typed_failure_with_refs(self):
        outcome = _parse(_adapter(), "board_jobs_malformed.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.evidence_refs

    @pytest.mark.parametrize("payload", [[], [1, 2], "text", 42, None])
    def test_a_non_object_document_is_a_typed_failure(self, payload):
        outcome = _parse_payload(_adapter(), payload)
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.evidence_refs

    def test_jobs_member_that_is_not_a_list_is_a_typed_failure(self):
        outcome = _parse_payload(_adapter(), {"jobs": {"data": []}, "apiVersion": "1"})
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        review = [item for item in outcome.review_evidence if item["reason"] == "ITEMS_MARKER_MISSING"]
        assert review and review[0]["jobs_type"] == "dict"

    # ------------------------------------------------ apiVersion discipline

    def test_an_unrecognized_api_version_is_partial_not_complete(self):
        outcome = _parse(_adapter(), "board_jobs_api_v2.json")
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        assert [o.source_job_id for o in outcome.observations] == [ID1]
        review = [item for item in outcome.review_evidence if item["reason"] == "UNRECOGNIZED_API_VERSION"]
        assert review and review[0]["api_version"] == "2"
        assert outcome.coverage_proposal["api_version"] == "2"

    @pytest.mark.parametrize("version", [None, 1, 1.0, ["1"], "2", "", "1.1"])
    def test_any_api_version_but_string_1_degrades_coverage(self, version):
        payload = {"jobs": [_job()], "apiVersion": version}
        outcome = _parse_payload(_adapter(), payload)
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        assert outcome.observations

    def test_a_missing_api_version_key_degrades_coverage(self):
        outcome = _parse_payload(_adapter(), {"jobs": [_job()]})
        assert outcome.kind is ParseOutcomeKind.PARTIAL

    def test_an_unrecognized_api_version_on_an_empty_board_is_not_authoritative(self):
        outcome = _parse_payload(_adapter(), {"jobs": [], "apiVersion": "2"})
        # the empty board itself is recognized; the unrecognized contract
        # version still withholds absence authority (ACQ-03, RUN-13)
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        assert outcome.observations == ()
        assert outcome.coverage_proposal["observed"] == 0

    # ------------------------------------------------- duplicate identity

    def test_a_duplicate_posting_id_is_evidence_and_first_wins(self):
        first = _job(id=ID1, title="First Spelling")
        second = _job(id=ID1, title="Second Spelling", location="Paris, France")
        outcome = _parse_payload(_adapter(), {"jobs": [first, second], "apiVersion": "1"})
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert [o.source_job_id for o in outcome.observations] == [ID1]
        assert outcome.observations[0].fields["title"] == "First Spelling"
        duplicates = [item for item in outcome.review_evidence if item["reason"] == "DUPLICATE_POSTING_ID"]
        assert duplicates and duplicates[0]["posting_id"] == ID1

    # ----------------------------------------------------- reviewed links

    @pytest.mark.parametrize(
        "link",
        [
            f"https://jobs.ashbyhq.com/OTHERBOARD/{ID1}",       # another board
            f"https://jobs.ashbyhq.com/acme/{OTHER}",           # another posting
            f"https://api.ashbyhq.com/posting-api/job-board/acme/job/{ID1}",  # API shape
            f"https://jobs.ashbyhq.com.evil.test/acme/{ID1}",   # look-alike host
            f"https://example.com/acme/{ID1}",                  # foreign host
            f"https://jobs.ashbyhq.com/acme/../{ID1}",          # traversal
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "//jobs.ashbyhq.com/acme/" + ID1,                   # schemeless
            "not a url",
            42,
        ],
    )
    def test_a_job_url_that_is_not_this_posting_is_refused(self, link):
        outcome = _parse_payload(
            _adapter(), {"jobs": [_job(jobUrl=link)], "apiVersion": "1"}
        )
        observation = outcome.observations[0]
        assert observation.canonical_url_candidate is None
        assert "hosted_url" not in observation.fields
        # the derived application link still comes from the endpoint table
        assert observation.application_url_candidate == f"{HOSTED}/{ID1}"
        assert any(item["reason"] == "UNSAFE_URL_REFUSED" for item in outcome.review_evidence)

    @pytest.mark.parametrize(
        "link",
        [
            f"https://jobs.ashbyhq.com/acme/{OTHER}/application",                # other posting
            f"https://jobs.ashbyhq.com/OTHERBOARD/{ID1}/application",            # other board
            f"https://jobs.ashbyhq.com/acme/{ID1}/apply",                        # lever shape
            f"https://api.ashbyhq.com/posting-api/job-board/acme/job/{ID1}/application",
            "javascript:alert(1)",
            42,
        ],
    )
    def test_an_apply_url_that_is_not_this_postings_application_page_is_refused(self, link):
        outcome = _parse_payload(
            _adapter(), {"jobs": [_job(applyUrl=link)], "apiVersion": "1"}
        )
        observation = outcome.observations[0]
        assert "apply_url" not in observation.fields
        assert any(item["reason"] == "UNSAFE_URL_REFUSED" for item in outcome.review_evidence)

    def test_a_tracked_apply_url_is_evidence_only_never_link_authority(self):
        """Parity with the reviewed S2.6 link gate: identity comparison is
        urlnorm's job (tracking parameters are cleaning, not a different
        posting), so a tracked application URL is kept as raw evidence —
        while the link the product shows is still derived from the endpoint
        table + pinned board + validated id, never from content."""
        tracked = f"https://jobs.ashbyhq.com/acme/{ID1}/application?utm_source=feed"
        outcome = _parse_payload(
            _adapter(), {"jobs": [_job(applyUrl=tracked)], "apiVersion": "1"}
        )
        observation = outcome.observations[0]
        assert observation.fields["apply_url"] == tracked
        assert observation.application_url_candidate == f"{HOSTED}/{ID1}"

    @pytest.mark.parametrize(
        "host", ["jobs.ashbyhq.com", "apps.ashbyhq.com"]
    )
    def test_reviewed_host_links_are_accepted_on_every_reviewed_hosted_host(self, host):
        outcome = _parse_payload(
            _adapter(),
            {"jobs": [_job(
                jobUrl=f"https://{host}/acme/{ID1}",
                applyUrl=f"https://{host}/acme/{ID1}/application",
            )], "apiVersion": "1"},
        )
        fields = outcome.observations[0].fields
        assert outcome.observations[0].canonical_url_candidate == f"https://{host}/acme/{ID1}"
        assert fields["hosted_url"] == f"https://{host}/acme/{ID1}"
        assert fields["apply_url"] == f"https://{host}/acme/{ID1}/application"

    def test_hostile_fixture_links_are_refused_end_to_end(self):
        outcome = _parse(_adapter(), "board_jobs.json")
        observation = next(o for o in outcome.observations if o.source_job_id == ID3)
        # the honest jobUrl is kept as the canonical candidate; the hostile
        # applyUrl is refused with typed evidence and never becomes a link
        assert observation.canonical_url_candidate == f"{HOSTED}/{ID3}"
        assert observation.fields["hosted_url"] == f"{HOSTED}/{ID3}"
        assert observation.application_url_candidate == f"{HOSTED}/{ID3}"
        assert "apply_url" not in observation.fields
        refused = [item for item in outcome.review_evidence if item["reason"] == "UNSAFE_URL_REFUSED"]
        assert refused
        assert {entry["field"] for entry in refused[0]["fields"]} == {"applyUrl"}
        # the raw description field is untouched — neutralization is the
        # host cleaner's job, not the adapter's
        assert "javascript:" in observation.fields["description"]
        assert "<script>" in observation.fields["description"]

    # ------------------------------------------------------ field evidence

    def test_every_field_carries_locator_evidence(self):
        outcome = _parse(_adapter(), "board_jobs.json")
        observation = outcome.observations[0]
        locators = {record.field_name: record.locator_value for record in observation.field_evidence}
        assert locators["source_job_id"] == "id"
        assert locators["title"] == "title"
        assert locators["posted_at"] == "publishedAt"
        assert locators["employment_type"] == "employmentType"
        assert locators["workplace_type"] == "workplaceType"
        assert locators["is_remote"] == "isRemote"
        assert locators["locations"] == "location+secondaryLocations"
        assert locators["salary"] == "compensation.scrapeableCompensationSalarySummary"
        assert locators["description"] == "descriptionHtml"
        assert locators["company"] == "company_name"
        for record in observation.field_evidence:
            assert record.value_hash and record.locator_kind


# --------------------------------------------------------------------------
# HEALTH / SMOKE probe parsing
# --------------------------------------------------------------------------


class TestHealthProbe:
    @pytest.mark.parametrize("kind", [AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE])
    def test_a_recognized_board_shape_is_a_healthy_probe(self, kind):
        outcome = _parse(_adapter(), "board_jobs.json", kind=kind)
        assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
        assert outcome.observations == ()
        review = [item for item in outcome.review_evidence if item["reason"] == "HEALTH_PROBE_RECOGNIZED"]
        assert review and review[0]["listed_postings"] == 3
        assert outcome.evidence_refs

    def test_a_recognized_empty_board_is_a_healthy_probe(self):
        outcome = _parse(
            _adapter(), "board_jobs_empty.json", kind=AdapterTaskKind.HEALTH,
            page_class=PageClass.EMPTY,
        )
        assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY

    @pytest.mark.parametrize("fixture", ["board_jobs_malformed.json", "board_jobs_changed_template.json"])
    def test_an_unrecognized_shape_is_a_typed_probe_failure(self, fixture):
        outcome = _parse(_adapter(), fixture, kind=AdapterTaskKind.HEALTH)
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.observations == ()
        assert outcome.evidence_refs


# --------------------------------------------------------------------------
# ACQ-09 failure traceability
# --------------------------------------------------------------------------


class TestFailureTraceability:
    @pytest.mark.parametrize(
        "fixture,kind",
        [
            ("board_jobs_changed_template.json", AdapterTaskKind.ENUMERATE),
            ("board_jobs_malformed.json", AdapterTaskKind.ENUMERATE),
            ("board_jobs_missing_required_fields.json", AdapterTaskKind.ENUMERATE),
            ("board_jobs_changed_template.json", AdapterTaskKind.HEALTH),
            ("board_jobs_malformed.json", AdapterTaskKind.HEALTH),
        ],
    )
    def test_every_failure_outcome_carries_durable_evidence_refs(self, fixture, kind):
        outcome = _parse(_adapter(), fixture, kind=kind)
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.evidence_refs, f"{fixture} failure dropped its ACQ-09 refs"
        assert outcome.failure is not None
        assert outcome.failure.adapter_id == ADAPTER_ID
        assert outcome.failure.retryable is False


# --------------------------------------------------------------------------
# Cursor: a single-document API never has one
# --------------------------------------------------------------------------


class TestCursor:
    @pytest.mark.parametrize(
        "fixture", ["board_jobs.json", "board_jobs_api_v2.json", "board_jobs_rejected_member.json"]
    )
    def test_next_cursor_is_always_none(self, fixture):
        """02 §19: there is no next page to propose; a bounded PARTIAL must
        never be mistaken for a terminal cursor either."""
        adapter = _adapter()
        outcome = _parse(adapter, fixture)
        assert outcome.continuation_required is False
        assert adapter.next_cursor(_task(), outcome, None, ctx=_ctx()) is None

    def test_next_cursor_is_none_for_terminal_and_probe_outcomes(self):
        adapter = _adapter()
        empty = _parse(adapter, "board_jobs_empty.json", page_class=PageClass.EMPTY)
        assert adapter.next_cursor(_task(), empty, None, ctx=_ctx()) is None
        probe = _parse(adapter, "board_jobs.json", kind=AdapterTaskKind.HEALTH)
        assert adapter.next_cursor(_task(AdapterTaskKind.HEALTH), probe, None, ctx=None) is None

    def test_a_failure_proposes_no_cursor(self):
        adapter = _adapter()
        outcome = _parse(adapter, "board_jobs_changed_template.json")
        assert adapter.next_cursor(_task(), outcome, None, ctx=_ctx()) is None


# --------------------------------------------------------------------------
# Purity: plan/parse only
# --------------------------------------------------------------------------


class TestPurity:
    def test_parsing_never_touches_a_connection(self):
        """A parser receives no connection and returns data only; passing a
        sentinel object as the context must not change the outcome."""
        outcome = _adapter().parse(_task(), _validated("board_jobs.json"), ctx=object())
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS

    def test_planning_never_reads_content_supplied_urls(self):
        """A board, host or URL found in a task payload (i.e. originating
        from scraped content) can never re-point a plan (ACQ-04)."""
        task = AdapterTask(
            kind=AdapterTaskKind.ENUMERATE,
            payload={
                "board": "evil",
                "url": "http://evil.test/steal",
                "api_base_url": "http://evil.test",
            },
        )
        assert _adapter().plan(task, None, ctx=None).url == LIST_URL

    def test_no_provider_host_literal_lives_in_the_adapter(self):
        from jobscraper.acquisition.atsendpoints import ATS_ENDPOINT_SPECS
        import jobscraper.adapters.ashby as module

        text = Path(module.__file__).read_text(encoding="utf-8")
        for spec in ATS_ENDPOINT_SPECS:
            for host in spec.hosts():
                assert f'"{host}"' not in text and f"'{host}'" not in text

    def test_no_io_or_storage_imports_live_in_the_adapter(self):
        import jobscraper.adapters.ashby as module

        text = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "import urllib", "from urllib", "import sqlite3", "import socket",
            "import requests", "import httpx", "urlopen", "urlretrieve",
        ):
            assert forbidden not in text

    def test_fixtures_are_deterministic(self):
        payload = _fixture_json("board_jobs.json")
        assert isinstance(payload, dict) and len(payload["jobs"]) == 3
        assert _fixture("board_jobs.json") == _fixture("board_jobs.json")
        assert _fixture_json("board_jobs_empty.json")["jobs"] == []
