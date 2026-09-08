"""S2.1 — richer ResultEnvelope + versioned cross-component evidence contracts.

Covers 02 §11.3 (complete ResultEnvelope), ACQ-09 (PlanningContext /
ValidatedResultEnvelope / ParseContext / ParseOutcome shapes) and 03 §30
(fetch → result → parse → field-evidence chain).
"""

from __future__ import annotations

import json

import pytest

from jobscraper.acquisition.result import (
    RESULT_CONTRACT_VERSION,
    ResultEnvelope,
    normalized_content_hash,
    refs_for,
)
from jobscraper.adapters.contract import (
    PARSE_CONTRACT_VERSION,
    FieldEvidenceRecord,
    ObservationRecord,
    ParseContext,
    ParseOutcome,
    ParseOutcomeKind,
    PlanningContext,
    ValidatedResult,
    ValidatedResultEnvelope,
)
from jobscraper.acquisition.pagevalidity import PageClass


def _envelope(**overrides) -> ResultEnvelope:
    body = overrides.pop("body", b'{"jobs": [{"id": "1"}]}')
    base = dict(
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
        requested_url="https://x.test/jobs?page=1",
        final_url="https://x.test/jobs?page=1",
        status_code=200,
        content_type="application/json",
        body=body,
    )
    base.update(overrides)
    envelope = ResultEnvelope(**base)
    if envelope.body_hash is None:
        envelope.body_hash = ResultEnvelope.make_body_hash(envelope.body)
    if envelope.normalized_content_hash is None:
        envelope.normalized_content_hash = normalized_content_hash(
            envelope.body, envelope.content_type
        )
    envelope.body_ref, envelope.structured_payload_ref = refs_for(envelope)
    return envelope


# ----------------------------------------------------------- ResultEnvelope


def test_result_envelope_carries_every_section_11_3_member_plus_refs():
    envelope = _envelope()
    for member in (
        "execution_plan_id", "request_id", "attempt_id", "run_source_plan_id",
        "source_id", "binding_id", "binding_revision_id", "adapter_id",
        "adapter_version", "strategy", "execution_class", "requested_url",
        "final_url", "status_code", "headers_redacted", "content_type",
        "body_hash", "normalized_content_hash", "fetched_at", "duration_ms",
        "bytes_downloaded", "redirect_chain", "transport", "browser_used",
        "robots_decision", "validators_sent", "was_304",
        "resource_blocking_applied", "failure",
    ):
        assert hasattr(envelope, member), member
    # Slice-2 additions (03 §30 evidence plane + ACQ-09 contract versioning)
    assert envelope.contract_version == RESULT_CONTRACT_VERSION
    assert envelope.body_ref.startswith("result://att-1/body/")
    assert envelope.structured_payload_ref.startswith("result://att-1/structured/")
    assert envelope.security_policy_result == "ALLOWED"
    assert envelope.evidence_refs == ()
    assert envelope.cache_representation_ref is None


def test_normalized_content_hash_is_stable_across_formatting_noise():
    a = b'{"b": 2, "a": [1,  2], "jobs": []}'
    b = b'{ "a": [1, 2], "jobs": [], "b": 2 }'
    assert normalized_content_hash(a, "application/json") == normalized_content_hash(
        b, "application/json;charset=utf-8"
    )


def test_normalized_content_hash_distinguishes_real_json_change():
    a = normalized_content_hash(b'{"jobs": [{"id": "1"}]}', "application/json")
    b = normalized_content_hash(b'{"jobs": [{"id": "2"}]}', "application/json")
    assert a != b


def test_normalized_content_hash_collapses_html_whitespace_but_not_text():
    html_a = b"<div>  <p>Hello   there</p> </div>"
    html_b = b"<div><p>Hello there</p></div>\n\n"
    assert normalized_content_hash(html_a, "text/html") == normalized_content_hash(
        html_b, "text/html"
    )
    assert normalized_content_hash(html_a, "text/html") != normalized_content_hash(
        b"<div><p>Hello there!</p></div>", "text/html"
    )


def test_body_ref_and_hash_are_content_addressed_and_304_has_no_new_body():
    envelope = _envelope(status_code=304, body=b"", was_304=True)
    assert envelope.body_ref is None
    assert envelope.structured_payload_ref is None
    assert envelope.cache_representation_ref is None


# ------------------------------------------------- ACQ-09 contract contracts


def test_planning_context_is_versioned_and_pin_only():
    ctx = PlanningContext(
        contract_version=PARSE_CONTRACT_VERSION,
        run_id="run-1",
        run_source_plan_id="rsp-1",
        source_snapshot_ref="snap://source",
        binding_revision_id="bndrev-1",
        permission_profile_revision=1,
        policy_snapshot_ref="snap://policy",
        query_revision_ref=None,
        budget_snapshot_ref="snap://budget",
    )
    assert ctx.contract_version == PARSE_CONTRACT_VERSION
    with pytest.raises(Exception):  # FrozenInstanceError: no silent mutation
        ctx.run_id = "other"


def test_validated_result_envelope_is_the_only_parse_input_and_rejects_invalid():
    envelope = _envelope()
    validated = ValidatedResultEnvelope(
        envelope=envelope,
        page_class=PageClass.VALID_LIST,
        validation_evidence={"status_code": 200},
        security_policy_result="ALLOWED",
        cache_representation_ref=None,
    )
    assert validated.contract_version == PARSE_CONTRACT_VERSION
    assert validated.result_envelope_ref == envelope.body_ref
    assert validated.validated_page_class is PageClass.VALID_LIST
    # the Slice-1 name remains a supported alias for the same contract
    assert ValidatedResult is ValidatedResultEnvelope
    for invalid in (
        PageClass.LOGIN_REQUIRED,
        PageClass.AUTH_EXPIRED,
        PageClass.RATE_LIMITED,
        PageClass.CHALLENGE_PAGE,
        PageClass.UNEXPECTED_CONTENT,
        PageClass.UNKNOWN,
    ):
        with pytest.raises(ValueError):
            ValidatedResultEnvelope(envelope=envelope, page_class=invalid)


def test_parse_context_carries_request_scoped_idempotency_namespace():
    ctx = ParseContext(
        contract_version=PARSE_CONTRACT_VERSION,
        request_id="req-1",
        attempt_id="att-1",
        run_source_plan_id="rsp-1",
        parser_version="1.0.0",
        normalization_version="norm-v1",
        idempotency_namespace="req-1",
    )
    assert ctx.idempotency_namespace == "req-1"


def test_parse_outcome_records_closure_evidence_and_refs():
    outcome = ParseOutcome(
        kind=ParseOutcomeKind.SUCCESS_EMPTY,
        closure_or_missing_evidence=(
            {"kind": "JOB_CLOSED", "source_job_id": "42", "ref": "result://att-1/body/ab"},
        ),
        evidence_refs=("result://att-1/body/ab",),
        coverage_proposal={"authority": "AUTHORITATIVE_FULL_SOURCE"},
        continuation_required=False,
    )
    assert outcome.contract_version == PARSE_CONTRACT_VERSION
    assert outcome.closure_or_missing_evidence[0]["kind"] == "JOB_CLOSED"
    assert outcome.evidence_refs == ("result://att-1/body/ab",)
    # defaults stay empty tuples rather than shared mutable objects
    assert ParseOutcome(kind=ParseOutcomeKind.SUCCESS_EMPTY).closure_or_missing_evidence == ()


def test_field_evidence_record_carries_spans_and_source_reference():
    record = FieldEvidenceRecord(
        field_name="description",
        locator_kind="json_path",
        locator_value="content",
        value_hash="deadbeef",
        excerpt="Build things",
        evidence_start=10,
        evidence_end=22,
        source_url="https://x.test/jobs/1",
    )
    payload = json.dumps(
        {
            "field": record.field_name,
            "start": record.evidence_start,
            "end": record.evidence_end,
            "source_url": record.source_url,
        }
    )
    assert json.loads(payload) == {
        "field": "description",
        "start": 10,
        "end": 22,
        "source_url": "https://x.test/jobs/1",
    }
    assert ObservationRecord(
        source_job_id="1",
        raw_url="https://x.test/jobs/1",
        canonical_url_candidate=None,
        application_url_candidate=None,
        fields={"title": "T"},
    ).field_evidence == ()
