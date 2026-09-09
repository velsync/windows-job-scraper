"""Unit tests for the declarative JSON feed adapter (S1.5 / ``json_api_feed``).

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md (02 §22
required-field discipline, ACQ-02/ACQ-03 parse outcomes, ACQ-09 versioned
contracts / traceable evidence).

Deferred finding **DF-2** (recorded S2.6, closed here in S2.9): every S2.5+
provider adapter attaches ACQ-09 ``evidence_refs`` (the envelope + validity
evidence rows) to its ``FAILURE`` ``ParseOutcome`` — a failure is as
traceable as a success.  ``FeedApiAdapter._failure`` did not, so a feed
failure outcome carried no reference to the durable evidence that produced
it.  These tests pin the parity so a feed failure can never drop its
evidence trail again.
"""

from __future__ import annotations

import pytest

from jobscraper.acquisition.failures import FailureKind
from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    ParseOutcomeKind,
    ValidatedResultEnvelope,
    validate_manifest,
)
from jobscraper.adapters.feed_api import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    MANIFEST,
    FeedApiAdapter,
    FeedApiConfig,
)

URL = "http://127.0.0.1:8765/feed?page=1"

_FIELDS = {
    "source_job_id": {"path": "id", "required": True},
    "title": {"path": "title", "required": True},
}


def _config(**overrides) -> FeedApiConfig:
    base = {
        "url_template": "http://127.0.0.1:8765/feed?page={page}",
        "items_path": "jobs",
        "fields": _FIELDS,
    }
    base.update(overrides)
    return FeedApiConfig(**base)


def _adapter(**overrides) -> FeedApiAdapter:
    return FeedApiAdapter(_config(**overrides))


def _task() -> AdapterTask:
    return AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={})


def _validated(body: bytes, *, page_class: PageClass = PageClass.VALID_LIST) -> ValidatedResultEnvelope:
    return ValidatedResultEnvelope(
        envelope=ResultEnvelope(
            execution_plan_id="plan-1",
            request_id="req-1",
            attempt_id="att-1",
            run_source_plan_id="rsp-1",
            source_id="src-1",
            binding_id="bnd-1",
            binding_revision_id="bndrev-1",
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            requested_url=URL,
            final_url=URL,
            status_code=200,
            content_type="application/json",
            body=body,
        ).finalize(),
        page_class=page_class,
        validation_evidence={"shape": "feed"},
    )


def _parse(body: bytes):
    return _adapter().parse(_task(), _validated(body), ctx=None)


class TestManifest:
    def test_manifest_is_schema_valid(self):
        from dataclasses import asdict

        manifest = validate_manifest(asdict(MANIFEST))
        assert manifest.id == "json_api_feed"
        assert manifest.version == "1.0.0"

    def test_listing_identity_is_sufficient(self):
        assert FeedApiAdapter.listing_identity_sufficient is True


class TestSuccessPaths:
    def test_valid_feed_produces_observations(self):
        body = b'{"jobs": [{"id": "1", "title": "Backend"}, {"id": "2", "title": "Frontend"}]}'
        outcome = _parse(body)
        assert outcome.kind is ParseOutcomeKind.SUCCESS_WITH_JOBS
        assert [o.source_job_id for o in outcome.observations] == ["1", "2"]
        assert outcome.failure is None

    def test_empty_feed_is_success_empty(self):
        outcome = _parse(b'{"jobs": []}')
        assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
        assert outcome.observations == ()


class TestFailureTraceability:
    """DF-2 (ACQ-09 parity): a FAILURE outcome carries durable evidence refs."""

    def test_malformed_body_failure_carries_evidence_refs(self):
        outcome = _parse(b"not json {")
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.failure.adapter_id == ADAPTER_ID
        assert outcome.evidence_refs, "feed failure dropped its ACQ-09 evidence refs (DF-2)"

    def test_missing_items_marker_failure_carries_evidence_refs(self):
        outcome = _parse(b'{"postings": []}')
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.evidence_refs

    def test_all_items_invalid_failure_carries_evidence_refs(self):
        # every item fails the required-field gate -> PARSE_MARKER_MISSING
        body = b'{"jobs": [{"title": "no id"}, {"id": "1"}]}'
        outcome = _parse(body)
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.review_evidence
        assert outcome.evidence_refs

    @pytest.mark.parametrize(
        "body",
        [
            b"not json {",
            b'{"postings": []}',
            b'{"jobs": [{"title": "no id"}]}',
        ],
    )
    def test_every_failure_kind_carries_the_envelope_and_validity_refs(self, body):
        """The two ACQ-09 refs (envelope body-ref + validity) are both present."""
        outcome = _parse(body)
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.evidence_refs
        assert len(outcome.evidence_refs) == 2
        assert any(ref.startswith("result://") for ref in outcome.evidence_refs)
        assert any(ref.startswith("validity://") for ref in outcome.evidence_refs)

    def test_a_failure_is_never_success_empty(self):
        outcome = _parse(b'{"jobs": "not-a-list"}')
        assert outcome.kind is ParseOutcomeKind.FAILURE
        assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
        assert outcome.observations == ()
        assert outcome.failure is not None
