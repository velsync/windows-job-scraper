"""Regression tests for S2.6 Lever HEALTH/SMOKE contract recognition.

Authority: v0.3.1.3 acquisition/adapters contract. HEALTH and SMOKE are
recognition-only tasks: they must never emit observations or child work, but
they also must not report a malformed postings array as an unqualified healthy
provider contract.
"""

from __future__ import annotations

import json

import pytest

from jobscraper.acquisition.failures import FailureKind
from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    ParseOutcomeKind,
    ValidatedResultEnvelope,
)
from jobscraper.adapters.lever import ADAPTER_ID, ADAPTER_VERSION, LeverAdapter, LeverConfig

BOARD = "acme"
API = "https://api.lever.co"
LIST_URL = f"{API}/v0/postings/{BOARD}?mode=json&skip=0&limit=100"


def _posting(index: int) -> dict:
    posting_id = f"aaaaaaaa-0000-4000-8000-{index:012d}"
    return {
        "id": posting_id,
        "text": f"Role {index}",
        "categories": {"location": "Berlin, Germany"},
        "createdAt": 1786698000000,
        "workplaceType": "on-site",
        "hostedUrl": f"https://jobs.lever.co/{BOARD}/{posting_id}",
        "applyUrl": f"https://jobs.lever.co/{BOARD}/{posting_id}/apply",
        "description": f"<p>Body {index}</p>",
        "lists": [],
        "additional": "",
    }


def _validated(payload) -> ValidatedResultEnvelope:
    body = json.dumps(payload).encode("utf-8")
    envelope = ResultEnvelope(
        execution_plan_id="plan-probe",
        request_id="req-probe",
        attempt_id="att-probe",
        run_source_plan_id="rsp-probe",
        source_id="src-probe",
        binding_id="bnd-probe",
        binding_revision_id="bndrev-probe",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        requested_url=LIST_URL,
        final_url=LIST_URL,
        status_code=200,
        content_type="application/json",
        body=body,
    ).finalize()
    return ValidatedResultEnvelope(
        envelope=envelope,
        page_class=PageClass.VALID_LIST,
        validation_evidence={"fixture": "inline-health-corrective"},
    )


def _probe(kind: AdapterTaskKind, payload):
    return LeverAdapter(LeverConfig(board=BOARD)).parse(
        AdapterTask(kind=kind), _validated(payload), ctx=None
    )


@pytest.mark.parametrize("kind", [AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE])
def test_probe_rejects_nonempty_array_when_no_member_matches_minimum_posting_contract(kind):
    outcome = _probe(
        kind,
        [
            {"garbage": 1},
            {"categories": {"location": "Berlin, Germany"}},
            "junk",
        ],
    )

    assert outcome.kind is ParseOutcomeKind.FAILURE
    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.PARSE_MARKER_MISSING
    assert outcome.observations == ()
    assert outcome.discovered_tasks == ()
    assert outcome.evidence_refs


@pytest.mark.parametrize("kind", [AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE])
def test_probe_marks_mixed_valid_and_invalid_members_partial_without_emitting_work(kind):
    outcome = _probe(kind, [_posting(1), {"garbage": 1}])

    assert outcome.kind is ParseOutcomeKind.PARTIAL
    assert outcome.failure is None
    assert outcome.observations == ()
    assert outcome.discovered_tasks == ()
    assert outcome.evidence_refs
    summary = next(
        evidence for evidence in outcome.review_evidence
        if evidence.get("reason") == "HEALTH_PROBE_PARTIAL_RECOGNITION"
    )
    assert summary["listed_postings"] == 2
    assert summary["recognized_postings"] == 1
    assert summary["rejected_postings"] == 1


@pytest.mark.parametrize("kind", [AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE])
def test_probe_recognizes_valid_members_without_emitting_observations_or_tasks(kind):
    outcome = _probe(kind, [_posting(1), _posting(2)])

    assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
    assert outcome.failure is None
    assert outcome.observations == ()
    assert outcome.discovered_tasks == ()
    assert outcome.evidence_refs
    assert any(
        evidence.get("reason") == "HEALTH_PROBE_RECOGNIZED"
        and evidence.get("listed_postings") == 2
        and evidence.get("recognized_postings") == 2
        and evidence.get("rejected_postings") == 0
        for evidence in outcome.review_evidence
    )


@pytest.mark.parametrize("kind", [AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE])
def test_probe_keeps_empty_array_as_recognized_empty_contract(kind):
    outcome = _probe(kind, [])

    assert outcome.kind is ParseOutcomeKind.SUCCESS_EMPTY
    assert outcome.failure is None
    assert outcome.observations == ()
    assert outcome.discovered_tasks == ()
    assert outcome.evidence_refs
    assert any(
        evidence.get("reason") == "HEALTH_PROBE_RECOGNIZED"
        and evidence.get("listed_postings") == 0
        and evidence.get("recognized_postings") == 0
        and evidence.get("rejected_postings") == 0
        for evidence in outcome.review_evidence
    )
