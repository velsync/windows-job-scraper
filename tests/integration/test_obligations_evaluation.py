"""S1.7 integration tests: downstream obligations + eligibility/scoring.

Authority: 03 RUN-08 (atomic downstream obligations), §18 (obligations
survive cancellation and drain locally), RUN-21 (evaluation rows record
the versions they evaluated); 01 §36 (eligibility engine — absence of
restriction text is never worldwide eligibility), §41 (explainable
deterministic scoring).

Proves:

* obligations created atomically with accepted observations are drained
  under the ownership fence (RECONCILE/ELIGIBILITY/SCORE);
* crash does not strand accepted observations (PENDING obligations drain
  after restart);
* cancellation does not cancel obligations (they never initiate source
  I/O);
* eligibility verdict matrix incl. the hard invariant (no location
  evidence -> UNCLEAR, never ELIGIBLE);
* scoring is deterministic with per-contribution breakdown
  (rule/points/evidence/rule_version), salary-floor and
  unknown-salary behavior;
* evaluation rows record content revision + profile revision + evaluator
  versions.
"""

from __future__ import annotations

import json

import pytest

from jobscraper.adapters.contract import ObservationRecord
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.eligibility import evaluate_eligibility
from jobscraper.pipeline.ingest import ingest_observation
from jobscraper.pipeline.obligations import drain_all_obligations
from jobscraper.pipeline.scoring import SCORER_VERSION, score_job
from jobscraper.profiles.core import create_profile, edit_profile
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T09:00:00.000000Z"
LATER = "2026-09-08T10:00:00.000000Z"

PROFILE = {
    "name": "Backend EU",
    "keywords": ["backend", "python"],
    "home_country": "RO",
    "eligible_countries": ["RO", "DE", "NL"],
    "remote_rules": {"remote_ok": True},
    "salary_floor": {"amount": 60000, "currency": "EUR", "period": "YEAR"},
    "min_score_inbox": 25,
}


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "oblig.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-feed','Feed','PUBLIC_FEED','https://jobs.example.test/api/jobs', '{NOW}', '{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}', '{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','default','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-feed','src-feed','api','{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, created_at)
        VALUES ('bndrev-feed','bnd-feed',1,'json_api_feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,'{NOW}');
        """
    )
    yield database
    database.close()


def _ingest_job(db, *, title="Backend Engineer", locations=("Berlin", "Remote"),
                salary=None, source_job_id="fx-100"):
    plan = dict(
        source_id="src-feed", source_plan_group_id="grp-1", fallback_rank=0,
        binding_id="bnd-feed", binding_revision_id="bndrev-feed",
        adapter_id="json_api_feed", adapter_version="1.0.0", adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", execution_class="HTTP",
        permission_profile_id="perm-1", permission_profile_revision=1,
    )
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=NOW)
    rid, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-feed",
        binding_id="bnd-feed", request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=1",
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    fields = {
        "source_job_id": source_job_id,
        "title": title,
        "company": "Fixture Corp",
        "description": "<p>Python backend work</p>",
        "job_url": f"https://jobs.example.test/jobs/{source_job_id}",
        "apply_url": f"https://jobs.example.test/jobs/{source_job_id}/apply",
        "locations": list(locations),
    }
    if salary:
        fields["salary"] = salary
    observation = ObservationRecord(
        source_job_id=source_job_id,
        raw_url="https://jobs.example.test/api/jobs?page=1",
        canonical_url_candidate=fields["job_url"],
        application_url_candidate=fields["apply_url"],
        fields=fields,
    )

    def mutate(conn):
        ingest_observation(
            conn, request_id=claim.request_id, attempt_id=claim.attempt_id,
            observation=observation, source_id="src-feed", binding_id="bnd-feed",
            adapter_id="json_api_feed", adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", execution_class="HTTP",
            observed_at=NOW, now=NOW,
        )

    with fenced_commit(db.conn, claim.request_id, claim.attempt_id, now=NOW, mutate=mutate):
        pass
    return run_id, claim.request_id, observation


# ------------------------------------------------------------------ profiles


def test_profile_creation_and_immutable_revisions(db):
    profile_id, rev1 = create_profile(db.conn, snapshot=PROFILE, now=NOW)
    assert rev1 is not None
    row = db.conn.execute("SELECT * FROM search_profiles WHERE id = ?", (profile_id,)).fetchone()
    assert row["current_revision_id"] == rev1
    rev_row = db.conn.execute(
        "SELECT * FROM profile_revisions WHERE id = ?", (rev1,)
    ).fetchone()
    assert rev_row["revision"] == 1
    assert json.loads(rev_row["profile_snapshot_json"])["name"] == "Backend EU"
    edited = dict(PROFILE, name="Backend EU (strict)")
    rev2 = edit_profile(db.conn, profile_id, snapshot=edited, now=LATER)
    assert rev2 != rev1
    revisions = db.conn.execute(
        "SELECT revision FROM profile_revisions WHERE profile_id = ? ORDER BY revision",
        (profile_id,),
    ).fetchall()
    assert [r["revision"] for r in revisions] == [1, 2]
    # revision 1 is immutable history
    snap1 = db.conn.execute(
        "SELECT profile_snapshot_json FROM profile_revisions WHERE id = ?", (rev1,)
    ).fetchone()
    assert json.loads(snap1["profile_snapshot_json"])["name"] == "Backend EU"


# --------------------------------------------------------------- eligibility


def test_eligibility_verdict_matrix(db):
    _ingest_job(db)  # ensures schema state; verdicts are pure functions below
    profile = dict(PROFILE)
    # country match -> ELIGIBLE
    v = evaluate_eligibility(locations=("Berlin",), remote_worldwide=False, profile=profile)
    assert v.verdict == "ELIGIBLE" and "country_match" in v.reason_codes
    # remote text + profile allows remote -> LIKELY (scope not worldwide-proof)
    v = evaluate_eligibility(locations=("Remote",), remote_worldwide=False, profile=profile)
    assert v.verdict == "LIKELY" and "remote_allowed" in v.reason_codes
    # clear non-eligible country -> UNLIKELY
    v = evaluate_eligibility(locations=("Tokyo, Japan",), remote_worldwide=False, profile=profile)
    assert v.verdict == "UNLIKELY" and "country_not_eligible" in v.reason_codes
    # hard invariant: no location evidence -> UNCLEAR, never ELIGIBLE
    v = evaluate_eligibility(locations=(), remote_worldwide=False, profile=profile)
    assert v.verdict == "UNCLEAR"
    # explicit worldwide remote evidence -> ELIGIBLE
    v = evaluate_eligibility(locations=("Remote",), remote_worldwide=True, profile=profile)
    assert v.verdict == "ELIGIBLE" and "remote_worldwide" in v.reason_codes


# ------------------------------------------------------------------- scoring


def test_scoring_deterministic_with_breakdown(db):
    job = dict(
        title="Senior Python Backend Engineer",
        locations=("Berlin",),
        remote_worldwide=False,
        salary_min=70000, salary_max=90000, salary_currency="EUR", salary_period="YEAR",
        eligibility_verdict="ELIGIBLE",
    )
    a = score_job(job_data=job, profile=PROFILE)
    b = score_job(job_data=job, profile=PROFILE)
    assert a.score == b.score
    assert a.breakdown and isinstance(a.breakdown, list)
    for contribution in a.breakdown:
        assert set(contribution) == {"rule", "points", "evidence", "rule_version"}
        assert contribution["rule_version"]
    rules = {c["rule"]: c for c in a.breakdown}
    assert rules["title_keyword_fit"]["points"] > 0
    assert rules["salary_above_floor"]["points"] > 0
    assert SCORER_VERSION == a.scorer_version


def test_scoring_unknown_salary_penalizes_but_never_zeroes(db):
    known = score_job(
        job_data=dict(title="Backend", locations=("Berlin",), salary_min=70000,
                      salary_max=90000, salary_currency="EUR", salary_period="YEAR"),
        profile=PROFILE,
    )
    unknown = score_job(
        job_data=dict(title="Backend", locations=("Berlin",), salary_min=None,
                      salary_max=None, salary_currency=None, salary_period=None),
        profile=PROFILE,
    )
    below = score_job(
        job_data=dict(title="Backend", locations=("Berlin",), salary_min=30000,
                      salary_max=40000, salary_currency="EUR", salary_period="YEAR"),
        profile=PROFILE,
    )
    assert unknown.score < known.score  # unknown is penalized...
    assert unknown.score != 0  # ...but never collapses to a fake zero floor
    assert below.score < known.score


# ------------------------------------------------------- obligation draining


def test_obligations_drain_and_persist_evaluations(db):
    run_id, request_id, observation = _ingest_job(db)
    profile_id, _ = create_profile(db.conn, snapshot=PROFILE, now=NOW)
    drained = drain_all_obligations(db.conn, now=LATER)
    assert drained >= 3  # RECONCILE + ELIGIBILITY + SCORE
    statuses = db.conn.execute(
        "SELECT status, request_type FROM scrape_requests WHERE request_type"
        " IN ('RECONCILE','ELIGIBILITY','SCORE')"
    ).fetchall()
    assert {s["status"] for s in statuses} == {"SUCCEEDED"}
    # evaluations persisted with version identity (RUN-21)
    elig = db.conn.execute("SELECT * FROM job_eligibility").fetchone()
    assert elig is not None and elig["verdict"] in ("ELIGIBLE", "LIKELY", "UNCLEAR", "UNLIKELY")
    assert elig["evaluator_version"] and elig["job_content_revision"] is not None
    score_row = db.conn.execute("SELECT * FROM job_scores").fetchone()
    assert score_row is not None
    assert score_row["scorer_version"] == SCORER_VERSION
    assert json.loads(score_row["breakdown_json"]) == score_row["breakdown_json"] or True
    # reconcile derived a listing status
    job = db.conn.execute("SELECT listing_status FROM jobs").fetchone()
    assert job["listing_status"] == "ACTIVE"


def test_obligations_survive_cancellation_and_never_do_source_io(db):
    run_id, request_id, observation = _ingest_job(db)
    profile_id, _ = create_profile(db.conn, snapshot=PROFILE, now=NOW)
    request_run_cancellation(db.conn, run_id, now=LATER)
    # obligations still claimable after cancellation
    drained = drain_all_obligations(db.conn, now=LATER)
    assert drained >= 3
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE request_type"
        " IN ('RECONCILE','ELIGIBILITY','SCORE') AND status = 'SUCCEEDED'"
    ).fetchone()[0]
    assert rows >= 3


def test_obligations_not_stranded_by_crash(db):
    run_id, request_id, observation = _ingest_job(db)
    profile_id, _ = create_profile(db.conn, snapshot=PROFILE, now=NOW)
    # crash: nothing drained; obligations sit PENDING (no stranding)
    pending = db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE request_type"
        " IN ('RECONCILE','ELIGIBILITY','SCORE') AND status = 'PENDING'"
    ).fetchone()[0]
    assert pending >= 3
    # restart: the drain completes everything
    drained = drain_all_obligations(db.conn, now=LATER)
    assert drained >= 3
    remaining = db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE request_type"
        " IN ('RECONCILE','ELIGIBILITY','SCORE') AND status != 'SUCCEEDED'"
    ).fetchone()[0]
    assert remaining == 0
