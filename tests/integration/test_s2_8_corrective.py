"""S2.8 corrective regression tests.

Authority: v0.3.1.3 acquisition §12.1, security §5.1, and the S2.8
cross-provider acceptance package.  These tests pin the two stop-the-line
findings from the 2026-09-10 corrective review:

* a careers-page fingerprint probe MUST already be durable SOURCE_DISCOVERY
  work before the first byte of network I/O; and
* once immutable routing selects a graduated provider adapter, host policy may
  authorize that provider's reviewed API host even when the Source entry URL
  remains the employer-owned careers host.
"""

from __future__ import annotations

from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.pipeline.driver import source_policy

import pytest

NOW = "2026-09-10T05:00:00.000000Z"


@pytest.mark.parametrize(
    ("adapter_id", "expected_api_host", "unrelated_api_host"),
    [
        ("greenhouse", "boards-api.greenhouse.io", "api.lever.co"),
        ("lever", "api.lever.co", "api.ashbyhq.com"),
        ("ashby", "api.ashbyhq.com", "boards-api.greenhouse.io"),
    ],
)
def test_selected_provider_adapter_authorizes_only_its_reviewed_api_host_from_employer_entry(
    adapter_id: str, expected_api_host: str, unrelated_api_host: str
):
    policy = source_policy(
        {"entry_url": "https://careers.acme.example/jobs"},
        adapter_id=adapter_id,
    )
    assert policy.allowed_hosts is not None
    assert "careers.acme.example" in policy.allowed_hosts
    assert expected_api_host in policy.allowed_hosts
    assert unrelated_api_host not in policy.allowed_hosts


def _seed_permission_profile(conn) -> None:
    conn.executescript(
        f"""
        INSERT INTO adapter_permission_profiles (id, display_name, created_at)
        VALUES ('perm-s28', 'S2.8 corrective', '{NOW}');
        INSERT INTO adapter_permission_profile_revisions
            (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-s28', 'perm-s28', 1, '{{}}', '{NOW}');
        """
    )
    conn.commit()


def test_discovery_is_durable_and_claimed_before_executor_io(db):
    """The first careers-page probe is real durable SOURCE_DISCOVERY work.

    The fake executor is the I/O boundary.  Its assertions therefore prove the
    Source, immutable generic-discovery binding revision, request and attempt
    all exist *before* execution starts.  The completed discovery must retain
    fetch/fingerprint/route evidence and must not fabricate enumeration
    coverage for a discovery-only request.
    """
    from jobscraper.pipeline.discovery import discover_source

    conn = db.conn
    _seed_permission_profile(conn)
    entry_url = "https://careers.acme.example/jobs"
    body = (
        b"<!doctype html><html><head>"
        b"<script src='https://jobs.lever.co/acme'></script>"
        b"<link rel='canonical' href='https://jobs.lever.co/acme'>"
        b"</head><body><div class='postings-group' data-qa='posting'></div>"
        b"</body></html>"
    )
    observed = {}

    def fake_execute(envelope, policy):
        # This function stands exactly at the first-I/O boundary.  All durable
        # identities required by §12.1 must already exist here.
        source = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (envelope.source_id,)
        ).fetchone()
        assert source is not None and source["entry_url"] == entry_url

        binding = conn.execute(
            "SELECT * FROM source_adapter_bindings WHERE id = ?",
            (envelope.binding_id,),
        ).fetchone()
        assert binding is not None
        assert binding["current_revision_id"] == envelope.binding_revision_id

        revision = conn.execute(
            "SELECT * FROM source_adapter_binding_revisions WHERE id = ?",
            (envelope.binding_revision_id,),
        ).fetchone()
        assert revision["adapter_id"] == "generic_discovery"
        assert revision["strategy"] == "GENERIC_DISCOVERY"

        request = conn.execute(
            "SELECT * FROM scrape_requests WHERE id = ?", (envelope.request_id,)
        ).fetchone()
        assert request is not None
        assert request["request_type"] == "SOURCE_DISCOVERY"
        assert request["status"] == "RUNNING"
        assert request["source_id"] == envelope.source_id
        assert request["binding_id"] == envelope.binding_id

        attempt = conn.execute(
            "SELECT * FROM request_attempts WHERE attempt_id = ?",
            (envelope.attempt_id,),
        ).fetchone()
        assert attempt is not None
        assert attempt["request_id"] == envelope.request_id

        # Generic discovery itself receives only the source-entry authority.
        assert policy.allowed_hosts == frozenset({"careers.acme.example"})
        observed.update(
            source_id=envelope.source_id,
            binding_id=envelope.binding_id,
            binding_revision_id=envelope.binding_revision_id,
            request_id=envelope.request_id,
            attempt_id=envelope.attempt_id,
        )
        return ResultEnvelope(
            execution_plan_id=envelope.plan_id,
            request_id=envelope.request_id,
            attempt_id=envelope.attempt_id,
            run_source_plan_id=envelope.run_source_plan_id,
            source_id=envelope.source_id,
            binding_id=envelope.binding_id,
            binding_revision_id=envelope.binding_revision_id,
            adapter_id=envelope.adapter_id,
            adapter_version=envelope.adapter_version,
            strategy=envelope.strategy,
            execution_class=envelope.execution_class,
            requested_url=envelope.payload.url,
            final_url=envelope.payload.url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=body,
            bytes_downloaded=len(body),
            fetched_at=NOW,
        ).finalize()

    discovery = discover_source(
        conn,
        display_name="Acme careers",
        source_family="EMPLOYER_CAREERS",
        entry_url=entry_url,
        worker_id="worker-s28",
        now=NOW,
        executor=fake_execute,
    )

    assert observed
    assert discovery.source_id == observed["source_id"]
    assert discovery.binding_id == observed["binding_id"]
    assert discovery.binding_revision_id == observed["binding_revision_id"]
    assert discovery.request_id == observed["request_id"]
    assert discovery.attempt_id == observed["attempt_id"]
    assert discovery.fingerprint.family == "LEVER"
    assert discovery.decision.outcome.value == "SPECIALIZED"

    request = conn.execute(
        "SELECT * FROM scrape_requests WHERE id = ?", (discovery.request_id,)
    ).fetchone()
    assert request["status"] == "SUCCEEDED"
    assert conn.execute(
        "SELECT COUNT(*) FROM fetch_attempts WHERE request_id = ?",
        (discovery.request_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM acquisition_evidence WHERE request_id = ?"
        " AND kind = 'RESULT_ENVELOPE'",
        (discovery.request_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM ats_fingerprints WHERE source_id = ?",
        (discovery.source_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM source_route_decisions WHERE source_id = ?",
        (discovery.source_id,),
    ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM enumeration_coverage").fetchone()[0] == 0
