"""S2.7 corrective regression: HEALTH/SMOKE must respect Ashby apiVersion.

The deterministic Ashby v2 fixture is structurally recognizable but carries a
provider contract stamp the adapter has not been reviewed against.  ENUMERATE
already returns PARTIAL for that document; HEALTH/SMOKE must not report it as
healthy/recognized success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.ashby import ADAPTER_ID, ADAPTER_VERSION, AshbyAdapter, AshbyConfig
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    ParseOutcomeKind,
    ValidatedResultEnvelope,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ashby"
BOARD = "acme"
LIST_URL = (
    "https://api.ashbyhq.com/posting-api/job-board/acme"
    "?includeCompensation=true"
)


def _validated_v2() -> ValidatedResultEnvelope:
    envelope = ResultEnvelope(
        execution_plan_id="plan-s27-corrective",
        request_id="req-s27-corrective",
        attempt_id="att-s27-corrective",
        run_source_plan_id="rsp-s27-corrective",
        source_id="src-s27-corrective",
        binding_id="bnd-s27-corrective",
        binding_revision_id="bndrev-s27-corrective",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        requested_url=LIST_URL,
        final_url=LIST_URL,
        status_code=200,
        content_type="application/json",
        body=(FIXTURES / "board_jobs_api_v2.json").read_bytes(),
    ).finalize()
    return ValidatedResultEnvelope(
        envelope=envelope,
        page_class=PageClass.VALID_LIST,
        validation_evidence={"fixture": "board_jobs_api_v2.json"},
    )


@pytest.mark.parametrize("kind", [AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE])
def test_unreviewed_api_version_is_partial_for_probe_tasks(kind: AdapterTaskKind) -> None:
    adapter = AshbyAdapter(AshbyConfig(board=BOARD, company_name="Acme Fixtures"))
    outcome = adapter.parse(AdapterTask(kind=kind, payload={}), _validated_v2())

    assert outcome.kind is ParseOutcomeKind.PARTIAL
    assert outcome.failure is None
    assert outcome.observations == ()
    assert outcome.discovered_tasks == ()
    assert outcome.evidence_refs

    version_evidence = [
        item for item in outcome.review_evidence
        if item.get("reason") == "UNRECOGNIZED_API_VERSION"
    ]
    assert version_evidence == [
        {"reason": "UNRECOGNIZED_API_VERSION", "api_version": "2"}
    ]
    assert not any(
        item.get("reason") == "HEALTH_PROBE_RECOGNIZED"
        for item in outcome.review_evidence
    )
