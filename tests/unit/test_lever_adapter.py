"""S2.6 unit tests: the built-in Lever postings-API code adapter.

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md
(§9 manifest/permissions, §12.3 provider priorities, §19 pagination/stop
policy, §22 required-field discipline, §26 fixture corpus, §27 typed
failures, §31/§32 URL + origin evidence, ACQ-02 protocol and request-type
mapping, ACQ-03 parse outcomes, ACQ-04 detail-child planning, ACQ-09
versioned contracts); docs/plans/slice-2-worker-implementation-plan-v0313.md
S2.6 ("same shape as S2.5", Lever semantics).

Proves, against the deterministic sanitized corpus in
``tests/fixtures/lever/``:

* planning is built only from the **pinned** site token and a validated
  source-native posting id — never from a URL found in page content;
* Lever's bare-array, no-declared-total, ``skip``/``limit`` shape is handled
  honestly: a short page is terminal, a full page is ``continuation_required``
  with a bounded offset cursor scoped to the run source plan, a repeated
  page is a trap, and an empty page is ``SUCCESS_EMPTY``;
* required-field failures reject with structured evidence and never invent
  values; a changed template is ``PARSE_MARKER_MISSING``, never
  ``SUCCESS_EMPTY``;
* ``createdAt`` is evidence, not a claimed publication time; ``workplaceType``
  ``remote`` is the documented remote side-channel; ``hostedUrl``/``applyUrl``
  are accepted only on reviewed hosted hosts;
* detail child work is typed (ACQ-04), bounded, and refused for a response
  that does not describe the requested posting;
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
    CrawlCursor,
    ParseOutcomeKind,
    PlanningContext,
    StopPolicy,
    ValidatedResultEnvelope,
    task_kind_for_request_type,
    validate_manifest,
)
from jobscraper.adapters.lever import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    MANIFEST,
    STOP_POLICY,
    LeverAdapter,
    LeverConfig,
)
from jobscraper.adapters.registry import BUILTIN_ADAPTERS, build_adapter, get_adapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "lever"

BOARD = "acme"
API = "https://api.lever.co"
LIST_PATH = f"/v0/postings/{BOARD}"
LIST_URL = f"{API}{LIST_PATH}?mode=json&skip=0&limit=100"
CONFIG = {"board": BOARD, "company_name": "Acme Fixtures"}

ID1 = "1a2b3c4d-0000-4000-8000-000000004001"
ID2 = "1a2b3c4d-0000-4000-8000-000000004002"
ID3 = "1a2b3c4d-0000-4000-8000-000000004003"
OTHER = "9e9f9a9b-0000-4000-8000-000000009999"
HOSTED = "https://jobs.lever.co/acme"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_json(name: str):
    return json.loads(_fixture(name).decode("utf-8"))


def _adapter(**overrides) -> LeverAdapter:
    return LeverAdapter(LeverConfig(**{**CONFIG, **overrides}))


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
    task_kind: AdapterTaskKind = AdapterTaskKind.ENUMERATE,
) -> ValidatedResultEnvelope:
    return ValidatedResultEnvelope(
        envelope=_envelope(_fixture(fixture), url=url, status=status, content_type=content_type),
        page_class=page_class,
        validation_evidence={"fixture": fixture, "task": task_kind.value},
    )


def _validated_payload(payload, *, url: str = LIST_URL,
                       page_class: PageClass = PageClass.VALID_LIST) -> ValidatedResultEnvelope:
    return ValidatedResultEnvelope(
        envelope=_envelope(json.dumps(payload).encode("utf-8"), url=url),
        page_class=page_class,
        validation_evidence={"fixture": "inline"},
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


def _posting(index: int, **overrides) -> dict:
    """One synthetic, well-formed posting for generated paging payloads."""
    posting_id = f"aaaaaaaa-0000-4000-8000-{index:012d}"
    item = {
        "id": posting_id,
        "text": f"Role {index}",
        "categories": {"location": "Berlin, Germany", "commitment": "Full-time",
                       "team": "Engineering", "allLocations": ["Berlin, Germany"]},
        "createdAt": 1786698000000,
        "workplaceType": "on-site",
        "hostedUrl": f"{HOSTED}/{posting_id}",
        "applyUrl": f"{HOSTED}/{posting_id}/apply",
        "description": f"<p>Body {index}</p>",
        "lists": [],
        "additional": "",
    }
    item.update(overrides)
    return item


def _page(count: int, start: int = 0) -> list[dict]:
    return [_posting(index) for index in range(start, start + count)]


def _ctx(plan: str = "rsp-1") -> PlanningContext:
    return PlanningContext(run_id="run-1", run_source_plan_id=plan,
                           binding_revision_id="bndrev-1", permission_profile_revision=1)


# --------------------------------------------------------------------------
# Manifest, registry and declared contract
# --------------------------------------------------------------------------


class TestManifestAndRegistry:
    def test_manifest_is_schema_valid_and_http_only(self):
        from dataclasses import asdict

        manifest = validate_manifest(asdict(MANIFEST))
        assert manifest.id == "lever"
        assert manifest.version == "1.0.0"
        assert manifest.supported_execution_classes == ("HTTP",)
        assert manifest.supported_auth_modes == ("NONE",)
        assert manifest.cost_class == "LIGHT"
        assert {"listing_parse", "detail_parse", "health", "smoke"} <= set(manifest.capabilities)
        # discovery is the generic binding's job; there is no incremental feed
        assert "discover" not in manifest.capabilities
        assert "incremental" not in manifest.capabilities

    def test_registered_as_a_builtin_and_constructible_from_config(self):
        assert BUILTIN_ADAPTERS["lever"] is LeverAdapter
        assert get_adapter("lever") is LeverAdapter
        adapter = build_adapter("lever", CONFIG)
        assert isinstance(adapter, LeverAdapter)
        assert adapter.config.board == BOARD
        assert adapter.manifest.id == "lever"

    def test_registry_refuses_unknown_config_keys(self):
        with pytest.raises(ValueError):
            build_adapter("lever", {**CONFIG, "follow_redirect_hosts": ["evil.test"]})
        with pytest.raises(ValueError):
            build_adapter("lever", {**CONFIG, "api_hosts": ["evil.test"]})

    def test_registry_refuses_a_non_mapping_config(self):
        with pytest.raises((ValueError, TypeError)):
            LeverConfig.from_mapping("board=acme")  # type: ignore[arg-type]

    def test_router_advertises_only_provider_native_for_lever(self):
        from jobscraper.adapters.router import _IMPLEMENTED_STRATEGIES

        assert _IMPLEMENTED_STRATEGIES["lever"] == frozenset({"PROVIDER_NATIVE"})

    def test_stop_policy_is_declared_and_bounded(self):
        policy = LeverAdapter.stop_policy
        assert isinstance(policy, StopPolicy)
        assert policy is STOP_POLICY
        # offset paging is bounded by pages; well below the host cap of 50
        assert 1 <= policy.max_pages <= 50
        assert policy.max_duplicate_pages == 1
        assert 1 <= policy.max_requests <= 200
        assert policy.max_runtime_s > 0

    def test_listing_identity_is_declared_sufficient(self):
        assert LeverAdapter.listing_identity_sufficient is True

    def test_the_adapter_cannot_exceed_its_own_declared_request_budget(self):
        """S2.6 corrective: the stop policy is a promise the host bounds a run
        against.  A full walk (``max_pages`` pages, each spawning its maximum
        detail budget) must fit inside the declared ``max_requests``."""
        from jobscraper.adapters.lever import DEFAULT_MAX_DETAIL_REQUESTS, MAX_DETAIL_REQUESTS

        worst_case = STOP_POLICY.max_pages * (1 + MAX_DETAIL_REQUESTS)
        assert worst_case <= STOP_POLICY.max_requests
        assert DEFAULT_MAX_DETAIL_REQUESTS <= MAX_DETAIL_REQUESTS
        # and the config cap is that same bound, not a looser one
        with pytest.raises(ValueError):
            LeverConfig(board=BOARD, max_detail_requests=MAX_DETAIL_REQUESTS + 1)
        assert LeverConfig(board=BOARD, max_detail_requests=MAX_DETAIL_REQUESTS).max_detail_requests == MAX_DETAIL_REQUESTS


# --------------------------------------------------------------------------
# Config validation (typed, fail closed)
# --------------------------------------------------------------------------


class TestConfig:
    def test_board_is_required(self):
        with pytest.raises(TypeError):
            LeverConfig(company_name="Acme")  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            LeverConfig(board="")
        with pytest.raises(ValueError):
            LeverConfig(board=None)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "board",
        ["../acme", "acme/jobs", "acme?mode=json", "acme#x", "postings", "v0", "jobs",
         "apply", "ac me", "acme.", "-acme", "acme\n", "https://evil.test/acme"],
    )
    def test_unsafe_site_tokens_are_refused(self, board):
        with pytest.raises(ValueError):
            LeverConfig(board=board)

    def test_site_token_is_lowercased_deterministically(self):
        assert LeverConfig(board="AcMe").board == "acme"

    @pytest.mark.parametrize(
        "base",
        [
            "ftp://api.lever.co",
            "https://user:pass@api.lever.co",
            "https://api.lever.co/v0",
            "https://api.lever.co/?x=1",
            "https://api.lever.co/#frag",
            "api.lever.co",
            "javascript:alert(1)",
            "",
        ],
    )
    def test_api_base_must_be_a_bare_http_origin(self, base):
        with pytest.raises(ValueError):
            LeverConfig(board=BOARD, api_base_url=base)

    def test_default_api_base_is_the_provider_api_origin_from_the_endpoint_table(self):
        from jobscraper.acquisition.atsendpoints import spec_for_provider

        config = LeverConfig(board=BOARD)
        assert config.api_base_url == "https://api.lever.co"
        assert config.api_base_url.split("://", 1)[1] in spec_for_provider("LEVER").api_hosts

    def test_timeout_and_byte_caps_cannot_exceed_host_policy(self):
        with pytest.raises(ValueError):
            LeverConfig(board=BOARD, timeout_s=31.0)
        with pytest.raises(ValueError):
            LeverConfig(board=BOARD, max_bytes=2_000_001)

    @pytest.mark.parametrize("count", [-1, 201, True, 2.5])
    def test_detail_budget_is_bounded_and_typed(self, count):
        with pytest.raises(ValueError):
            LeverConfig(board=BOARD, max_detail_requests=count)

    @pytest.mark.parametrize("size", [0, 101, -5, True, "100"])
    def test_page_size_is_bounded_and_typed(self, size):
        with pytest.raises(ValueError):
            LeverConfig(board=BOARD, page_size=size)  # type: ignore[arg-type]

    def test_detail_fetch_can_be_disabled(self):
        assert LeverConfig(board=BOARD, detail_fetch=False).detail_fetch is False

    def test_careers_url_must_be_safe(self):
        with pytest.raises(ValueError):
            LeverConfig(board=BOARD, careers_url="javascript:alert(1)")
        assert LeverConfig(board=BOARD, careers_url="https://acme.example/careers").careers_url


# --------------------------------------------------------------------------
# Planning: pinned site token only
# --------------------------------------------------------------------------


class TestPlanning:
    def test_enumerate_plans_the_public_postings_endpoint_from_offset_zero(self):
        plan = _adapter().plan(_task(), None, ctx=_ctx())
        assert plan.method == "GET"
        assert plan.url == LIST_URL
        assert plan.headers["Accept"] == "application/json"
        assert plan.expected_content_types == ("application/json",)
        assert plan.purpose == "ENUMERATE"

    def test_page_size_is_a_binding_choice(self):
        plan = _adapter(page_size=25).plan(_task(), None, ctx=_ctx())
        assert plan.url == f"{API}{LIST_PATH}?mode=json&skip=0&limit=25"

    def test_pinned_api_base_is_used_verbatim(self):
        adapter = _adapter(api_base_url="http://127.0.0.1:8123")
        assert adapter.plan(_task(), None, ctx=_ctx()).url == (
            f"http://127.0.0.1:8123{LIST_PATH}?mode=json&skip=0&limit=100"
        )

    def test_detail_url_is_built_from_the_pinned_site_and_a_validated_id(self):
        plan = _adapter().plan(
            _task(AdapterTaskKind.DETAIL, {"target_reference": ID1}), None, ctx=None
        )
        assert plan.url == f"{API}{LIST_PATH}/{ID1}"
        assert plan.purpose == "DETAIL"

    @pytest.mark.parametrize(
        "reference",
        ["../../etc/passwd", f"{ID1}/../../x", f"{ID1}?x=1", f"{ID1}#f", "", "   ", None,
         "short", f"{ID1} ", f"https://evil.test/{ID1}", 4001, 4001.5, True, [ID1],
         ID1.upper(), "postings", "a" * 41],
    )
    def test_detail_planning_refuses_an_unusable_posting_reference(self, reference):
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
                    "target_reference": ID1,
                    "board": "evil",
                    "url": "http://evil.test/steal",
                    "api_base_url": "http://evil.test",
                    "hostedUrl": f"http://evil.test/{ID1}",
                    "applyUrl": f"http://evil.test/{ID1}/apply",
                },
            ),
            None,
            ctx=None,
        )
        assert plan.url == f"{API}{LIST_PATH}/{ID1}"

    def test_health_and_smoke_plan_the_first_postings_page(self):
        adapter = _adapter()
        for kind in (AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE):
            plan = adapter.plan(_task(kind), None, ctx=None)
            assert plan.url == LIST_URL

    @pytest.mark.parametrize("kind", [AdapterTaskKind.CRAWL, AdapterTaskKind.DISCOVER])
    def test_unsupported_tasks_are_refused_explicitly(self, kind):
        with pytest.raises(ValueError):
            _adapter().plan(_task(kind), None, ctx=None)

    def test_a_cursor_from_this_plan_advances_the_offset(self):
        cursor = CrawlCursor(
            source_id="src-1", binding_id="bnd-1", adapter_id="lever", adapter_version="1.0.0",
            cursor_schema_version=1,
            state_json=json.dumps({"plan": "rsp-1", "skip": 100, "page": 1, "ids_hash": "x"}),
            checkpoint_at="",
        )
        plan = _adapter().plan(_task(), cursor, ctx=_ctx("rsp-1"))
        assert plan.url == f"{API}{LIST_PATH}?mode=json&skip=100&limit=100"

    @pytest.mark.parametrize(
        "state",
        [
            {"plan": "rsp-OLD", "skip": 100, "page": 1},      # another run's offset
            {"plan": "rsp-1", "skip": -100, "page": 1},       # negative
            {"plan": "rsp-1", "skip": "100", "page": 1},      # wrong type
            {"plan": "rsp-1", "skip": True, "page": 1},       # bool
            {"skip": 100, "page": 1},                          # unscoped
            [100],                                             # not an object
        ],
    )
    def test_a_cursor_that_is_not_this_plans_restarts_the_full_enumeration(self, state):
        """Coverage is AUTHORITATIVE_FULL_SOURCE: a stale or foreign offset must
        never make a new run skip postings it has not seen."""
        cursor = CrawlCursor(
            source_id="src-1", binding_id="bnd-1", adapter_id="lever", adapter_version="1.0.0",
            cursor_schema_version=1, state_json=json.dumps(state), checkpoint_at="",
        )
        assert _adapter().plan(_task(), cursor, ctx=_ctx("rsp-1")).url == LIST_URL

    def test_a_cursor_of_another_adapter_or_schema_is_ignored(self):
        for adapter_id, schema in (("greenhouse", 1), ("lever", 2)):
            cursor = CrawlCursor(
                source_id="src-1", binding_id="bnd-1", adapter_id=adapter_id,
                adapter_version="1.0.0", cursor_schema_version=schema,
                state_json=json.dumps({"plan": "rsp-1", "skip": 100, "page": 1}),
                checkpoint_at="",
            )
            assert _adapter().plan(_task(), cursor, ctx=_ctx("rsp-1")).url == LIST_URL

    def test_unparseable_cursor_state_restarts_from_zero(self):
        cursor = CrawlCursor(
            source_id="src-1", binding_id="bnd-1", adapter_id="lever", adapter_version="1.0.0",
            cursor_schema_version=1, state_json="{not json", checkpoint_at="",
        )
        assert _adapter().plan(_task(), cursor, ctx=_ctx("rsp-1")).url == LIST_URL


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
            if kind in (AdapterTaskKind.CRAWL, AdapterTaskKind.DISCOVER):
                with pytest.raises(ValueError):
                    adapter.plan(_task(kind, payload), None, ctx=None)
            else:
                assert adapter.plan(_task(kind, payload), None, ctx=None).url


# --------------------------------------------------------------------------
# ENUMERATE parsing
# --------------------------------------------------------------------------


class TestListParse:
    def test_valid_list_produces_one_observation_per_posting(self):
        outcome = _parse(_adapter(), "postings_list.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert [o.source_job_id for o in outcome.observations] == [ID1, ID2, ID3]
        assert [o.fields["title"] for o in outcome.observations] == [
            "Backend Engineer",
            "Platform Engineer",
            "Senior Data Engineer",
        ]
        assert outcome.failure is None

    def test_a_short_page_is_terminal_not_a_continuation(self):
        """Lever declares no total: fewer items than ``limit`` ends the walk."""
        outcome = _parse(_adapter(), "postings_list.json")
        assert outcome.continuation_required is False
        assert outcome.coverage_proposal == {
            "declared_total": None,
            "skip": 0,
            "limit": 100,
            "page_full": False,
            "observed": 3,
            "rejected_members": 0,
            "detail_tasks": 3,
        }

    def test_observation_carries_source_native_identity_and_rank(self):
        outcome = _parse(_adapter(), "postings_list.json")
        first = outcome.observations[0]
        assert first.fields["source_job_id"] == ID1
        assert first.source_rank_or_order == 0
        assert first.raw_url == LIST_URL

    def test_rank_continues_across_offset_pages(self):
        url = f"{API}{LIST_PATH}?mode=json&skip=200&limit=100"
        outcome = _adapter().parse(_task(), _validated_payload(_page(3), url=url), ctx=None)
        assert [o.source_rank_or_order for o in outcome.observations] == [200, 201, 202]
        assert outcome.coverage_proposal["skip"] == 200

    def test_application_url_is_derived_from_the_endpoint_table_not_content(self):
        outcome = _parse(_adapter(), "postings_list.json")
        assert [o.application_url_candidate for o in outcome.observations] == [
            f"{HOSTED}/{ID1}", f"{HOSTED}/{ID2}", f"{HOSTED}/{ID3}",
        ]

    def test_hosted_url_on_a_reviewed_host_is_the_canonical_candidate(self):
        outcome = _parse(_adapter(), "postings_list.json")
        assert outcome.observations[0].canonical_url_candidate == f"{HOSTED}/{ID1}"

    def test_hosted_url_on_an_unreviewed_host_is_refused_with_evidence(self):
        payload = [_posting(1, hostedUrl=f"https://evil.example/acme/{ID1}",
                            applyUrl=f"https://evil.example/acme/{ID1}/apply")]
        outcome = _adapter().parse(_task(), _validated_payload(payload), ctx=None)
        observation = outcome.observations[0]
        assert observation.canonical_url_candidate is None
        assert "hosted_url" not in observation.fields
        assert "apply_url" not in observation.fields
        assert observation.application_url_candidate.startswith(HOSTED)
        assert any(item.get("reason") == "UNSAFE_URL_REFUSED" for item in outcome.review_evidence)

    @pytest.mark.parametrize(
        "hosted_url",
        [
            # reviewed host, *another site*: would re-attribute the employer
            f"{HOSTED.rsplit('/', 1)[0]}/evilcorp/{ID1}",
            # reviewed host, this site, *another posting*: would re-attribute the job
            f"{HOSTED}/9e9f9a9b-0000-4000-8000-000000009999",
            # the API host is not a hosted job page
            f"https://api.lever.co/v0/postings/acme/{ID1}",
            # a hosted-looking host that is not in the endpoint table
            f"https://jobs.lever.co.evil.example/acme/{ID1}",
            # path games never reach a different identity
            f"{HOSTED}/{ID1}/../../evilcorp/{ID1}",
        ],
    )
    def test_a_provider_link_for_another_site_or_posting_is_refused(self, hosted_url):
        """S2.6 corrective (02 §32): ``hostedUrl`` feeds origin resolution as
        the canonical job URL, so it is accepted only when the endpoint table
        reads it as *this* site's *this* posting.  A well-formed link about
        something else is content, not a candidate."""
        payload = [_posting(1, id=ID1, hostedUrl=hosted_url, applyUrl=f"{hosted_url}/apply")]
        outcome = _adapter().parse(_task(), _validated_payload(payload), ctx=None)
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        observation = outcome.observations[0]
        assert observation.canonical_url_candidate is None
        assert "hosted_url" not in observation.fields
        assert "apply_url" not in observation.fields
        # the product link is still derived from the pinned site + validated id
        assert observation.application_url_candidate == f"{HOSTED}/{ID1}"
        assert any(item.get("reason") == "UNSAFE_URL_REFUSED" for item in outcome.review_evidence)

    def test_this_postings_hosted_and_apply_links_are_accepted_on_every_reviewed_host(self):
        for host in ("jobs.lever.co", "eu.jobs.lever.co", "hk.jobs.lever.co"):
            hosted = f"https://{host}/acme/{ID1}"
            payload = [_posting(1, id=ID1, hostedUrl=hosted, applyUrl=f"{hosted}/apply")]
            outcome = _adapter().parse(_task(), _validated_payload(payload), ctx=None)
            observation = outcome.observations[0]
            assert observation.canonical_url_candidate == hosted
            assert observation.fields["hosted_url"] == hosted
            assert observation.fields["apply_url"] == f"{hosted}/apply"
            assert outcome.review_evidence == ()

    def test_company_comes_from_the_pinned_binding_config(self):
        outcome = _parse(_adapter(), "postings_list.json")
        assert outcome.observations[0].fields["company"] == "Acme Fixtures"
        locators = {
            (e.field_name, e.locator_kind) for e in outcome.observations[0].field_evidence
        }
        assert ("company", "binding_config") in locators

    def test_without_a_declared_company_no_company_is_invented(self):
        adapter = LeverAdapter(LeverConfig(board=BOARD))
        outcome = _parse(adapter, "postings_list.json")
        assert "company" not in outcome.observations[0].fields

    def test_created_at_is_evidence_not_a_claimed_posted_time(self):
        """Lever states ``createdAt`` (creation), not a publication time.

        It is kept as a durable UTC evidence field; ``posted_at`` stays absent
        so the canonical row never asserts a publication date the provider
        did not state (02 §22).
        """
        outcome = _parse(_adapter(), "postings_list.json")
        for observation in outcome.observations:
            assert "posted_at" not in observation.fields
            assert observation.fields["posting_created_at"].endswith("Z")
        assert outcome.observations[0].fields["posting_created_at"] == "2026-08-18T06:00:00.000000Z"
        locators = {
            e.locator_value for e in outcome.observations[0].field_evidence
            if e.field_name == "posting_created_at"
        }
        assert locators == {"createdAt"}

    @pytest.mark.parametrize("stamp", ["1786698000000", "2026-08-18T06:00:00Z", True, -1, 1.5, None])
    def test_an_unusable_created_at_is_dropped_not_guessed(self, stamp):
        outcome = _adapter().parse(_task(), _validated_payload([_posting(1, createdAt=stamp)]), ctx=None)
        assert "posting_created_at" not in outcome.observations[0].fields

    def test_team_commitment_country_and_workplace_are_kept_as_evidence(self):
        outcome = _parse(_adapter(), "postings_list.json")
        fields = outcome.observations[1].fields
        assert fields["team"] == "Infrastructure"
        assert fields["department"] == "Technology"
        assert fields["employment_type"] == "Full-time"
        assert fields["country"] == "DE"
        assert fields["workplace_type"] == "hybrid"
        # hybrid is not remote: no remote side-channel is asserted
        assert "job_location_type" not in fields

    def test_remote_workplace_type_is_the_documented_remote_side_channel(self):
        outcome = _parse(_adapter(), "postings_list.json")
        remote = outcome.observations[2].fields
        assert remote["workplace_type"] == "remote"
        assert remote["job_location_type"] == "REMOTE"
        assert remote["locations"] == ["Remote"]

    def test_an_unknown_workplace_type_is_refused_not_mapped(self):
        outcome = _adapter().parse(
            _task(), _validated_payload([_posting(1, workplaceType="teleport")]), ctx=None
        )
        fields = outcome.observations[0].fields
        assert "workplace_type" not in fields and "job_location_type" not in fields
        assert any(item.get("reason") == "UNRECOGNIZED_VALUE_REFUSED" for item in outcome.review_evidence)

    def test_all_locations_win_over_the_primary_location(self):
        outcome = _parse(_adapter(), "postings_list.json")
        assert outcome.observations[1].fields["locations"] == ["Berlin, Germany", "Paris, France"]
        locators = {
            e.locator_value for e in outcome.observations[1].field_evidence
            if e.field_name == "locations"
        }
        assert locators == {"categories.allLocations"}

    def test_primary_location_is_the_fallback(self):
        item = _posting(1)
        item["categories"].pop("allLocations")
        outcome = _adapter().parse(_task(), _validated_payload([item]), ctx=None)
        observation = outcome.observations[0]
        assert observation.fields["locations"] == ["Berlin, Germany"]
        assert {e.locator_value for e in observation.field_evidence
                if e.field_name == "locations"} == {"categories.location"}

    def test_every_extracted_field_has_locator_evidence(self):
        outcome = _parse(_adapter(), "postings_list.json")
        for observation in outcome.observations:
            names = {e.field_name for e in observation.field_evidence}
            assert names == set(observation.fields)
            assert {"source_job_id", "title"} <= names
            for evidence in observation.field_evidence:
                assert evidence.locator_kind in {"json_path", "binding_config"}
                assert evidence.locator_value
                assert len(evidence.value_hash) == 64

    def test_detail_child_tasks_are_typed_bounded_and_lower_priority(self):
        outcome = _parse(_adapter(), "postings_list.json")
        assert [t.kind for t in outcome.discovered_tasks] == ["DETAIL"] * 3
        assert [t.target_reference for t in outcome.discovered_tasks] == [ID1, ID2, ID3]
        assert {t.depth for t in outcome.discovered_tasks} == {1}
        # enumeration pages must be claimable before their own child work
        assert all(t.priority < 0 for t in outcome.discovered_tasks)
        assert len({t.logical_key for t in outcome.discovered_tasks}) == 3
        assert all(t.logical_key.startswith("lever:acme:posting:") for t in outcome.discovered_tasks)

    def test_detail_budget_caps_child_work_without_losing_listing_evidence(self):
        outcome = _parse(_adapter(max_detail_requests=2), "postings_list.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert outcome.continuation_required is False
        assert len(outcome.discovered_tasks) == 2
        assert len(outcome.observations) == 3
        assert outcome.coverage_proposal["detail_tasks"] == 2
        assert outcome.coverage_proposal["observed"] == 3
        assert any(
            item.get("reason") == "DETAIL_BUDGET_REACHED" for item in outcome.review_evidence
        )

    def test_detail_fetch_disabled_emits_no_child_work(self):
        outcome = _parse(_adapter(detail_fetch=False), "postings_list.json")
        assert outcome.discovered_tasks == ()
        assert len(outcome.observations) == 3

    def test_inline_content_suppresses_child_work(self):
        outcome = _parse(_adapter(), "postings_list_with_content.json")
        assert outcome.discovered_tasks == ()
        description = outcome.observations[0].fields["description"]
        assert "local-first" in description
        # the split content members are joined into one document for the
        # host's single cleaning path: body, then headed lists, then additional
        assert "<h3>Responsibilities</h3><ul><li>Own the ingestion pipeline</li>" in description
        assert description.index("local-first") < description.index("Responsibilities")
        assert description.index("Responsibilities") < description.index("Salary band")
        locators = {e.locator_value for e in outcome.observations[0].field_evidence
                    if e.field_name == "description"}
        assert locators == {"description+lists+additional"}

    def test_empty_site_is_a_recognized_success_not_a_failure(self):
        outcome = _parse(_adapter(), "postings_list_empty.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
        assert outcome.observations == ()
        assert outcome.discovered_tasks == ()
        assert outcome.failure is None
        assert outcome.continuation_required is False

    def test_a_full_page_is_a_continuation_never_terminal(self):
        """No declared total: a page that fills ``limit`` may be followed by
        more, so it can never be terminal membership proof (ACQ-03)."""
        adapter = _adapter(page_size=5, detail_fetch=False)
        url = f"{API}{LIST_PATH}?mode=json&skip=0&limit=5"
        outcome = adapter.parse(_task(), _validated_payload(_page(5), url=url), ctx=None)
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert outcome.continuation_required is True
        assert outcome.coverage_proposal["page_full"] is True
        assert len(outcome.observations) == 5

    def test_a_rejected_member_makes_the_page_partial_with_accepted_observations(self):
        payload = [_posting(1), _posting(2, id="../passwd"), _posting(3, text=None)]
        outcome = _adapter().parse(_task(), _validated_payload(payload), ctx=None)
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        assert [o.source_job_id for o in outcome.observations] == [_posting(1)["id"]]
        assert outcome.coverage_proposal["rejected_members"] == 2
        assert outcome.continuation_required is False
        reasons = [item.get("reason") for item in outcome.review_evidence]
        assert reasons.count("REQUIRED_FIELD_MISSING") == 2
        assert outcome.failure is None

    def test_changed_template_is_parse_marker_missing_not_success_empty(self):
        outcome = _parse(_adapter(), "postings_list_changed_template.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.observations == ()
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.failure.retryable is False
        assert outcome.failure.source_health_impact == "DEGRADED"
        assert outcome.review_evidence[0]["reason"] == "ITEMS_MARKER_MISSING"
        assert "data" in outcome.review_evidence[0]["top_level_keys"]

    @pytest.mark.parametrize("payload", [{}, {"postings": []}, "text", 42, None])
    def test_a_non_array_payload_is_never_an_empty_site(self, payload):
        outcome = _adapter().parse(_task(), _validated_payload(payload), ctx=None)
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING

    def test_required_field_failures_reject_items_with_evidence(self):
        outcome = _parse(_adapter(), "postings_list_missing_required_fields.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.observations == ()
        assert outcome.discovered_tasks == ()
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        reasons = " ".join(json.dumps(item) for item in outcome.review_evidence)
        assert '"id"' in reasons and '"text"' in reasons
        assert len(outcome.review_evidence) == 3

    def test_a_traversal_id_never_becomes_a_fetch_target(self):
        outcome = _parse(_adapter(), "postings_list_missing_required_fields.json")
        assert outcome.discovered_tasks == ()
        assert outcome.observations == ()
        # the refused value survives as bounded review evidence only
        assert any("passwd" in json.dumps(item) for item in outcome.review_evidence)
        assert any("javascript" in json.dumps(item).lower() for item in outcome.review_evidence)

    def test_non_object_items_are_rejected_with_evidence(self):
        outcome = _adapter().parse(_task(), _validated_payload([_posting(1), "junk", 7]), ctx=None)
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        assert len(outcome.observations) == 1
        assert [i.get("reason") for i in outcome.review_evidence] == ["ITEM_NOT_AN_OBJECT"] * 2

    def test_unparseable_body_is_a_typed_parse_failure(self):
        # wrapped as VALID_LIST on purpose: this pins the *parser's* own
        # refusal; the classifier gate for the same bytes is pinned in the
        # E2E suite (UNEXPECTED_CONTENT never reaches a normal parser)
        outcome = _parse(_adapter(), "postings_list_malformed.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.observations == ()

    def test_outcome_carries_acq09_evidence_references(self):
        outcome = _parse(_adapter(), "postings_list.json")
        assert outcome.contract_version >= 2
        assert outcome.evidence_refs
        assert all(ref.startswith("result://") or ref.startswith("validity://")
                   for ref in outcome.evidence_refs)


# --------------------------------------------------------------------------
# DETAIL parsing
# --------------------------------------------------------------------------


class TestFailureTraceability:
    """S2.6 corrective (ACQ-09): a failed parse is as traceable as a success."""

    @pytest.mark.parametrize(
        "fixture",
        [
            "postings_list_changed_template.json",
            "postings_list_missing_required_fields.json",
            "postings_list_malformed.json",
        ],
    )
    def test_list_failures_link_the_envelope_and_validity_evidence(self, fixture):
        outcome = _parse(_adapter(), fixture)
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure is not None
        assert outcome.evidence_refs, "failure outcome lost its ACQ-09 evidence refs"
        assert len(outcome.evidence_refs) == len(set(outcome.evidence_refs))

    def test_detail_failures_link_the_envelope_and_validity_evidence(self):
        adapter = _adapter()
        task = _task(AdapterTaskKind.DETAIL, {"target_reference": ID1})
        for body in (b"[]", b"not json", b'{"id": "' + ID1.encode() + b'"}'):
            outcome = adapter.parse(
                task,
                ValidatedResultEnvelope(
                    envelope=_envelope(body, url=f"{API}{LIST_PATH}/{ID1}"),
                    page_class=PageClass.VALID_JOB,
                    validation_evidence={"fixture": "inline"},
                ),
                ctx=None,
            )
            assert outcome.kind is ParseOutcomeKind.FAILURE, body
            assert outcome.evidence_refs, body

    def test_health_probe_failures_link_evidence(self):
        outcome = _adapter().parse(
            _task(AdapterTaskKind.HEALTH),
            _validated("postings_list_changed_template.json", task_kind=AdapterTaskKind.HEALTH),
            ctx=None,
        )
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.evidence_refs


class TestDetailParse:
    def _detail(self, adapter, fixture, reference=ID1, **kwargs):
        url = kwargs.pop("url", f"{API}{LIST_PATH}/{reference}")
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
        outcome = self._detail(_adapter(), "posting_detail.json")
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert len(outcome.observations) == 1
        observation = outcome.observations[0]
        assert observation.source_job_id == ID1
        assert observation.fields["title"] == "Backend Engineer"
        assert "local-first" in observation.fields["description"]
        assert "Own the ingestion pipeline" in observation.fields["description"]
        assert "Salary band" in observation.fields["description"]
        assert observation.fields["employment_type"] == "Full-time"
        assert observation.fields["team"] == "Engineering"
        assert observation.fields["country"] == "DE"
        assert observation.fields["posting_created_at"] == "2026-08-18T06:00:00.000000Z"
        assert "posted_at" not in observation.fields
        assert observation.canonical_url_candidate == f"{HOSTED}/{ID1}"
        assert observation.application_url_candidate == f"{HOSTED}/{ID1}"
        assert observation.fields["apply_url"] == f"{HOSTED}/{ID1}/apply"

    def test_detail_parse_emits_no_further_child_work(self):
        outcome = self._detail(_adapter(), "posting_detail.json")
        assert outcome.discovered_tasks == ()

    def test_multi_location_detail_keeps_the_whole_set(self):
        outcome = self._detail(_adapter(), "posting_detail_multi_location.json", reference=ID2)
        fields = outcome.observations[0].fields
        assert fields["locations"] == ["Berlin, Germany", "Paris, France"]
        assert fields["workplace_type"] == "hybrid"
        assert "job_location_type" not in fields

    def test_remote_detail_is_explicit_and_hostile_links_are_refused(self):
        outcome = self._detail(_adapter(), "posting_detail_remote_hostile_links.json", reference=ID3)
        observation = outcome.observations[0]
        assert observation.fields["locations"] == ["Remote"]
        assert observation.fields["job_location_type"] == "REMOTE"
        # javascript:/data: hostedUrl/applyUrl are refused as link candidates
        assert observation.canonical_url_candidate is None
        assert "hosted_url" not in observation.fields
        assert "apply_url" not in observation.fields
        # the safe apply link is derived from the endpoint table, not content
        assert observation.application_url_candidate == f"{HOSTED}/{ID3}"
        assert any(
            item.get("reason") == "UNSAFE_URL_REFUSED"
            and "javascript" in json.dumps(item).lower()
            for item in outcome.review_evidence
        )
        # the raw body travels untouched to the host's cleaning path
        assert "<script>" in observation.fields["description"]

    def test_a_response_about_another_posting_cannot_mint_an_observation(self):
        outcome = self._detail(_adapter(), "posting_detail_id_mismatch.json")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.observations == ()
        assert outcome.failure.kind is FailureKind.INVALID_JOB_RECORD
        assert outcome.failure.retryable is False
        assert outcome.closure_or_missing_evidence
        evidence = outcome.closure_or_missing_evidence[0]
        assert evidence["reason"] == "DETAIL_ID_MISMATCH"
        assert evidence["target_reference"] == ID1
        assert evidence["observed_id"] == OTHER

    def test_a_detail_without_required_fields_reports_missing_evidence(self):
        outcome = _adapter().parse(
            _task(AdapterTaskKind.DETAIL, {"target_reference": ID1}),
            _validated_payload({"id": ID1, "categories": {"location": "Berlin"}},
                               url=f"{API}{LIST_PATH}/{ID1}", page_class=PageClass.VALID_JOB),
            ctx=None,
        )
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.observations == ()
        assert outcome.closure_or_missing_evidence
        assert outcome.closure_or_missing_evidence[0]["reason"] == "REQUIRED_FIELD_MISSING"
        assert outcome.closure_or_missing_evidence[0]["missing"] == ["text"]

    def test_a_detail_that_is_an_array_is_not_a_posting(self):
        outcome = _adapter().parse(
            _task(AdapterTaskKind.DETAIL, {"target_reference": ID1}),
            _validated_payload([_posting(1)], url=f"{API}{LIST_PATH}/{ID1}",
                               page_class=PageClass.VALID_JOB),
            ctx=None,
        )
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.observations == ()

    def test_health_and_smoke_parses_never_emit_observations(self):
        adapter = _adapter()
        for kind in (AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE):
            outcome = adapter.parse(
                _task(kind), _validated("postings_list.json", task_kind=kind), ctx=None
            )
            assert outcome.observations == ()
            assert outcome.discovered_tasks == ()
            assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
            assert outcome.review_evidence[0]["listed_postings"] == 3

    def test_health_probe_on_a_changed_template_is_a_failure(self):
        outcome = _parse(_adapter(), "postings_list_changed_template.json",
                         task_kind=AdapterTaskKind.HEALTH)
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING


# --------------------------------------------------------------------------
# Cursor behaviour (02 §19): bounded offset paging, never absence authority
# --------------------------------------------------------------------------


class TestCursor:
    def _full_page(self, adapter, *, skip=0, start=0, count=None):
        count = adapter.config.page_size if count is None else count
        url = f"{API}{LIST_PATH}?mode=json&skip={skip}&limit={adapter.config.page_size}"
        return adapter.parse(_task(), _validated_payload(_page(count, start), url=url), ctx=None)

    def test_a_short_page_proposes_no_cursor(self):
        adapter = _adapter()
        outcome = _parse(adapter, "postings_list.json")
        assert adapter.next_cursor(_task(), outcome, None, ctx=_ctx()) is None

    def test_empty_and_failure_never_advance_a_cursor(self):
        adapter = _adapter()
        for fixture in ("postings_list_empty.json", "postings_list_changed_template.json",
                        "postings_list_missing_required_fields.json"):
            outcome = _parse(adapter, fixture)
            assert adapter.next_cursor(_task(), outcome, None, ctx=_ctx()) is None

    def test_a_full_page_proposes_the_next_offset_scoped_to_this_plan(self):
        adapter = _adapter(page_size=5, detail_fetch=False)
        outcome = self._full_page(adapter)
        cursor = adapter.next_cursor(_task(), outcome, None, ctx=_ctx("rsp-1"))
        assert cursor is not None
        assert cursor.adapter_id == "lever"
        assert cursor.adapter_version == "1.0.0"
        assert cursor.cursor_schema_version == 1
        state = json.loads(cursor.state_json)
        assert state["plan"] == "rsp-1"
        assert state["skip"] == 5
        assert state["page"] == 1
        assert len(state["ids_hash"]) == 64
        # and planning with it fetches exactly that page
        assert adapter.plan(_task(), cursor, ctx=_ctx("rsp-1")).url == (
            f"{API}{LIST_PATH}?mode=json&skip=5&limit=5"
        )

    def test_the_cursor_is_deterministic(self):
        adapter = _adapter(page_size=5, detail_fetch=False)
        outcome = self._full_page(adapter)
        first = adapter.next_cursor(_task(), outcome, None, ctx=_ctx())
        second = adapter.next_cursor(_task(), outcome, None, ctx=_ctx())
        assert first == second

    def test_a_repeated_page_is_a_trap_not_progress(self):
        """A server that ignores ``skip`` returns the same set again: the
        adapter stops proposing cursors and the page stays non-terminal."""
        adapter = _adapter(page_size=5, detail_fetch=False)
        first = self._full_page(adapter)
        cursor = adapter.next_cursor(_task(), first, None, ctx=_ctx())
        repeated = self._full_page(adapter, skip=5, start=0)  # same ids again
        assert repeated.continuation_required is True
        assert adapter.next_cursor(_task(), repeated, cursor, ctx=_ctx()) is None

    def test_a_full_page_with_a_rejected_member_proposes_no_cursor(self):
        """S2.6 corrective (ACQ-03, RUN-13): a PARTIAL page has already broken
        the membership proof; continuing would let a later clean short page
        launder it into COMPLETE.  The page stays ``continuation_required``
        (bounded PARTIAL at the host), but no cursor is proposed."""
        adapter = _adapter(page_size=3, detail_fetch=False)
        page = _page(2) + [{"text": "No id here", "hostedUrl": f"{HOSTED}/nope"}]
        url = f"{API}{LIST_PATH}?mode=json&skip=0&limit=3"
        outcome = adapter.parse(_task(), _validated_payload(page, url=url), ctx=None)
        assert outcome.kind is ParseOutcomeKind.PARTIAL
        assert outcome.continuation_required is True
        assert len(outcome.observations) == 2
        assert outcome.coverage_proposal["rejected_members"] == 1
        assert adapter.next_cursor(_task(), outcome, None, ctx=_ctx()) is None

    def test_a_different_full_page_keeps_walking(self):
        adapter = _adapter(page_size=5, detail_fetch=False)
        first = self._full_page(adapter)
        cursor = adapter.next_cursor(_task(), first, None, ctx=_ctx())
        second = self._full_page(adapter, skip=5, start=5)
        following = adapter.next_cursor(_task(), second, cursor, ctx=_ctx())
        assert following is not None
        assert json.loads(following.state_json)["skip"] == 10
        assert json.loads(following.state_json)["page"] == 2

    def test_the_declared_page_bound_stops_the_walk_without_terminal_claim(self):
        adapter = _adapter(page_size=5, detail_fetch=False)
        last_index = STOP_POLICY.max_pages - 1
        cursor = CrawlCursor(
            source_id="src-1", binding_id="bnd-1", adapter_id="lever", adapter_version="1.0.0",
            cursor_schema_version=1,
            state_json=json.dumps({"plan": "rsp-1", "skip": 5 * last_index,
                                   "page": last_index, "ids_hash": "prev"}),
            checkpoint_at="",
        )
        outcome = self._full_page(adapter, skip=5 * last_index, start=5 * last_index)
        assert outcome.continuation_required is True  # still not terminal
        assert adapter.next_cursor(_task(), outcome, cursor, ctx=_ctx("rsp-1")) is None

    def test_a_foreign_cursor_is_treated_as_a_fresh_pass(self):
        adapter = _adapter(page_size=5, detail_fetch=False)
        stale = CrawlCursor(
            source_id="src-1", binding_id="bnd-1", adapter_id="lever", adapter_version="1.0.0",
            cursor_schema_version=1,
            state_json=json.dumps({"plan": "rsp-OLD", "skip": 40, "page": 8, "ids_hash": "x"}),
            checkpoint_at="",
        )
        outcome = self._full_page(adapter)
        cursor = adapter.next_cursor(_task(), outcome, stale, ctx=_ctx("rsp-1"))
        assert json.loads(cursor.state_json) == {
            "ids_hash": json.loads(cursor.state_json)["ids_hash"],
            "page": 1, "plan": "rsp-1", "skip": 5,
        }

    def test_detail_outcomes_never_propose_cursors(self):
        adapter = _adapter()
        outcome = adapter.parse(
            _task(AdapterTaskKind.DETAIL, {"target_reference": ID1}),
            _validated("posting_detail.json", page_class=PageClass.VALID_JOB,
                       url=f"{API}{LIST_PATH}/{ID1}", task_kind=AdapterTaskKind.DETAIL),
            ctx=None,
        )
        assert adapter.next_cursor(
            _task(AdapterTaskKind.DETAIL, {"target_reference": ID1}), outcome, None, ctx=_ctx()
        ) is None


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


class TestPurity:
    def test_parsing_never_touches_a_connection(self):
        """A parser receives no connection and returns data only; passing a
        sentinel object as the context must not change the outcome."""
        outcome = _adapter().parse(_task(), _validated("postings_list.json"), ctx=object())
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS

    def test_planning_uses_the_planning_context_read_only(self):
        ctx = _ctx()
        plan = _adapter().plan(_task(), None, ctx=ctx)
        assert plan.url == LIST_URL
        assert ctx == _ctx()

    def test_no_provider_host_literal_lives_in_the_adapter(self):
        from jobscraper.acquisition.atsendpoints import ATS_ENDPOINT_SPECS
        import jobscraper.adapters.lever as module

        text = Path(module.__file__).read_text(encoding="utf-8")
        for spec in ATS_ENDPOINT_SPECS:
            for host in spec.hosts():
                assert f'"{host}"' not in text and f"'{host}'" not in text

    def test_fixtures_are_deterministic(self):
        payload = _fixture_json("postings_list.json")
        assert isinstance(payload, list) and len(payload) == 3
        assert _fixture("postings_list.json") == _fixture("postings_list.json")
        assert _fixture_json("postings_list_empty.json") == []
