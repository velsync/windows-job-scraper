"""S2.2 integration: companies, multi-location, canonical provenance selection.

Two sources see the same posting through different-quality routes, and the
tests pin what 01 §33/§38/§39 requires of the pipeline:

* the employer-side structured presence owns the canonical presentation,
  while the aggregator presence stays fully inspectable (presentation, not
  deletion);
* the resolved origin identity rolls up onto ``jobs`` from the *winning
  presence*, and the derived location set, remote mode, employment type and
  experience level follow the same winner's evidence;
* one company row serves both sources, keyed by strong identifiers only, with
  every decision (including a refused name-only merge) recorded;
* §38 stage 2 (shared origin identity across sources) attaches a presence
  rather than merging jobs, and refuses to attach on a meaningful location
  disagreement;
* re-running the same evidence is idempotent: no duplicate companies,
  identifiers, locations, or observations.
"""

from __future__ import annotations

import json

import pytest

from jobscraper.acquisition.origin import OriginStatus, resolve_origin
from jobscraper.adapters.contract import ObservationRecord
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.ingest import ingest_observation
from jobscraper.pipeline.provenance import (
    AGGREGATOR_WITH_RESOLVED_ORIGIN,
    EMPLOYER_STRUCTURED_API,
    EMPLOYER_STRUCTURED_ATS,
)
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T09:00:00.000000Z"
LATER = "2026-09-08T11:00:00.000000Z"

ATS_JOB_URL = "https://boards.greenhouse.io/acme/jobs/1234567"
EMPLOYER_JOB_URL = "https://careers.acme.example/jobs/1234567"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "provenance.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    database.conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-employer','Acme Careers','EMPLOYER_CAREERS','https://careers.acme.example/jobs',
                '{NOW}', '{NOW}');
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-agg','Job Board Aggregator','PUBLIC_BOARD','https://aggregator.example.test/feed',
                '{NOW}', '{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}','{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-employer','src-employer','api','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-agg','src-agg','api','{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, created_at)
        VALUES ('bndrev-employer','bnd-employer',1,'json_api_feed','1.0.0',
                'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,'{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, created_at)
        VALUES ('bndrev-agg','bnd-agg',1,'json_api_feed','1.0.0',
                'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,'{NOW}');
        """
    )
    database.conn.commit()
    # Model the service lifetime: the coordinator opens its service epoch
    # before any claiming (03 §50; claims fail closed without one).
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    yield database
    database.close()


def _claim(db, *, source_id: str, binding_id: str, now: str = NOW):
    plan = dict(
        source_id=source_id,
        source_plan_group_id=f"grp-{source_id}",
        fallback_rank=0,
        binding_id=binding_id,
        binding_revision_id=f"bndrev-{binding_id.split('-')[1]}",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
    )
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=now)
    rid, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plans[0],
        source_id=source_id,
        binding_id=binding_id,
        request_type="LIST_FETCH",
        target_identity=f"https://x.test/{source_id}?page=1",
        logical_key=f'{{"page":1,"src":"{source_id}"}}',
    )
    claim = claim_next_request(db.conn, f"worker-{source_id}", now=now)
    assert claim is not None and claim.request_id == rid
    return claim


def _observation(
    *,
    source_job_id,
    title,
    company,
    urls,
    locations,
    employment=None,
    experience=None,
    careers_url=None,
):
    return ObservationRecord(
        source_job_id=source_job_id,
        raw_url=urls[0],
        canonical_url_candidate=urls[0],
        application_url_candidate=urls[1] if len(urls) > 1 else urls[0],
        fields={
            "title": title,
            "company": company,
            "description": "<p>Owns our ingestion pipeline.</p>",
            "locations": list(locations),
            "employment_type": employment,
            "experience_level": experience,
            "careers_url": careers_url,
        },
    )


def _ingest(db, *, claim, source_id, binding_id, observation, content_kind, at=NOW):
    """Ingest one observation exactly as the driver would.

    The §39 employer-vs-aggregator flag is produced by the driver's own helper
    (imported, not reimplemented here) from the recorded source + observation.
    """
    from jobscraper.pipeline.driver import posting_host_matches_source

    source_row = db.conn.execute(
        "SELECT * FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    origin = resolve_origin(
        discovery_url={
            "src-employer": "https://careers.acme.example/jobs",
            "src-agg": "https://aggregator.example.test/feed",
        }[source_id],
        raw_source_url=observation.raw_url,
        canonical_job_url=observation.canonical_url_candidate,
        application_url=observation.application_url_candidate,
        redirect_chain=[],
        final_url=observation.raw_url,
        observed_at=at,
    )
    return ingest_observation(
        db.conn,
        request_id=claim.request_id,
        attempt_id=claim.attempt_id,
        observation=observation,
        source_id=source_id,
        binding_id=binding_id,
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        observed_at=at,
        now=at,
        content_kind=content_kind,
        origin=origin,
        same_host_as_source=posting_host_matches_source(source_row, observation),
        source_family=source_row["source_family"],
    )


def _employer_observation(**overrides):
    kwargs = dict(
        source_job_id="acme-1234567",
        title="Staff Backend Engineer",
        company="Acme Data",
        urls=[EMPLOYER_JOB_URL, ATS_JOB_URL],
        locations=["Berlin, Germany / Paris, France"],
        employment="Full-time",
        experience="Senior",
        careers_url="https://careers.acme.example/jobs",
    )
    kwargs.update(overrides)
    return _observation(**kwargs)


def _aggregator_observation(**overrides):
    kwargs = dict(
        source_job_id="AGG-77",
        title="Staff Backend Engineer @ Acme Data",
        company="Acme Data GmbH",
        urls=[ATS_JOB_URL, ATS_JOB_URL + "?utm_source=aggregator"],
        locations=["Berlin"],
    )
    kwargs.update(overrides)
    return _observation(**kwargs)


def test_both_sources_land_on_one_canonical_job_with_the_employer_presenting(db):
    employer_claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=employer_claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    agg_claim = _claim(db, source_id="src-agg", binding_id="bnd-agg")
    _ingest(
        db,
        claim=agg_claim,
        source_id="src-agg",
        binding_id="bnd-agg",
        observation=_aggregator_observation(),
        content_kind="STRUCTURED",
        at=LATER,
    )
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    assert job["title"] == "Staff Backend Engineer"
    assert db.conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 2
    winner = db.conn.execute(
        "SELECT source_id FROM job_sources WHERE id = ?", (job["canonical_provenance_id"],)
    ).fetchone()
    assert winner["source_id"] == "src-employer"
    assert {
        r[0] for r in db.conn.execute("SELECT source_id FROM job_sources")
    } == {"src-employer", "src-agg"}


def test_quality_class_is_recorded_per_presence_from_its_own_evidence(db):
    claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    presence = db.conn.execute(
        "SELECT * FROM job_sources WHERE source_id = 'src-employer'"
    ).fetchone()
    assert presence["source_quality_class"] == EMPLOYER_STRUCTURED_ATS
    assert presence["content_kind"] == "STRUCTURED"
    assert presence["same_host_as_source"] == 1

    agg_claim = _claim(db, source_id="src-agg", binding_id="bnd-agg")
    _ingest(
        db,
        claim=agg_claim,
        source_id="src-agg",
        binding_id="bnd-agg",
        observation=_aggregator_observation(),
        content_kind="STRUCTURED",
    )
    agg = db.conn.execute(
        "SELECT * FROM job_sources WHERE source_id = 'src-agg'"
    ).fetchone()
    assert agg["source_quality_class"] == AGGREGATOR_WITH_RESOLVED_ORIGIN
    assert agg["same_host_as_source"] == 0


def test_origin_identity_and_categorical_projection_roll_up_from_the_winner(db):
    claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    assert job["origin_provider"] == "GREENHOUSE"
    assert job["origin_board"] == "acme"
    assert job["origin_job_id"] == "1234567"
    assert job["employment_type"] == "FULL_TIME"
    assert job["experience_level"] == "SENIOR"
    assert job["remote_mode"] == "UNSPECIFIED"
    assert job["remote_worldwide"] == 0
    assert job["provenance_selector_version"] == "canonical-provenance-selector-v2"
    assert job["location_rules_version"] == "location-rules-v1"
    assert job["company_resolution_version"] == "company-resolution-v2"
    rows = db.conn.execute(
        "SELECT * FROM job_locations WHERE job_id = ? ORDER BY raw_text", (job["id"],)
    ).fetchall()
    assert [r["city"] for r in rows] == ["Berlin", "Paris"]
    assert [r["country"] for r in rows] == ["DE", "FR"]
    assert all(r["confidence"] >= 0.8 for r in rows)
    assert all(r["remote"] == 0 for r in rows)


def test_one_company_serves_both_sources_through_strong_identifiers_only(db):
    claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    agg_claim = _claim(db, source_id="src-agg", binding_id="bnd-agg")
    _ingest(
        db,
        claim=agg_claim,
        source_id="src-agg",
        binding_id="bnd-agg",
        observation=_aggregator_observation(),
        content_kind="STRUCTURED",
    )
    companies = db.conn.execute("SELECT * FROM companies").fetchall()
    assert len(companies) == 1
    assert companies[0]["ats_provider"] == "GREENHOUSE"
    assert companies[0]["ats_board"] == "acme"
    assert companies[0]["domain"] == "careers.acme.example"
    kinds = {
        (r["kind"], r["value"])
        for r in db.conn.execute("SELECT kind, value FROM company_identifiers")
    }
    assert {k for k, _ in kinds} == {"ATS_BOARD", "CAREERS_HOST"}
    assert ("ATS_BOARD", "GREENHOUSE/acme") in kinds
    assert ("CAREERS_HOST", "careers.acme.example") in kinds
    assert ("APP_HOST", "boards.greenhouse.io") not in kinds
    events = db.conn.execute(
        "SELECT * FROM company_resolution_events ORDER BY created_at, id"
    ).fetchall()
    assert [e["decision"] for e in events] == ["CREATED", "ATTACHED"]
    assert all(e["observation_id"] for e in events)
    assert companies[0]["name"] == "Acme Data"


def test_meaningful_location_disagreement_does_not_attach(db):
    claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    agg_claim = _claim(db, source_id="src-agg", binding_id="bnd-agg")
    _ingest(
        db,
        claim=agg_claim,
        source_id="src-agg",
        binding_id="bnd-agg",
        observation=_aggregator_observation(locations=["Amsterdam, Netherlands"]),
        content_kind="STRUCTURED",
    )
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    event = db.conn.execute(
        "SELECT * FROM entity_resolution_events ORDER BY decided_at DESC, id DESC LIMIT 1"
    ).fetchone()
    assert event["decision"] == "CREATED"
    assert event["stage"] == "origin_identity_stage2"
    assert event["reason_code"] == "LOCATION_DISAGREEMENT"
    evidence = json.loads(event["evidence_json"])
    assert evidence["stage"] == "origin_identity"
    assert evidence["location_conflict"] is True
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_matching_origin_identity_without_conflict_attaches_the_presence(db):
    claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    agg_claim = _claim(db, source_id="src-agg", binding_id="bnd-agg")
    result = _ingest(
        db,
        claim=agg_claim,
        source_id="src-agg",
        binding_id="bnd-agg",
        observation=_aggregator_observation(
            source_job_id="AGG-999",
            urls=["https://aggregator.example.test/jobs/AGG-999", ATS_JOB_URL],
            locations=["Paris, France"],
        ),
        content_kind="STRUCTURED",
    )
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    presence = db.conn.execute(
        "SELECT source_quality_class, source_identity_generation FROM job_sources"
        " WHERE source_id = 'src-agg'"
    ).fetchone()
    assert presence["source_quality_class"] == AGGREGATOR_WITH_RESOLVED_ORIGIN
    assert presence["source_identity_generation"] == 1
    assert result["decision"] in ("MATCHED_ORIGIN", "MATCHED", "MATCHED_URL")
    event = db.conn.execute(
        "SELECT * FROM entity_resolution_events ORDER BY decided_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if result["decision"] == "MATCHED_ORIGIN":
        assert event["stage"] == "origin_identity_stage2"


def test_employer_only_feed_without_an_ats_origin_is_structured_api_quality(db):
    claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(
            urls=["https://careers.acme.example/jobs/plain-1"],
            locations=["Remote — EMEA"],
        ),
        content_kind="STRUCTURED",
    )
    presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    assert presence["source_quality_class"] == EMPLOYER_STRUCTURED_API
    assert presence["origin_provider"] is None
    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    assert job["origin_provider"] is None
    location = db.conn.execute(
        "SELECT * FROM job_locations WHERE job_id = ?", (job["id"],)
    ).fetchone()
    assert location["remote"] == 1
    assert location["country"] is None


def test_replaying_the_same_evidence_is_idempotent(db):
    claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    first = _ingest(
        db,
        claim=claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    replay = _ingest(
        db,
        claim=claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    assert replay["idempotent"] is True
    counts = {
        table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "jobs",
            "job_sources",
            "companies",
            "company_identifiers",
            "job_locations",
            "company_resolution_events",
        )
    }
    assert counts == {
        "jobs": 1,
        "job_sources": 1,
        "companies": 1,
        "company_identifiers": 2,
        "job_locations": 2,
        "company_resolution_events": 1,
    }
    assert replay["job_id"] is None
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_observations"
    ).fetchone()[0] == 1
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_origin_resolution_status_is_durable_evidence_for_the_class(db):
    claim = _claim(db, source_id="src-agg", binding_id="bnd-agg")
    _ingest(
        db,
        claim=claim,
        source_id="src-agg",
        binding_id="bnd-agg",
        observation=_aggregator_observation(),
        content_kind="STRUCTURED",
    )
    presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    detail = json.loads(presence["origin_resolution_evidence_json"])
    assert detail["status"] == OriginStatus.RESOLVED.value
    assert detail["origin_provider"] == "GREENHOUSE"
    assert detail["confidence"] >= 0.85
    evidence = db.conn.execute(
        "SELECT detail_json FROM acquisition_evidence WHERE kind = 'ORIGIN_RESOLUTION'"
    ).fetchone()
    assert json.loads(evidence["detail_json"])["endpoint_rules_version"] == 1


def test_a_late_lower_quality_presence_reasserts_the_winning_projection(db):
    """§39/§38: canonical presentation is the *winner's*, restated every refresh."""
    employer_claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=employer_claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    agg_claim = _claim(db, source_id="src-agg", binding_id="bnd-agg")
    _ingest(
        db,
        claim=agg_claim,
        source_id="src-agg",
        binding_id="bnd-agg",
        observation=_aggregator_observation(locations=[]),
        content_kind="STRUCTURED",
        at=LATER,
    )

    db.conn.execute("UPDATE jobs SET title = 'DRIFTED'")
    db.conn.execute("DELETE FROM job_locations")
    db.conn.commit()

    second_agg_claim = _claim(db, source_id="src-agg", binding_id="bnd-agg", now=LATER)
    _ingest(
        db,
        claim=second_agg_claim,
        source_id="src-agg",
        binding_id="bnd-agg",
        observation=_aggregator_observation(locations=[], title="Staff Backend Engineer @ Acme Data"),
        content_kind="STRUCTURED",
        at=LATER,
    )

    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    assert job["title"] == "Staff Backend Engineer"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_locations WHERE job_id = ?", (job["id"],)
    ).fetchone()[0] == 2
    history = db.conn.execute(
        "SELECT change_class FROM job_history WHERE job_id = ? ORDER BY rowid", (job["id"],)
    ).fetchall()
    assert [row[0] for row in history] == ["TITLE_CHANGED", "LOCATION_CHANGED"]
    assert {
        row[0] for row in db.conn.execute("SELECT source_id FROM job_sources")
    } == {"src-employer", "src-agg"}


def test_projection_is_left_alone_when_the_winner_has_no_retained_payload(db):
    """No retained evidence means no re-projection — never an invention."""
    from jobscraper.pipeline.canonical import _latest_observation_projection

    employer_claim = _claim(db, source_id="src-employer", binding_id="bnd-employer")
    _ingest(
        db,
        claim=employer_claim,
        source_id="src-employer",
        binding_id="bnd-employer",
        observation=_employer_observation(),
        content_kind="STRUCTURED",
    )
    presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    assert _latest_observation_projection(db.conn, presence) is not None

    db.conn.execute("UPDATE job_observations SET raw_payload_ref = NULL")
    db.conn.execute("UPDATE job_observations SET raw_payload_ref = 'not json' ")
    db.conn.commit()
    presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    assert _latest_observation_projection(db.conn, presence) is None

# ---------------------------------------------------------------------------
# S2.5 — a name-less sighting must not fabricate (or crash on) a company
# ---------------------------------------------------------------------------


def test_strong_identifiers_without_a_name_never_create_a_company(db):
    """The shape a provider-native board produces before a name is known.

    A Greenhouse sighting resolves an ATS board identity (§32) but the board
    API itself does not state the employer's name.  Creating a company would
    mean inventing one; crashing would mean one honest observation takes the
    whole fenced commit down.  Neither happens: the decision is recorded, no
    row is created, and the identifiers stay available for the sighting that
    does carry a name.
    """
    from jobscraper.pipeline.companies import CompanySignals, resolve_company

    nameless = CompanySignals(ats_provider="GREENHOUSE", ats_board="acme")
    resolution = resolve_company(
        db.conn,
        signals=nameless,
        observed_at=NOW,
        now=NOW,
    )
    assert resolution.company_id is None
    assert resolution.decision == "NO_SIGNAL"
    assert resolution.reason_code == "IDENTIFIERS_WITHOUT_NAME"
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    event = db.conn.execute(
        "SELECT * FROM company_resolution_events ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert event["decision"] == "NO_SIGNAL"
    assert json.loads(event["signals_json"])["ats_board"] == "acme"

    # the next sighting that does carry the employer name creates the company
    named = CompanySignals(
        name="Acme Fixtures", ats_provider="GREENHOUSE", ats_board="acme"
    )
    created = resolve_company(db.conn, signals=named, observed_at=LATER, now=LATER)
    assert created.decision == "CREATED"
    assert created.company_id is not None
    company = db.conn.execute(
        "SELECT * FROM companies WHERE id = ?", (created.company_id,)
    ).fetchone()
    assert company["name"] == "Acme Fixtures"
    assert company["ats_provider"] == "GREENHOUSE"
    assert company["ats_board"] == "acme"
    identifiers = db.conn.execute(
        "SELECT kind, value FROM company_identifiers WHERE company_id = ?",
        (created.company_id,),
    ).fetchall()
    assert [(row["kind"], row["value"]) for row in identifiers] == [
        ("ATS_BOARD", "GREENHOUSE/acme")
    ]

    # and a later name-less sighting from the same board attaches to it
    attached = resolve_company(
        db.conn, signals=nameless, observed_at=LATER, now=LATER
    )
    assert attached.decision == "ATTACHED"
    assert attached.company_id == created.company_id
    assert attached.matched_on == "ATS_BOARD"
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
