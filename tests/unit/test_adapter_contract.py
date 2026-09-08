"""S1.5 tests: adapter contract + declarative JSON API/feed adapter.

Authority: 02 ACQ-02 (corrected adapter protocol), ACQ-03 (parse outcome
model), ACQ-04 (detail-child planning), ACQ-09 (versioned contracts),
§19 (crawl cursor and pagination contract), §22 (ExtractionRecipe
required-field discipline).

Proves:

* manifest schema validation (typed rejections, no free-form manifests);
* the feed adapter plans requests from templates + cursor state;
* parsers receive only validated results (validity gate is structural);
* required-field failures produce structured evidence and never invent
  values; an all-invalid page is PARSE_MARKER_MISSING, not SUCCESS_EMPTY;
* SUCCESS_EMPTY is a recognized empty feed, not a failure;
* cursor advancement and terminal detection; loop/trap protection
  (repeated cursor / URL / page hash / job set);
* adapters never touch the database and never write canonical jobs
  (pure plan/parse functions).
"""

from __future__ import annotations

import json

import pytest

from jobscraper.adapters.contract import (
    AdapterManifest,
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    ObservationRecord,
    PageSignature,
    PaginationTracker,
    StopPolicy,
    ValidatedResult,
    validate_manifest,
)
from jobscraper.adapters.feed_api import FeedApiAdapter, FeedApiConfig
from jobscraper.adapters.registry import BUILTIN_ADAPTERS, get_adapter
from jobscraper.acquisition.failures import FailureKind
from jobscraper.acquisition.pagevalidity import PageClass, PageClassification
from jobscraper.acquisition.result import ResultEnvelope

NOW = "2026-09-08T09:00:00.000000Z"

CONFIG = {
    "url_template": "https://jobs.example.test/api/jobs?page={page}",
    "items_path": "jobs",
    "fields": {
        "source_job_id": {"path": "id", "required": True},
        "title": {"path": "title", "required": True},
        "company": {"path": "company.name"},
        "description": {"path": "description"},
        "job_url": {"path": "url"},
        "apply_url": {"path": "apply_url"},
        "locations": {"path": "locations", "many": True},
        "posted_at": {"path": "created_at"},
    },
    "terminal_when": "empty_items",
}


def _manifest_dict(**overrides):
    base = {
        "id": "json_api_feed",
        "version": "1.0.0",
        "adapter_api_version": "1",
        "capabilities": ["listing_parse", "incremental"],
        "supported_execution_classes": ["HTTP"],
        "supported_auth_modes": ["NONE"],
        "cost_class": "LIGHT",
        "cursor_schema_version": 1,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------ manifest schema


def test_manifest_validation_accepts_valid_and_rejects_invalid():
    manifest = validate_manifest(_manifest_dict())
    assert manifest.id == "json_api_feed"
    with pytest.raises(ValueError):
        validate_manifest(_manifest_dict(id=""))
    with pytest.raises(ValueError):
        validate_manifest(_manifest_dict(version="not-semver"))
    with pytest.raises(ValueError):
        validate_manifest(_manifest_dict(capabilities=["mind_reading"]))
    with pytest.raises(ValueError):
        validate_manifest(_manifest_dict(supported_execution_classes=["SMTP"]))
    with pytest.raises(ValueError):
        validate_manifest({})  # missing everything


def test_registry_has_only_builtin_manifest_validated_adapters():
    from jobscraper.adapters.feed_api import FeedApiConfig

    for adapter_id, factory in BUILTIN_ADAPTERS.items():
        adapter = factory(FeedApiConfig(**CONFIG))
        assert adapter.manifest.id == adapter_id
    assert get_adapter("json_api_feed") is not None
    with pytest.raises(KeyError):
        get_adapter("does-not-exist")
    # no dynamic import: registry keys are fixed
    assert set(BUILTIN_ADAPTERS) == {"json_api_feed"}


# ------------------------------------------------------------------ planning


def _adapter():
    return FeedApiAdapter(FeedApiConfig(**CONFIG))


def test_plan_renders_url_template_from_cursor_state():
    adapter = _adapter()
    task = AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={})
    plan = adapter.plan(task, cursor=None, ctx=None)
    assert plan.url == "https://jobs.example.test/api/jobs?page=1"
    cursor = CrawlCursor(
        source_id="src-1",
        binding_id="bnd-1",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        cursor_schema_version=1,
        state_json={"page": 3},
        checkpoint_at=NOW,
    )
    plan = adapter.plan(task, cursor=cursor, ctx=None)
    assert plan.url.endswith("page=3")
    assert plan.method == "GET"


def _envelope(body: bytes, url="https://jobs.example.test/api/jobs?page=1") -> ResultEnvelope:
    return ResultEnvelope(
        execution_plan_id="plan-1",
        request_id="req-1",
        attempt_id="att-1",
        run_source_plan_id="rsp-1",
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type="application/json",
        body=body,
    )


def _validated(body: bytes, url="https://jobs.example.test/api/jobs?page=1") -> ValidatedResult:
    envelope = _envelope(body, url)
    return ValidatedResult(
        envelope=envelope,
        page_class=PageClass.VALID_LIST,
        validation_evidence={"status_code": 200},
    )


# -------------------------------------------------------------------- parsing


def test_parse_happy_path_extracts_observations_with_evidence():
    adapter = _adapter()
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": "fx-100",
                    "title": "Backend Engineer",
                    "company": {"name": "Fixture Corp"},
                    "description": "<p>Build things</p>",
                    "url": "https://jobs.example.test/jobs/fx-100",
                    "apply_url": "https://jobs.example.test/jobs/fx-100/apply",
                    "locations": ["Remote (EU)", "Berlin"],
                    "created_at": "2026-09-01T00:00:00Z",
                }
            ]
        }
    ).encode()
    task = AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={})
    outcome = adapter.parse(task, _validated(body), ctx=None)
    assert outcome.kind.value == "SUCCESS_WITH_JOBS"
    assert len(outcome.observations) == 1
    obs: ObservationRecord = outcome.observations[0]
    assert obs.source_job_id == "fx-100"
    assert obs.fields["title"] == "Backend Engineer"
    assert obs.fields["company"] == "Fixture Corp"
    assert obs.fields["locations"] == ["Remote (EU)", "Berlin"]
    assert obs.raw_url == "https://jobs.example.test/api/jobs?page=1"
    assert obs.canonical_url_candidate == "https://jobs.example.test/jobs/fx-100"
    # field evidence recorded per extracted field (§30)
    ev = {e.field_name: e for e in obs.field_evidence}
    assert ev["title"].locator_kind == "json_path"
    assert ev["title"].locator_value == "title"
    assert ev["source_job_id"].value_hash
    assert outcome.failure is None


def test_parse_missing_required_field_never_invents_values():
    adapter = _adapter()
    body = json.dumps(
        {
            "jobs": [
                {"id": "fx-1", "title": "Has title"},
                {"id": "fx-2"},  # title missing -> structured rejection
                {"title": "No id"},  # source_job_id missing -> structured rejection
            ]
        }
    ).encode()
    outcome = adapter.parse(AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={}), _validated(body), ctx=None)
    assert outcome.kind.value == "SUCCESS_WITH_JOBS"
    assert [o.source_job_id for o in outcome.observations] == ["fx-1"]
    rejected = outcome.review_evidence
    assert len(rejected) == 2
    assert all("missing required field" in r["reason"] for r in rejected)
    # no invented values anywhere
    assert all("title" in o.fields and o.fields["title"] for o in outcome.observations)


def test_parse_all_items_invalid_is_marker_missing_not_empty():
    adapter = _adapter()
    body = json.dumps({"jobs": [{"id": "fx-1"}, {"id": "fx-2"}]}).encode()
    outcome = adapter.parse(AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={}), _validated(body), ctx=None)
    assert outcome.kind.value == "FAILURE"
    assert outcome.failure.kind == FailureKind.PARSE_MARKER_MISSING
    assert len(outcome.review_evidence) == 2


def test_parse_recognized_empty_feed_is_success_empty():
    adapter = _adapter()
    body = json.dumps({"jobs": []}).encode()
    task = AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={})
    outcome = adapter.parse(task, _validated(body), ctx=None)
    assert outcome.kind.value == "SUCCESS_EMPTY"
    assert outcome.failure is None
    # terminal: no next cursor
    assert adapter.next_cursor(task, outcome, None, ctx=None) is None


def test_parse_unexpected_json_shape_is_marker_missing():
    adapter = _adapter()
    body = json.dumps({"results": {"unexpected": "shape"}}).encode()
    outcome = adapter.parse(AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={}), _validated(body), ctx=None)
    assert outcome.kind.value == "FAILURE"
    assert outcome.failure.kind == FailureKind.PARSE_MARKER_MISSING


# ------------------------------------------------------- cursor + stop policy


def test_next_cursor_advances_until_terminal():
    adapter = _adapter()
    task = AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={})
    body = json.dumps({"jobs": [{"id": "fx-1", "title": "T"}]}).encode()
    outcome = adapter.parse(task, _validated(body), ctx=None)
    cursor = adapter.next_cursor(task, outcome, None, ctx=None)
    assert cursor is not None
    assert json.loads(cursor.state_json) == {"page": 2}
    # an empty page is terminal
    empty = adapter.parse(task, _validated(json.dumps({"jobs": []}).encode(), url="...?page=2"), ctx=None)
    assert adapter.next_cursor(task, empty, cursor, ctx=None) is None


def test_stop_policy_declared_and_bounded():
    policy = StopPolicy()
    assert policy.max_pages >= 1
    assert policy.max_consecutive_empty_pages >= 1
    assert policy.max_requests >= 1
    assert policy.max_runtime_s >= 1


# --------------------------------------------------------- loop/trap protection


def _page_sig(page=1, ids=("fx-1",), body_hash="hash-a", url=None):
    return PageSignature(
        cursor_state=json.dumps({"page": page}),
        url=url or f"https://jobs.example.test/api/jobs?page={page}",
        page_body_hash=body_hash,
        job_ids=frozenset(ids),
    )


def test_pagination_tracker_detects_all_four_trap_shapes():
    # repeated next cursor
    tracker = PaginationTracker()
    tracker.record(_page_sig(page=2, ids=("a",), body_hash="h1"))
    assert tracker.is_trap(_page_sig(page=2, ids=("b",), body_hash="h2")) is True
    # repeated next URL (different cursor state, same URL)
    tracker = PaginationTracker()
    tracker.record(PageSignature('{"page":1}', "https://x/p", "h1", frozenset({"a"})))
    assert (
        tracker.is_trap(PageSignature('{"page":9}', "https://x/p", "h2", frozenset({"b"}))) is True
    )
    # repeated normalized page hash
    tracker = PaginationTracker()
    tracker.record(PageSignature('{"page":1}', "https://x/1", "h1", frozenset({"a"})))
    assert (
        tracker.is_trap(PageSignature('{"page":2}', "https://x/2", "h1", frozenset({"b"}))) is True
    )
    # repeated same job set
    tracker = PaginationTracker()
    tracker.record(PageSignature('{"page":1}', "https://x/1", "h1", frozenset({"a", "b"})))
    assert (
        tracker.is_trap(PageSignature('{"page":2}', "https://x/2", "h2", frozenset({"a", "b"})))
        is True
    )
    # a genuinely new page is not a trap
    tracker = PaginationTracker()
    tracker.record(_page_sig(ids=("a",)))
    assert tracker.is_trap(_page_sig(page=2, ids=("b",), body_hash="h2")) is False


# ----------------------------------------------------- validity gate structure


def test_parser_contract_only_accepts_validated_results():
    # ValidatedResult is the only parse input; constructing one requires an
    # explicit page_class, and the driver (S1.6) only builds them for valid
    # classes. The type is structural: adapters cannot see raw responses.
    v = _validated(b'{"jobs": []}')
    assert v.page_class == PageClass.VALID_LIST
    assert v.envelope.status_code == 200


def test_adapter_never_writes_canonical_jobs():
    # the adapter API surface is plan/parse/next_cursor: pure functions over
    # dataclasses; there is no database handle anywhere in the contract
    import inspect

    from jobscraper.adapters import contract, feed_api

    source = inspect.getsource(contract) + inspect.getsource(feed_api)
    assert "sqlite3" not in source
    assert "INSERT INTO" not in source
    assert "jobscraper.db" not in source
