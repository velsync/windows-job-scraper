"""Slice 3 S3.0 contract gate (VER-14 / ROAD-04).

S3.0 freezes the additive v3 cross-component shape before later Slice-3
runtime/crawler packages integrate against it. This file intentionally tests
contracts and boundaries only; it does not authorize S3.1+ behavior.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import (
    ACQ02_REQUEST_TASK_MAP,
    CONTRACT_VERSION,
    HOST_NATIVE_REQUEST_TYPE_NAMES,
    PARSE_CONTRACT_VERSION,
    AdapterTaskKind,
    DiscoveredTask,
    ParseContext,
    ParseOutcome,
    ParseOutcomeKind,
    PlanningContext,
    ValidatedResultEnvelope,
    task_kind_for_request_type,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "slice3_contract_v3.json").read_text(
        encoding="utf-8"
    )
)


def _field_names(cls) -> set[str]:
    return {item.name for item in fields(cls)}


def _result() -> ResultEnvelope:
    return ResultEnvelope(
        execution_plan_id="plan-s3",
        request_id="req-s3",
        attempt_id="att-s3",
        run_source_plan_id="rsp-s3",
        source_id="src-s3",
        binding_id="bnd-s3",
        binding_revision_id="bndrev-s3",
        adapter_id="fixture",
        adapter_version="3.0.0",
        strategy="HTTP_HTML",
        execution_class="HTTP",
        requested_url="https://jobs.example.test/jobs",
        final_url="https://jobs.example.test/jobs",
        status_code=200,
        content_type="application/json",
        body=b'{"jobs": []}',
    ).finalize()


def test_contract_v3_is_frozen_before_cross_component_slice3_work():
    assert FIXTURE["contract_version"] == 3
    assert CONTRACT_VERSION == PARSE_CONTRACT_VERSION
    assert CONTRACT_VERSION >= FIXTURE["contract_version"]

    planning = PlanningContext(**FIXTURE["planning_context"])
    assert planning.contract_version == 3
    assert {
        "run_id",
        "run_source_plan_id",
        "source_snapshot_ref",
        "binding_revision_id",
        "permission_profile_revision",
        "policy_snapshot_ref",
        "query_revision_ref",
        "budget_snapshot_ref",
    } <= _field_names(PlanningContext)

    parse = ParseContext(**FIXTURE["parse_context"])
    assert parse.contract_version == 3
    assert parse.parser_version == "3.0.0"
    assert parse.recipe_version_id is None
    assert parse.normalization_contract_version == "norm-v1"
    # Slice-2 callers may still use the compatibility name while v3 consumers
    # have an explicit normative normalization-contract field.
    assert parse.normalization_version == parse.normalization_contract_version
    assert parse.idempotency_namespace == "req-s3"

    child = DiscoveredTask(**FIXTURE["discovered_task"])
    assert child.parent_reference == "observation://obs-42"


def test_parse_context_v3_is_additive_for_legacy_callers_and_fail_closed_on_conflict():
    # Preserve the complete v2 positional constructor prefix. New v3 fields
    # append rather than shifting normalization_version/idempotency_namespace.
    legacy = ParseContext(3, "req", "att", "rsp", "parser", "norm-v1", "req")
    assert legacy.normalization_version == "norm-v1"
    assert legacy.normalization_contract_version == "norm-v1"
    assert legacy.recipe_version_id is None
    assert legacy.idempotency_namespace == "req"

    canonical = ParseContext(normalization_contract_version="norm-v2")
    assert canonical.normalization_contract_version == "norm-v2"
    assert canonical.normalization_version == "norm-v2"

    with pytest.raises(ValueError, match="must identify the same contract"):
        ParseContext(
            normalization_version="norm-v1",
            normalization_contract_version="norm-v2",
        )


def test_all_acquisition_task_kinds_are_fixture_locked_and_host_native_is_non_network():
    assert [kind.value for kind in AdapterTaskKind] == FIXTURE["task_kinds"]
    assert set(ACQ02_REQUEST_TASK_MAP.values()) == set(AdapterTaskKind)
    assert HOST_NATIVE_REQUEST_TYPE_NAMES == frozenset(
        FIXTURE["host_native_request_types"]
    )
    for request_type in FIXTURE["host_native_request_types"]:
        assert task_kind_for_request_type(request_type) is None


def test_validated_result_envelope_v3_keeps_the_page_validity_gate():
    locked = FIXTURE["validated_result_envelope"]
    validated = ValidatedResultEnvelope(
        envelope=_result(),
        page_class=PageClass(locked["validated_page_class"]),
        validation_evidence=locked["validation_evidence"],
        contract_version=locked["contract_version"],
        security_policy_result=locked["security_policy_result"],
        cache_representation_ref=locked["cache_representation_ref"],
    )
    assert validated.contract_version == 3
    assert validated.validated_page_class is PageClass.EMPTY
    assert validated.result_envelope_ref is not None
    assert validated.validation_evidence_ref is not None


def test_parse_outcome_fixture_covers_partial_closure_and_terminal_no_work():
    terminal = ParseOutcome(
        contract_version=FIXTURE["contract_version"],
        kind=ParseOutcomeKind(FIXTURE["terminal_no_work"]["kind"]),
        coverage_proposal=FIXTURE["coverage_proposal"],
        continuation_required=FIXTURE["terminal_no_work"]["continuation_required"],
    )
    assert terminal.kind is ParseOutcomeKind.SUCCESS_EMPTY
    assert terminal.discovered_tasks == ()
    assert terminal.continuation_required is False

    child = DiscoveredTask(**FIXTURE["discovered_task"])
    partial = ParseOutcome(
        contract_version=FIXTURE["contract_version"],
        kind=ParseOutcomeKind(FIXTURE["partial"]["kind"]),
        discovered_tasks=(child,),
        coverage_proposal=FIXTURE["coverage_proposal"],
        continuation_required=FIXTURE["partial"]["continuation_required"],
    )
    assert partial.kind is ParseOutcomeKind.PARTIAL
    assert partial.discovered_tasks == (child,)
    assert partial.continuation_required is True

    non_job = ParseOutcome(
        contract_version=FIXTURE["contract_version"],
        kind=ParseOutcomeKind.FAILURE,
        closure_or_missing_evidence=tuple(FIXTURE["closure_or_missing_evidence"]),
    )
    assert [item["kind"] for item in non_job.closure_or_missing_evidence] == [
        "JOB_CLOSED",
        "NOT_FOUND",
    ]
    assert non_job.observations == ()
