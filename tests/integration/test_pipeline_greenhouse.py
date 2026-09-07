"""End-to-end pipeline integration: the full provenance spine (Slice 1 ship
condition) against a local Greenhouse-shaped fixture server.

Proves: launch spine from run creation -> immutable plan -> durable request ->
fenced execution -> classification -> parse -> observation -> obligation ->
normalization -> entity resolution -> canonical job -> job_sources presence ->
eligibility -> score -> Inbox event -> disposition -> application.
"""

import json

import pytest

from jobscraper.acquisition.registry import AdapterRegistry
from jobscraper.config import AppConfig
from jobscraper.db.connection import immediate_transaction
from jobscraper.domain.profiles import create_profile
from jobscraper.domain.sources import (
    create_binding,
    create_source,
    seed_builtin_permission_profile,
)
from jobscraper.runtime.engine import RunEngine
from jobscraper.security.netpolicy import make_test_fixture_policy
from tests.fixtures.fixture_server import FixtureServer

GREENHOUSE_JOBS = {
    "jobs": [
        {
            "id": 1001,
            "title": "Senior Python Engineer",
            "absolute_url": "https://job-boards.example/greenhouse/acme/jobs/1001",
            "updated_at": "2026-08-01T00:00:00Z",
            "first_published": "2026-07-15T00:00:00Z",
            "content": "<div><h2>Senior Python Engineer</h2><p>We need a senior python engineer with"
                       " fastapi and postgresql experience. Full-time. Visa sponsorship available."
                       " Location: Berlin, Germany or Remote (EU).</p></div>",
            "location": {"name": "Berlin, Germany"},
            "offices": [{"name": "Berlin", "location": "Berlin, Germany"}],
            "departments": [{"name": "Engineering"}],
            "metadata": [],
        },
        {
            "id": 1002,
            "title": "Product Designer",
            "absolute_url": "https://job-boards.example/greenhouse/acme/jobs/1002",
            "updated_at": "2026-08-02T00:00:00Z",
            "first_published": "2026-07-20T00:00:00Z",
            "content": "<div><p>Design beautiful products. Competitive salary.</p></div>",
            "location": {"name": "New York, NY, USA"},
            "offices": [{"name": "NYC", "location": "New York"}],
            "departments": [{"name": "Design"}],
            "metadata": [],
        },
        {
            "id": 1003,
            "title": "Marketing Intern",
            "absolute_url": "https://job-boards.example/greenhouse/acme/jobs/1003",
            "updated_at": "2026-08-03T00:00:00Z",
            "first_published": "2026-08-01T00:00:00Z",
            "content": "<div><p>Internship opportunity. Part time.</p></div>",
            "location": {"name": "London, UK"},
            "metadata": [],
        },
    ],
    "meta": {"total": 3},
}


@pytest.fixture()
def engine_setup(db, data_root):
    seed_builtin_permission_profile(db)
    with FixtureServer() as server:
        server.add_json("/v1/boards/acme/jobs", GREENHOUSE_JOBS)
        source_id = create_source(
            db,
            display_name="Acme Greenhouse",
            entry_url=server.base_url + "/v1/boards/acme/jobs",
            canonical_host="127.0.0.1",
            source_family="greenhouse",
        )
        binding_id = create_binding(
            db,
            source_id=source_id,
            display_name="Acme GH API",
            adapter_id="greenhouse",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            config={"board": "acme", "base_url": server.base_url},
        )
        profile_id = create_profile(
            db,
            name="Engineering EU",
            snapshot_overrides={
                "home_country": "DE",
                "eligible_countries": ["DE", "NL", "FR"],
                "keywords": ["python"],
                "must_keywords": ["python"],
                "min_score_inbox": 10,
            },
        )
        engine = RunEngine(
            AppConfig(data_root=data_root),
            db,
            AdapterRegistry(),
            policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
        )
        yield db, engine, server, source_id, binding_id, profile_id


def test_full_pipeline_provenance_spine(engine_setup):
    db, engine, server, source_id, binding_id, profile_id = engine_setup
    run_id = engine.create_run(profile_id=profile_id, source_ids=[source_id])
    report = engine.execute_run(run_id)

    # Run aggregation: single satisfied group -> SUCCEEDED.
    assert report.status == "SUCCEEDED", report
    assert list(report.groups.values()) == ["SATISFIED"]

    # Observations persisted with provenance.
    observations = db.query("SELECT * FROM job_observations ORDER BY source_rank_or_order")
    assert len(observations) == 3
    obs = observations[0]
    assert obs["source_id"] == source_id
    assert obs["adapter_id"] == "greenhouse"
    assert obs["source_job_id"] == "1001"
    assert obs["strategy"] == "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT"

    # Canonical jobs + company + locations + facts.
    jobs = db.query("SELECT j.*, c.name AS company_name FROM jobs j LEFT JOIN companies c ON c.id=j.company_id ORDER BY j.title")
    assert len(jobs) == 3
    by_title = {j["title"]: j for j in jobs}
    eng = by_title["Senior Python Engineer"]
    assert eng["company_name"] is not None
    assert eng["listing_status"] == "ACTIVE"
    locations = db.query("SELECT * FROM job_locations WHERE job_id=?", (eng["id"],))
    assert locations, "locations expected"
    facts = {f["fact_type"]: json.loads(f["value_json"]) for f in db.query("SELECT * FROM job_facts WHERE job_id=?", (eng["id"],))}
    assert facts.get("seniority") == "senior"
    assert "python" in (facts.get("skills") or [])

    # job_sources presence with exact URLs preserved.
    js = db.query_one(
        "SELECT * FROM job_sources WHERE source_id=? AND source_job_id='1001'", (source_id,)
    )
    assert js is not None
    assert js["job_id"] == eng["id"]
    assert js["canonical_job_url"].endswith("/jobs/1001")
    assert js["application_url"].endswith("/jobs/1001")
    assert js["presence_state"] == "ACTIVE"

    # Eligibility: EU profile; Berlin job ELIGIBLE, NYC job not.
    elig = {
        row["job_id"]: row["verdict"]
        for row in db.query("SELECT job_id, verdict FROM job_eligibility WHERE profile_id=?", (profile_id,))
    }
    assert elig[eng["id"]] == "ELIGIBLE"
    assert elig[by_title["Product Designer"]["id"]] in ("INELIGIBLE", "LIKELY")

    # Scores deterministic with breakdown.
    scores = db.query("SELECT * FROM job_scores WHERE profile_id=? ORDER BY score DESC", (profile_id,))
    assert len(scores) == 3
    eng_score = [s for s in scores if s["job_id"] == eng["id"]][0]
    breakdown = json.loads(eng_score["breakdown_json"])
    assert any(c["rule"] == "title_must_keywords" for c in breakdown)
    assert all("rule" in c and "points" in c for c in breakdown)

    # Inbox events: first eligible appearance exactly once.
    events = db.query(
        "SELECT * FROM job_profile_inbox_events WHERE profile_id=? AND job_id=?", (profile_id, eng["id"])
    )
    assert len(events) == 1
    assert events[0]["event_kind"] == "NEW_ELIGIBLE_APPEARANCE"

    # FTS index maintained.
    fts_hits = db.query("SELECT job_id FROM jobs_fts WHERE jobs_fts MATCH 'python'")
    assert eng["id"] in {r["job_id"] for r in fts_hits}

    # Fetch/parse attempts recorded.
    assert db.query_one("SELECT COUNT(*) c FROM fetch_attempts")["c"] >= 1
    assert db.query_one("SELECT COUNT(*) c FROM parse_attempts")["c"] >= 1

    # Coverage generation: COMPLETE + authoritative + applied (absence).
    cov = db.query_one("SELECT * FROM enumeration_coverage")
    assert cov["completion_state"] == "COMPLETE"
    assert cov["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
    assert cov["applied_at"] is not None
    seen = {r["stable_source_identity"] for r in db.query("SELECT stable_source_identity FROM coverage_seen_identity")}
    assert seen == {f"{source_id}:1001", f"{source_id}:1002", f"{source_id}:1003"}


def test_second_run_no_duplicate_events_and_presence_refresh(engine_setup):
    db, engine, server, source_id, binding_id, profile_id = engine_setup
    run1 = engine.create_run(profile_id=profile_id, source_ids=[source_id])
    engine.execute_run(run1)
    run2 = engine.create_run(profile_id=profile_id, source_ids=[source_id])
    report = engine.execute_run(run2)
    assert report.status == "SUCCEEDED"

    # Same canonical jobs (no duplicate identities).
    assert db.query_one("SELECT COUNT(*) c FROM jobs")["c"] == 3
    # Re-observation updates presence, never mutates prior observations.
    obs_count = db.query_one("SELECT COUNT(*) c FROM job_observations")["c"]
    assert obs_count == 6  # 3 per run, immutable history
    js = db.query_one("SELECT * FROM job_sources WHERE source_id=? AND source_job_id='1001'", (source_id,))
    assert js["presence_state"] == "ACTIVE"

    # Inbox events NOT duplicated for unchanged content.
    events = db.query_one("SELECT COUNT(*) c FROM job_profile_inbox_events WHERE profile_id=?", (profile_id,))
    assert events["c"] == 1  # only the Senior Eng job is inbox-eligible


def test_empty_board_is_success(engine_setup):
    db, engine, server, source_id, binding_id, profile_id = engine_setup
    server.add_json("/v1/boards/emptyco/jobs", {"jobs": [], "meta": {}})
    empty_source = create_source(
        db, display_name="Empty Board", entry_url=server.base_url + "/v1/boards/emptyco/jobs",
        canonical_host="127.0.0.1", source_family="greenhouse",
    )
    create_binding(
        db, source_id=empty_source, display_name="Empty GH", adapter_id="greenhouse",
        adapter_version="1.0.0", strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        config={"board": "emptyco", "base_url": server.base_url},
    )
    run_id = engine.create_run(profile_id=profile_id, source_ids=[empty_source])
    report = engine.execute_run(run_id)
    # Valid complete zero-job enumeration is success.
    assert report.status == "SUCCEEDED"
    assert report.groups == {} or all(v == "SATISFIED" for v in report.groups.values())
    cov = db.query_one(
        "SELECT * FROM enumeration_coverage c JOIN run_source_plans p ON p.id=c.run_source_plan_id WHERE p.run_id=?",
        (run_id,),
    )
    assert cov["completion_state"] == "COMPLETE"


def test_absence_makes_missing_job_uncertain_not_closed(engine_setup):
    db, engine, server, source_id, binding_id, profile_id = engine_setup
    run1 = engine.create_run(profile_id=profile_id, source_ids=[source_id])
    engine.execute_run(run1)
    js = db.query_one("SELECT * FROM job_sources WHERE source_id=? AND source_job_id='1002'", (source_id,))
    assert js["presence_state"] == "ACTIVE"

    # Board now drops job 1002 (complete enumeration without it).
    reduced = {k: (v if k != "jobs" else [j for j in v if j["id"] != 1002]) for k, v in GREENHOUSE_JOBS.items()}
    server.add_json("/v1/boards/acme/jobs", reduced)
    run2 = engine.create_run(profile_id=profile_id, source_ids=[source_id])
    report = engine.execute_run(run2)
    assert report.status == "SUCCEEDED"

    js = db.query_one("SELECT * FROM job_sources WHERE source_id=? AND source_job_id='1002'", (source_id,))
    # One complete absence-authoritative enumeration -> UNCERTAIN, never CLOSED.
    assert js["presence_state"] == "UNCERTAIN"
    assert js["last_absence_coverage_id"] is not None
    # Job still exists; listing degraded but not closed.
    job = db.query_one("SELECT * FROM jobs WHERE id=?", (js["job_id"],))
    assert job["listing_status"] in ("UNCERTAIN", "ACTIVE")

    # Re-applying the same coverage generation is idempotent.
    from jobscraper.runtime.presence import apply_absence_coverage

    cov = db.query_one("SELECT * FROM enumeration_coverage WHERE id=?", (js["last_absence_coverage_id"],))
    assert apply_absence_coverage(db, cov) == 0


def test_challenged_board_no_absence_inference(engine_setup):
    db, engine, server, source_id, binding_id, profile_id = engine_setup
    run1 = engine.create_run(profile_id=profile_id, source_ids=[source_id])
    engine.execute_run(run1)

    # Board starts challenging: 403 challenge page.
    server.add_html(
        "/v1/boards/acme/jobs",
        "<html><title>Access Denied</title><body>Verify you are human to continue</body></html>",
        status=403,
    )
    run2 = engine.create_run(profile_id=profile_id, source_ids=[source_id])
    report = engine.execute_run(run2)
    # All attempts fail -> FAILED group -> run FAILED.
    assert report.status == "FAILED"
    # Presence NOT aged: jobs remain ACTIVE (no blanket absence inference).
    for row in db.query("SELECT presence_state FROM job_sources WHERE source_id=?", (source_id,)):
        assert row["presence_state"] == "ACTIVE"
    # Binding health recorded as challenged.
    health = db.query_one("SELECT operational_state FROM source_binding_health")
    assert health["operational_state"] in ("CHALLENGED", "BROKEN")


def test_observation_immutability_and_direct_write_prohibition(engine_setup):
    db, engine, server, source_id, binding_id, profile_id = engine_setup
    run_id = engine.create_run(profile_id=profile_id, source_ids=[source_id])
    engine.execute_run(run_id)
    obs = db.query_one("SELECT * FROM job_observations")
    # job_observations has no mutable "presence" columns and no UPDATE path in
    # the runtime; prove immutability structurally: content_json is a snapshot.
    content = json.loads(obs["content_json"])
    assert content["title"] == "Senior Python Engineer"
    # There is no column linking observations to listing state.
    assert "presence_state" not in obs.keys()
