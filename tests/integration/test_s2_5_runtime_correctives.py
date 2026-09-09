"""Corrective regressions for S2.5 listing coverage and child-work recovery."""

from __future__ import annotations

import http.server
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    ParseOutcomeKind,
    ValidatedResultEnvelope,
)
from jobscraper.adapters.greenhouse import GreenhouseAdapter, GreenhouseConfig
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline import driver as driver_module
from jobscraper.pipeline.driver import execute_run
from jobscraper.search.provision import provision_search


# Reuse only the deterministic Greenhouse fixture server + host-owned run
# builders from the accepted S2.5 E2E corpus.  The module is loaded under a
# non-test name, so pytest does not duplicate its test collection here.
_HELPERS_PATH = Path(__file__).with_name("test_greenhouse_e2e.py")
_SPEC = importlib.util.spec_from_file_location("_s25_greenhouse_helpers", _HELPERS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_gh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_gh)


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _gh._GreenhouseHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def db(tmp_path, server):
    database = Database(tmp_path / "s25-runtime-correctives.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    provision_search(database.conn, now=_gh.NOW)
    yield database
    database.close()


def _validated_list(payload: dict) -> ValidatedResultEnvelope:
    url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    envelope = ResultEnvelope(
        execution_plan_id="plan-runtime-corrective",
        request_id="req-runtime-corrective",
        attempt_id="att-runtime-corrective",
        run_source_plan_id="rsp-runtime-corrective",
        source_id="src-runtime-corrective",
        binding_id="bnd-runtime-corrective",
        binding_revision_id="bndrev-runtime-corrective",
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
        page_class=PageClass.VALID_LIST,
        validation_evidence={"fixture": "mixed-membership-corrective"},
    )


def test_mixed_valid_and_invalid_membership_is_partial_not_absence_authority():
    """One rejected member means the listing did not prove the whole set."""
    adapter = GreenhouseAdapter(
        GreenhouseConfig(board="acme", detail_fetch=False)
    )
    outcome = adapter.parse(
        AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={}),
        _validated_list(
            {
                "jobs": [
                    {
                        "id": 4001,
                        "title": "Backend Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/4001",
                    },
                    {
                        "id": "../passwd",
                        "title": "Unusable Identity",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/evil",
                    },
                ],
                "meta": {"total": 2},
            }
        ),
    )

    assert outcome.kind is ParseOutcomeKind.PARTIAL
    assert len(outcome.observations) == 1
    assert outcome.observations[0].source_job_id == "4001"
    assert any(e.get("reason") == "REQUIRED_FIELD_MISSING" for e in outcome.review_evidence)
    assert outcome.coverage_proposal["rejected_members"] == 1


def test_complete_listing_coverage_survives_detail_parse_failures(db, server):
    """DETAIL quality can degrade the run without rewriting listing truth."""
    run_id, _ = _gh._run_board(db, server, board="idmismatch")

    assert execute_run(db.conn, run_id) == "PARTIAL"
    coverage = _gh._coverage(db)
    assert len(coverage) == 1
    assert coverage[0]["completion_state"] == "COMPLETE"
    assert coverage[0]["terminal_enumeration_proven"] == 1
    assert coverage[0]["pages_completed"] == 1
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"] == "SATISFIED_PARTIAL"


def test_same_run_restart_drains_pending_details_without_refetching_listing(db, server):
    """A clean listing is durable truth; restart resumes only accepted DETAIL work."""
    provisioned = _gh._provision(db, server, board="acme")
    run_id = _gh._start_run(db, provisioned, board="acme")

    original_budget = driver_module.MAX_DETAIL_REQUESTS_PER_RUN
    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0
    try:
        assert execute_run(db.conn, run_id) == "PARTIAL"
    finally:
        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original_budget

    first_requests = _gh._requests(db, run_id)
    assert [r["request_type"] for r in first_requests] == ["LIST_FETCH"] + [
        "DETAIL_FETCH"
    ] * 3
    assert [r["status"] for r in first_requests] == ["SUCCEEDED"] + ["PENDING"] * 3

    # Enumeration coverage is already complete even though accepted enrichment
    # work still keeps the run itself partial.
    first_coverage = _gh._coverage(db)
    assert len(first_coverage) == 1
    assert first_coverage[0]["completion_state"] == "COMPLETE"
    assert first_coverage[0]["terminal_enumeration_proven"] == 1
    assert first_coverage[0]["pages_completed"] == 1
    listing_id = first_requests[0]["id"]
    listing_fetches = db.conn.execute(
        "SELECT COUNT(*) FROM fetch_attempts WHERE request_id = ?", (listing_id,)
    ).fetchone()[0]
    assert listing_fetches == 1

    # Re-drive the SAME run.  The durable COMPLETE listing generation is reused
    # as truth; only the already-accepted child work is drained.
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    final_requests = _gh._requests(db, run_id)
    assert [r["request_type"] for r in final_requests] == ["LIST_FETCH"] + [
        "DETAIL_FETCH"
    ] * 3
    assert all(r["status"] == "SUCCEEDED" for r in final_requests)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM fetch_attempts WHERE request_id = ?", (listing_id,)
    ).fetchone()[0] == 1

    final_coverage = _gh._coverage(db)
    assert len(final_coverage) == 1
    assert final_coverage[0]["id"] == first_coverage[0]["id"]
    assert final_coverage[0]["completion_state"] == "COMPLETE"
    assert final_coverage[0]["pages_completed"] == 1
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"] == "SATISFIED"
