"""Corrective regressions for the production Greenhouse Job Board contract."""

from __future__ import annotations

import json

from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import AdapterTask, AdapterTaskKind, ValidatedResultEnvelope
from jobscraper.adapters.greenhouse import GreenhouseAdapter, GreenhouseConfig
from jobscraper.pipeline.contentclean import clean
from jobscraper.pipeline.driver import source_policy


def _official_detail_result(payload: dict) -> ValidatedResultEnvelope:
    url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4001"
    envelope = ResultEnvelope(
        execution_plan_id="plan-corrective",
        request_id="req-corrective",
        attempt_id="att-corrective",
        run_source_plan_id="rsp-corrective",
        source_id="src-corrective",
        binding_id="bnd-corrective",
        binding_revision_id="bndrev-corrective",
        adapter_id="greenhouse",
        adapter_version="1.0.0",
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type="application/json",
        body=json.dumps(payload).encode("utf-8"),
    ).finalize()
    return ValidatedResultEnvelope(
        envelope=envelope,
        page_class=PageClass.VALID_JOB,
        validation_evidence={"fixture": "official-contract-shape"},
    )


def test_host_policy_authorizes_reviewed_greenhouse_api_host_not_arbitrary_config():
    """Provider API authority comes from reviewed host code, never config content."""
    source = {"entry_url": "https://boards.greenhouse.io/acme"}
    policy = source_policy(source, adapter_id="greenhouse")

    assert policy.allowed_hosts == frozenset(
        {"boards.greenhouse.io", "boards-api.greenhouse.io"}
    )
    assert "evil.example" not in policy.allowed_hosts

    # An operator-pinned alternate origin may be syntactically valid to the
    # adapter, but it is not network authority unless host policy separately
    # approves it.
    config = GreenhouseConfig(board="acme", api_base_url="https://evil.example")
    assert config.api_base_url == "https://evil.example"
    assert "evil.example" not in policy.allowed_hosts


def test_current_public_detail_fields_supply_company_and_first_published():
    adapter = GreenhouseAdapter(GreenhouseConfig(board="acme"))
    payload = {
        "id": 4001,
        "title": "Backend Engineer",
        "company_name": "Acme Fixtures",
        "first_published": "2026-08-18T06:00:00Z",
        "updated_at": "2026-08-20T09:15:00-04:00",
        "location": {"name": "Berlin, Germany"},
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/4001",
        "content": "&lt;p&gt;Build durable Python services.&lt;/p&gt;",
    }
    outcome = adapter.parse(
        AdapterTask(kind=AdapterTaskKind.DETAIL, payload={"target_reference": "4001"}),
        _official_detail_result(payload),
        ctx=None,
    )

    observation = outcome.observations[0]
    assert observation.fields["company"] == "Acme Fixtures"
    assert observation.fields["posted_at"] == "2026-08-18T06:00:00.000000Z"
    locators = {(e.field_name, e.locator_value, e.locator_kind) for e in observation.field_evidence}
    assert ("company", "company_name", "json_path") in locators
    assert ("posted_at", "first_published", "json_path") in locators


def test_entity_encoded_greenhouse_html_is_decoded_then_sanitized_once():
    encoded = (
        "&lt;div&gt;&lt;p&gt;Build &amp;amp; operate services.&lt;/p&gt;"
        "&lt;script&gt;alert(1)&lt;/script&gt;&lt;/div&gt;"
    )
    cleaned = clean(encoded)

    assert cleaned.markdown
    assert cleaned.text
    assert "Build &amp; operate services." in cleaned.text
    assert "&lt;" not in cleaned.text
    assert "<script" not in cleaned.text.lower()
    assert "alert(1)" not in cleaned.text
