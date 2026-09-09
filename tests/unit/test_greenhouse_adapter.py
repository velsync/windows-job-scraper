"""S2.5 unit tests: the built-in Greenhouse board-API code adapter.

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md
(§9 manifest/permissions, §12.3 provider priorities, §19 pagination/stop
policy, §22 required-field discipline, §26 fixture corpus, §27 typed
failures, §31/§32 URL + origin evidence, ACQ-02 protocol and request-type
mapping, ACQ-03 parse outcomes, ACQ-04 detail-child planning, ACQ-09
versioned contracts); docs/plans/slice-2-worker-implementation-plan-v0313.md
S2.5.

Proves, against the deterministic sanitized corpus in
``tests/fixtures/greenhouse/``:

* planning is built only from the **pinned** board token and a validated
  source-native job id — never from a URL found in page content;
* the ACQ-02 request-type↔task mapping is complete and explicit;
* required-field failures reject with structured evidence and never invent
  values; a changed template is ``PARSE_MARKER_MISSING``, never
  ``SUCCESS_EMPTY``;
* detail child work is typed (ACQ-04), bounded, and refused for a response
  that does not describe the requested job;
* hostile content (``javascript:``/``data:`` URLs, traversal ids) never
  becomes a fetch target, a canonical URL candidate or an observation;
* the adapter stays pure: no I/O, no database, no policy authority.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobscraper.acquisition.failures import FailureKind
from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    ParseOutcomeKind,
    PlanningContext,
    StopPolicy,
    ValidatedResultEnvelope,
    task_kind_for_request_type,
    validate_manifest,
)
from jobscraper.adapters.greenhouse import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    MANIFEST,
    GreenhouseAdapter,
    GreenhouseConfig,
)
from jobscraper.adapters.registry import BUILTIN_ADAPTERS, build_adapter, get_adapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "greenhouse"

BOARD = "acme"
LIST_URL = f"https://boards-api.greenhouse.io/v1/boards/{BOARD}/jobs"
CONFIG = {"board": BOARD, "company_name": "Acme Fixtures"}


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_json(name: str):
    return json.loads(_fixture(name).decode("utf-8"))


def _adapter(**overrides) -> GreenhouseAdapter:
    return GreenhouseAdapter(GreenhouseConfig(**{**CONFIG, **overrides}))


def _validated(
    fixture: str,
    *,
    page_class: PageClass = PageClass.VALID_LIST,
    status: int = 200,
    content_type: str = "application/json",
    url: str = LIST_URL,
    task_kind: AdapterTaskKind = AdapterTaskKind.ENUMERATE,
) -> ValidatedResultEnvelope:
    envelope = ResultEnvelope(
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
        body=_fixture(fixture),
    ).finalize()
    return ValidatedResultEnvelope(
        envelope=envelope,
        page_class=page_class,
        validation_evidence={"fixture": fixture, "task": task_kind.value},
    )


def _task(kind=AdapterTaskKind.ENUMERATE, payload=None) -> AdapterTask:
    return AdapterTask(kind=kind, payload=dict(payload or {}))


def _parse(adapter, fixture, **kwargs):
    kind = kwargs.pop("task_kind", AdapterTaskKind.ENUMERATE)
    return adapter.parse(
        _task(kind, kwargs.pop("payload", None)),
        _validated(fixture, task_kind=kind, **kwargs),
        ctx=None,
    )


# --------------------------------------------------------------------------
# Manifest, registry and declared contract
# --------------------------------------------------------------------------


class TestManifestAndRegistry:
    def test_manifest_is_schema_valid_and_http_only(self):
        from dataclasses import asdict

        manifest = validate_manifest(asdict(MANIFEST))
        assert manifest.id == "greenhouse"
        assert manifest.version == ADAPTER_VERSION
        assert set(manifest.supported_execution_classes) == {"HTTP"}
        assert set(manifest.supported_auth_modes) == {"NONE"}
        assert {"listing_parse", "detail_parse"} <= set(manifest.capabilities)
        assert manifest.cost_class == "LIGHT"

    def test_registered_as_a_builtin_adapter(self):
        assert BUILTIN_ADAPTERS["greenhouse"] is GreenhouseAdapter
        assert get_adapter("greenhouse") is GreenhouseAdapter
        adapter = build_adapter("greenhouse", CONFIG)
        assert isinstance(adapter, GreenhouseAdapter)
        assert adapter.manifest.id == "greenhouse"

    def test_build_adapter_refuses_an_unregistered_id(self):
        with pytest.raises(KeyError):
            build_adapter("greenhouse_pro", CONFIG)

    def test_build_adapter_refuses_unknown_config_keys(self):
        with pytest.raises(ValueError):
            build_adapter("greenhouse", {**CONFIG, "rotate_proxy_on_block": True})

    def test_listing_identity_is_declared_sufficient(self):
        """03 §40: the coverage barrier may prove listing presence without
        detail completion only when the binding contract declares it."""
        assert GreenhouseAdapter.listing_identity_sufficient is True

    def test_stop_policy_is_declared_and_bounded(self):
        policy = GreenhouseAdapter.stop_policy
        assert isinstance(policy, StopPolicy)
        assert policy.max_pages == 1  # the board API is not paginated
        assert 1 <= policy.max_requests <= 200
        assert policy.max_runtime_s > 0


# --------------------------------------------------------------------------
# Config validation (typed, fail closed)
# --------------------------------------------------------------------------


class TestConfig:
    def test_board_is_required(self):
        with pytest.raises(TypeError):
            GreenhouseConfig(company_name="Acme")  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            GreenhouseConfig(board="")
        with pytest.raises(ValueError):
            GreenhouseConfig(board=None)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "board",
        ["../acme", "acme/jobs", "acme?content=true", "acme#x", "boards", "v1", "jobs",
         "ac me", "acme.", "-acme", "acme\n", "https://evil.test/acme"],
    )
    def test_unsafe_board_tokens_are_refused(self, board):
        with pytest.raises(ValueError):
            GreenhouseConfig(board=board)

    def test_board_token_is_lowercased_deterministically(self):
        assert GreenhouseConfig(board="AcMe").board == "acme"

    @pytest.mark.parametrize(
        "base",
        [
            "ftp://boards-api.greenhouse.io",
            "https://user:pass@boards-api.greenhouse.io",
            "https://boards-api.greenhouse.io/v1",
            "https://boards-api.greenhouse.io/?x=1",
            "https://boards-api.greenhouse.io/#frag",
            "boards-api.greenhouse.io",
            "javascript:alert(1)",
            "",
        ],
    )
    def test_api_base_must_be_a_bare_http_origin(self, base):
        with pytest.raises(ValueError):
            GreenhouseConfig(board=BOARD, api_base_url=base)

    def test_default_api_base_is_the_provider_api_origin(self):
        config = GreenhouseConfig(board=BOARD)
        assert config.api_base_url == "https://boards-api.greenhouse.io"

    def test_timeout_and_byte_caps_cannot_exceed_host_policy(self):
        with pytest.raises(ValueError):
            GreenhouseConfig(board=BOARD, timeout_s=31.0)
        with pytest.raises(ValueError):
            GreenhouseConfig(board=BOARD, max_bytes=2_000_001)

    @pytest.mark.parametrize("count", [-1, 201])
    def test_detail_budget_is_bounded(self, count):
        with pytest.raises(ValueError):
            GreenhouseConfig(board=BOARD, max_detail_requests=count)

    def test_detail_fetch_can_be_disabled(self):
        assert GreenhouseConfig(board=BOARD, detail_fetch=False).detail_fetch is False


# --------------------------------------------------------------------------
# Planning: pinned board token only
# --------------------------------------------------------------------------


class TestPlanning:
    def test_enumerate_plans_the_public_board_endpoint(self):
        plan = _adapter().plan(_task(), None, ctx=PlanningContext())
        assert plan.method == "GET"
        assert plan.url == LIST_URL
        assert plan.headers["Accept"] == "application/json"
        assert plan.expected_content_types == ("application/json",)
        assert plan.purpose == "ENUMERATE"

    def test_inline_content_is_an_explicit_binding_choice(self):
        plan = _adapter(include_content=True).plan(_task(), None, ctx=None)
        assert plan.url == f"{LIST_URL}?content=true"

    def test_pinned_api_base_is_used_verbatim(self):
        adapter = _adapter(api_base_url="http://127.0.0.1:8123")
        assert adapter.plan(_task(), None, ctx=None).url == (
            "http://127.0.0.1:8123/v1/boards/acme/jobs"
        )

    def test_detail_url_is_built_from_the_pinned_board_and_a_validated_id(self):
        plan = _adapter().plan(
            _task(AdapterTaskKind.DETAIL, {"target_reference": "4001"}), None, ctx=None
        )
        assert plan.url == f"{LIST_URL}/4001"
        assert plan.purpose == "DETAIL"

    @pytest.mark.parametrize(
        "reference",
        ["../../etc/passwd", "4001/../../x", "4001?x=1", "4001#f", "", "   ", None,
         "abc", "4001 ", "https://evil.test/4001", 4001.5, True, ["4001"]],
    )
    def test_detail_planning_refuses_an_unusable_job_reference(self, reference):
        with pytest.raises(ValueError):
            _adapter().plan(
                _task(AdapterTaskKind.DETAIL, {"target_reference": reference}),
                None,
                ctx=None,
            )

    def test_detail_planning_ignores_content_supplied_targets(self):
        """A discovered reference is not I/O permission (ACQ-04): anything but
        the source-native id in the task payload is ignored, so scraped
        content can never re-point a detail fetch."""
        plan = _adapter().plan(
            _task(
                AdapterTaskKind.DETAIL,
                {
                    "target_reference": "4001",
                    "board": "evil",
                    "url": "http://evil.test/steal",
                    "api_base_url": "http://evil.test",
                    "absolute_url": "http://evil.test/4001",
                },
            ),
            None,
            ctx=None,
        )
        assert plan.url == f"{LIST_URL}/4001"

    def test_health_and_smoke_plan_the_board_endpoint(self):
        adapter = _adapter()
        for kind in (AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE):
            plan = adapter.plan(_task(kind), None, ctx=None)
            assert plan.url == LIST_URL

    @pytest.mark.parametrize("kind", [AdapterTaskKind.CRAWL, AdapterTaskKind.DISCOVER])
    def test_unsupported_tasks_are_refused_explicitly(self, kind):
        with pytest.raises(ValueError):
            _adapter().plan(_task(kind), None, ctx=None)


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

    @pytest.mark.parametrize(
        "request_type", ["NORMALIZE", "RECONCILE", "ENRICH", "ELIGIBILITY", "SCORE", "EXPORT"]
    )
    def test_host_native_pipeline_tasks_are_not_adapter_tasks(self, request_type):
        assert task_kind_for_request_type(request_type) is None

    def test_unknown_request_type_is_refused(self):
        with pytest.raises(ValueError):
            task_kind_for_request_type("FETCH_EVERYTHING")

    def test_mapping_covers_every_acquisition_request_type(self):
        from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES

        assert {
            request_type
            for request_type in ACQUISITION_REQUEST_TYPES
            if task_kind_for_request_type(request_type) is not None
        } == set(ACQUISITION_REQUEST_TYPES)


# --------------------------------------------------------------------------
# ENUMERATE parsing
# --------------------------------------------------------------------------


class TestListParse:
    def test_valid_list_produces_one_observation_per_job(self):
        outcome = _parse(_adapter(), "board_list.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert [o.source_job_id for o in outcome.observations] == ["4001", "4002", "4003"]
        assert [o.fields["title"] for o in outcome.observations] == [
            "Backend Engineer",
            "Platform Engineer",
            "Senior Data Engineer",
        ]
        assert outcome.failure is None

    def test_observation_carries_source_native_identity_and_rank(self):
        outcome = _parse(_adapter(), "board_list.json")
        first = outcome.observations[0]
        assert first.fields["source_job_id"] == "4001"
        assert first.source_rank_or_order == 0
        assert first.raw_url == LIST_URL

    def test_application_url_is_derived_from_the_endpoint_table_not_content(self):
        outcome = _parse(_adapter(), "board_list.json")
        assert [o.application_url_candidate for o in outcome.observations] == [
            "https://boards.greenhouse.io/acme/jobs/4001",
            "https://boards.greenhouse.io/acme/jobs/4002",
            "https://boards.greenhouse.io/acme/jobs/4003",
        ]

    def test_absolute_url_is_preserved_raw_as_the_canonical_candidate(self):
        """02 §31: keep the raw URL; tracking cleanup is the resolver's job."""
        outcome = _parse(_adapter(), "board_list.json")
        assert outcome.observations[1].canonical_url_candidate == (
            "https://boards.greenhouse.io/acme/jobs/4002?utm_source=board_mailer"
        )

    def test_company_comes_from_the_pinned_binding_config(self):
        outcome = _parse(_adapter(), "board_list.json")
        assert outcome.observations[0].fields["company"] == "Acme Fixtures"
        locators = {
            (e.field_name, e.locator_kind)
            for e in outcome.observations[0].field_evidence
        }
        assert ("company", "binding_config") in locators

    def test_without_a_declared_company_no_company_is_invented(self):
        adapter = GreenhouseAdapter(GreenhouseConfig(board=BOARD))
        outcome = _parse(adapter, "board_list.json")
        assert "company" not in outcome.observations[0].fields

    def test_a_last_modified_stamp_is_not_claimed_as_a_posted_time(self):
        """The board listing only carries ``updated_at``; that is not posted_at.

        Guessing here would be sticky: a presence keeps the first value it was
        given, so a listing-derived guess would shadow the provider's real
        ``first_published_at`` when the detail pass arrives.
        """
        outcome = _parse(_adapter(), "board_list.json")
        for observation in outcome.observations:
            assert "posted_at" not in observation.fields
            assert not any(
                e.field_name == "posted_at" for e in observation.field_evidence
            )


    def test_departments_and_offices_are_kept_as_evidence(self):
        outcome = _parse(_adapter(), "board_list.json")
        fields = outcome.observations[1].fields
        assert fields["departments"] == ["Infrastructure"]
        assert fields["offices"] == ["Berlin", "Paris"]
        assert fields["requisition_id"] == "REQ-4002"

    def test_every_extracted_field_has_locator_evidence(self):
        outcome = _parse(_adapter(), "board_list.json")
        for observation in outcome.observations:
            names = {e.field_name for e in observation.field_evidence}
            assert {"source_job_id", "title"} <= names
            for evidence in observation.field_evidence:
                assert evidence.locator_kind in {"json_path", "binding_config"}
                assert evidence.locator_value
                assert len(evidence.value_hash) == 64

    def test_detail_child_tasks_are_typed_bounded_and_lower_priority(self):
        outcome = _parse(_adapter(), "board_list.json")
        assert [t.kind for t in outcome.discovered_tasks] == ["DETAIL"] * 3
        assert [t.target_reference for t in outcome.discovered_tasks] == [
            "4001",
            "4002",
            "4003",
        ]
        assert {t.depth for t in outcome.discovered_tasks} == {1}
        # enumeration pages must be claimable before their own child work
        assert all(t.priority < 0 for t in outcome.discovered_tasks)
        assert len({t.logical_key for t in outcome.discovered_tasks}) == 3

    def test_detail_budget_caps_child_work_without_losing_listing_evidence(self):
        """The declared stop policy bounds enrichment, never the enumeration:
        listing identity stays authoritative (03 §40) and the bound is
        recorded instead of silently dropping work."""
        outcome = _parse(_adapter(max_detail_requests=2), "board_list.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert outcome.continuation_required is False
        assert len(outcome.discovered_tasks) == 2
        assert len(outcome.observations) == 3
        assert outcome.coverage_proposal == {
            "declared_total": 3,
            "observed": 3,
            "detail_tasks": 2,
            "rejected_members": 0,
        }
        assert any(
            item.get("reason") == "DETAIL_BUDGET_REACHED" for item in outcome.review_evidence
        )

    def test_inline_content_suppresses_child_work(self):
        outcome = _parse(
            _adapter(include_content=True),
            "board_list_with_content.json",
        )
        assert outcome.discovered_tasks == ()
        assert outcome.observations[0].fields["description"].startswith(
            '<div class="job__description--opening">'
        )

    def test_child_work_is_emitted_per_item_when_inline_content_is_absent(self):
        outcome = _parse(_adapter(include_content=True), "board_list.json")
        assert [t.target_reference for t in outcome.discovered_tasks] == [
            "4001",
            "4002",
            "4003",
        ]
        assert "description" not in outcome.observations[0].fields

    def test_empty_board_is_a_recognized_success_not_a_failure(self):
        outcome = _parse(_adapter(), "board_list_empty.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
        assert outcome.observations == ()
        assert outcome.discovered_tasks == ()
        assert outcome.failure is None

    def test_truncated_response_is_bounded_partial_without_coverage_authority(self):
        outcome = _parse(_adapter(), "board_list_truncated.json")
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        assert outcome.continuation_required is True
        assert len(outcome.observations) == 2
        assert outcome.failure is None
        assert outcome.coverage_proposal == {
            "declared_total": 5,
            "observed": 2,
            "detail_tasks": 2,
            "rejected_members": 0,
        }

    def test_changed_template_is_parse_marker_missing_not_success_empty(self):
        outcome = _parse(_adapter(), "board_list_changed_template.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.observations == ()
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.failure.retryable is False
        assert outcome.failure.source_health_impact == "DEGRADED"

    def test_required_field_failures_reject_items_with_evidence(self):
        outcome = _parse(_adapter(), "board_list_missing_required_fields.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.observations == ()
        assert outcome.discovered_tasks == ()
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        reasons = " ".join(json.dumps(item) for item in outcome.review_evidence)
        assert "id" in reasons and "title" in reasons
        assert len(outcome.review_evidence) == 3

    def test_a_traversal_id_never_becomes_a_fetch_target(self):
        outcome = _parse(_adapter(), "board_list_missing_required_fields.json")
        assert outcome.discovered_tasks == ()
        for observation in outcome.observations:
            assert (observation.source_job_id or "").isdigit()
        # the refused value survives as bounded review evidence only
        assert any("passwd" in json.dumps(item) for item in outcome.review_evidence)

    def test_unparseable_body_is_a_typed_parse_failure(self):
        # wrapped as VALID_LIST on purpose: this pins the *parser's* own
        # refusal, the classifier gate for the same bytes is pinned in the
        # E2E suite (UNEXPECTED_CONTENT never reaches a normal parser)
        outcome = _parse(_adapter(), "board_list_malformed.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.observations == ()

    def test_a_json_array_is_not_a_board_response(self):
        envelope = ResultEnvelope(
            execution_plan_id="plan-1", request_id="req-1", attempt_id="att-1",
            run_source_plan_id="rsp-1", source_id="src-1", binding_id="bnd-1",
            binding_revision_id="bndrev-1", adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION, strategy="PROVIDER_NATIVE",
            execution_class="HTTP", requested_url=LIST_URL, final_url=LIST_URL,
            status_code=200, content_type="application/json", body=b"[1, 2, 3]",
        ).finalize()
        outcome = _adapter().parse(
            _task(),
            ValidatedResultEnvelope(envelope=envelope, page_class=PageClass.VALID_LIST),
            ctx=None,
        )
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING

    def test_outcome_carries_acq09_evidence_references(self):
        """S2.1 recorded these fields as accepted-but-unproduced; the provider
        parser is their first producer."""
        outcome = _parse(_adapter(), "board_list.json")
        assert outcome.contract_version == 2
        assert outcome.evidence_refs
        assert all(ref.startswith("result://") or ref.startswith("validity://")
                   for ref in outcome.evidence_refs)


# --------------------------------------------------------------------------
# DETAIL parsing
# --------------------------------------------------------------------------


class TestDetailParse:
    DETAIL_URL = f"{LIST_URL}/4001"

    def _detail(self, adapter, fixture, reference="4001", **kwargs):
        url = kwargs.pop("url", f"{LIST_URL}/{reference}")
        return adapter.parse(
            _task(AdapterTaskKind.DETAIL, {"target_reference": reference}),
            _validated(
                fixture,
                page_class=PageClass.VALID_JOB,
                url=url,
                task_kind=AdapterTaskKind.DETAIL,
                **kwargs,
            ),
            ctx=None,
        )

    def test_detail_parse_produces_one_enriched_observation(self):
        outcome = self._detail(_adapter(), "job_detail.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert len(outcome.observations) == 1
        observation = outcome.observations[0]
        assert observation.source_job_id == "4001"
        assert observation.fields["title"] == "Backend Engineer"
        assert "Backend Engineer" in observation.fields["description"]
        assert observation.fields["posted_at"] == "2026-08-18T06:00:00.000000Z"
        assert observation.fields["employment_type"] == "Full time"
        assert observation.fields["departments"] == ["Engineering"]

    def test_publication_stamp_is_utc_rfc3339_and_names_its_locator(self):
        outcome = self._detail(_adapter(), "job_detail.json")
        observation = outcome.observations[0]
        assert observation.fields["posted_at"] == "2026-08-18T06:00:00.000000Z"
        locators = {
            e.locator_value for e in observation.field_evidence
            if e.field_name == "posted_at"
        }
        assert locators == {"first_published_at"}

    def test_date_published_is_the_fallback_when_no_first_published_at(self):
        outcome = self._detail(
            _adapter(), "job_detail_multi_location.json", reference="4002"
        )
        observation = outcome.observations[0]
        assert observation.fields["posted_at"] == "2026-08-30T06:00:00.000000Z"
        locators = {
            e.locator_value for e in observation.field_evidence
            if e.field_name == "posted_at"
        }
        assert locators == {"job_post_information.date_published"}

    def test_detail_parse_emits_no_further_child_work(self):
        outcome = self._detail(_adapter(), "job_detail.json")
        assert outcome.discovered_tasks == ()

    def test_structured_admin_locations_win_over_composed_text(self):
        outcome = self._detail(_adapter(), "job_detail.json")
        locations = outcome.observations[0].fields["locations"]
        assert locations == [
            {
                "city": "Berlin",
                "state": "Berlin",
                "country": "Germany",
                "location_type": "Employee - Onsite",
            }
        ]

    def test_multi_location_detail_keeps_the_whole_set(self):
        outcome = self._detail(
            _adapter(), "job_detail_multi_location.json", reference="4002"
        )
        fields = outcome.observations[0].fields
        assert [item["city"] for item in fields["locations"]] == ["Berlin", "Paris"]
        assert fields["applicant_location_requirements"] == [
            {"location_type": "Employee - Remote", "country": "Germany"},
            {"location_type": "Employee - Remote", "country": "France"},
        ]

    def test_remote_worldwide_requirement_is_passed_through_explicitly(self):
        outcome = self._detail(
            _adapter(), "job_detail_remote_hostile_links.json", reference="4003"
        )
        fields = outcome.observations[0].fields
        assert fields["applicant_location_requirements"] == [
            {"location_type": "Remote - Worldwide"}
        ]
        assert fields["locations"] == ["Remote"]

    def test_active_scheme_urls_are_refused_as_evidence_but_never_fatal(self):
        outcome = self._detail(
            _adapter(), "job_detail_remote_hostile_links.json", reference="4003"
        )
        observation = outcome.observations[0]
        assert observation.canonical_url_candidate is None
        # the safe apply link is derived from the endpoint table, not content
        assert observation.application_url_candidate == (
            "https://boards.greenhouse.io/acme/jobs/4003"
        )
        assert any(
            "javascript" in json.dumps(item).lower() for item in outcome.review_evidence
        )

    def test_a_response_about_another_job_cannot_mint_an_observation(self):
        outcome = self._detail(_adapter(), "job_detail_id_mismatch.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.observations == ()
        assert outcome.failure.kind is FailureKind.INVALID_JOB_RECORD
        assert outcome.closure_or_missing_evidence
        evidence = outcome.closure_or_missing_evidence[0]
        assert evidence["reason"] == "DETAIL_ID_MISMATCH"
        assert evidence["target_reference"] == "4001"
        assert evidence["observed_id"] == "9999"

    def test_a_detail_without_required_fields_reports_missing_evidence(self):
        envelope_body = json.dumps({"id": 4001, "location": {"name": "Berlin"}}).encode()
        envelope = ResultEnvelope(
            execution_plan_id="plan-1", request_id="req-1", attempt_id="att-1",
            run_source_plan_id="rsp-1", source_id="src-1", binding_id="bnd-1",
            binding_revision_id="bndrev-1", adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION, strategy="PROVIDER_NATIVE",
            execution_class="HTTP", requested_url=self.DETAIL_URL,
            final_url=self.DETAIL_URL, status_code=200,
            content_type="application/json", body=envelope_body,
        ).finalize()
        outcome = _adapter().parse(
            _task(AdapterTaskKind.DETAIL, {"target_reference": "4001"}),
            ValidatedResultEnvelope(
                envelope=envelope, page_class=PageClass.VALID_JOB
            ),
            ctx=None,
        )
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.observations == ()
        assert outcome.closure_or_missing_evidence
        assert outcome.closure_or_missing_evidence[0]["reason"] == "REQUIRED_FIELD_MISSING"

    def test_health_and_smoke_parses_never_emit_observations(self):
        adapter = _adapter()
        for kind in (AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE):
            outcome = adapter.parse(
                _task(kind),
                _validated("board_list.json", task_kind=kind),
                ctx=None,
            )
            assert outcome.observations == ()
            assert outcome.discovered_tasks == ()
            assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY


# --------------------------------------------------------------------------
# Cursor behaviour (02 §19)
# --------------------------------------------------------------------------


class TestCursor:
    def test_the_board_endpoint_is_not_paginated(self):
        adapter = _adapter()
        outcome = _parse(adapter, "board_list.json")
        assert adapter.next_cursor(_task(), outcome, None, ctx=None) is None

    def test_empty_and_failure_never_advance_a_cursor(self):
        adapter = _adapter()
        for fixture in ("board_list_empty.json", "board_list_changed_template.json"):
            outcome = _parse(adapter, fixture)
            assert adapter.next_cursor(_task(), outcome, None, ctx=None) is None

    def test_a_truncated_partial_does_not_claim_a_terminal_cursor(self):
        adapter = _adapter()
        outcome = _parse(adapter, "board_list_truncated.json")
        assert outcome.continuation_required is True
        assert adapter.next_cursor(_task(), outcome, None, ctx=None) is None


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


class TestPurity:
    def test_parsing_never_touches_a_connection(self):
        """A parser receives no connection and returns data only; passing a
        sentinel object as the context must not change the outcome."""
        outcome = _adapter().parse(
            _task(),
            _validated("board_list.json"),
            ctx=object(),
        )
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS

    def test_planning_uses_the_planning_context_read_only(self):
        ctx = PlanningContext(
            run_id="run-1",
            run_source_plan_id="rsp-1",
            binding_revision_id="bndrev-1",
            permission_profile_revision=1,
        )
        plan = _adapter().plan(_task(), None, ctx=ctx)
        assert plan.url == LIST_URL

    def test_fixtures_are_deterministic(self):
        payload = _fixture_json("board_list.json")
        assert payload["meta"]["total"] == len(payload["jobs"]) == 3
        assert _fixture("board_list.json") == _fixture("board_list.json")
