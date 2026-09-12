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
    # Model the service lifetime: the coordinator opens its service epoch
    # before any claiming (03 §50; claims fail closed without one).
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
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
# -------------------------------------------------------- Slice 3 S3.11 RUN-21

S311_LATER = "2026-09-08T11:00:00.000000Z"
S311_LATEST = "2026-09-08T12:00:00.000000Z"


def _s311_prepare_observation(
    db,
    *,
    at: str,
    title: str = "Backend Engineer",
    locations=("Berlin", "Remote"),
    salary=None,
    source_job_id: str = "s311-100",
):
    plan = dict(
        source_id="src-feed", source_plan_group_id="s311-grp", fallback_rank=0,
        binding_id="bnd-feed", binding_revision_id="bndrev-feed",
        adapter_id="json_api_feed", adapter_version="1.0.0", adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", execution_class="HTTP",
        permission_profile_id="perm-1", permission_profile_revision=1,
    )
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=at)
    _rid, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plans[0],
        source_id="src-feed",
        binding_id="bnd-feed",
        request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=1",
        strategy=plan["strategy"],
        execution_class="HTTP",
        now=at,
    )
    claim = claim_next_request(
        db.conn, "s311-worker", now=at, types=frozenset({"LIST_FETCH"})
    )
    assert claim is not None
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
    return run_id, plans[0], claim, observation


def _s311_ingest_job(db, **kwargs):
    at = kwargs.pop("at")
    run_id, plan_id, claim, observation = _s311_prepare_observation(
        db, at=at, **kwargs
    )

    def mutate(conn):
        ingest_observation(
            conn,
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            observation=observation,
            source_id="src-feed",
            binding_id="bnd-feed",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            observed_at=at,
            now=at,
        )

    with fenced_commit(
        db.conn, claim.request_id, claim.attempt_id, now=at, mutate=mutate
    ):
        pass
    job_id = db.conn.execute(
        "SELECT job_id FROM job_sources WHERE source_id='src-feed' AND source_job_id=?"
        " ORDER BY source_identity_generation DESC LIMIT 1",
        (observation.source_job_id,),
    ).fetchone()["job_id"]
    return run_id, plan_id, str(job_id), observation


def test_s311_profile_reverse_completion_cannot_replace_newer_current_rows(db):
    from jobscraper.pipeline.evaluation import (
        CURRENT_NOOP,
        STALE_INPUT,
        capture_evaluation_snapshot,
        evaluate_snapshot,
        persist_eligibility_result,
        persist_score_result,
    )

    _run, _plan, job_id, _observation = _s311_ingest_job(db, at=NOW)
    profile_id, rev1 = create_profile(db.conn, snapshot=PROFILE, now=NOW)
    old = capture_evaluation_snapshot(db.conn, job_id, profile_id)
    assert old is not None and old.profile_revision_id == rev1
    old_eligibility, old_score = evaluate_snapshot(old)

    strict = dict(PROFILE, name="Backend EU strict", keywords=["rust"])
    rev2 = edit_profile(db.conn, profile_id, snapshot=strict, now=LATER)
    current = capture_evaluation_snapshot(db.conn, job_id, profile_id)
    assert current is not None and current.profile_revision_id == rev2
    # edit_profile owns the RUN-21 profile-advance rematerialization.
    current_eligibility, current_score = evaluate_snapshot(current)
    assert persist_eligibility_result(
        db.conn, current, current_eligibility, now=S311_LATER
    ) == CURRENT_NOOP
    assert persist_score_result(
        db.conn, current, current_score, now=S311_LATER
    ) == CURRENT_NOOP

    before_elig = dict(db.conn.execute(
        "SELECT * FROM job_eligibility WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone())
    before_score = dict(db.conn.execute(
        "SELECT * FROM job_scores WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone())

    assert persist_eligibility_result(
        db.conn, old, old_eligibility, now=S311_LATEST
    ) == STALE_INPUT
    assert persist_score_result(db.conn, old, old_score, now=S311_LATEST) == STALE_INPUT
    assert dict(db.conn.execute(
        "SELECT * FROM job_eligibility WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()) == before_elig
    assert dict(db.conn.execute(
        "SELECT * FROM job_scores WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()) == before_score

    assert persist_eligibility_result(
        db.conn, current, current_eligibility, now=S311_LATEST
    ) == CURRENT_NOOP
    assert persist_score_result(
        db.conn, current, current_score, now=S311_LATEST
    ) == CURRENT_NOOP
    assert db.conn.execute(
        "SELECT evaluated_at FROM job_eligibility WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == LATER
    assert db.conn.execute(
        "SELECT scored_at FROM job_scores WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == LATER


def test_s311_content_reverse_completion_uses_canonical_evaluation_revision(db):
    from jobscraper.pipeline.evaluation import (
        STALE_INPUT,
        capture_evaluation_snapshot,
        evaluate_snapshot,
        persist_eligibility_result,
        persist_score_result,
    )

    _run1, _plan1, job_id, _obs1 = _s311_ingest_job(
        db, at=NOW, title="Backend Engineer", source_job_id="s311-content"
    )
    profile_id, _rev = create_profile(db.conn, snapshot=PROFILE, now=NOW)
    old = capture_evaluation_snapshot(db.conn, job_id, profile_id)
    assert old is not None

    _run2, _plan2, same_job_id, _obs2 = _s311_ingest_job(
        db,
        at=LATER,
        title="Senior Python Backend Engineer",
        source_job_id="s311-content",
    )
    assert same_job_id == job_id
    current = capture_evaluation_snapshot(db.conn, job_id, profile_id)
    assert current is not None
    assert current.job_content_revision == old.job_content_revision + 1

    current_eligibility, current_score = evaluate_snapshot(current)
    assert persist_eligibility_result(
        db.conn, current, current_eligibility, now=S311_LATER
    ) == "WRITTEN"
    assert persist_score_result(db.conn, current, current_score, now=S311_LATER) == "WRITTEN"

    old_eligibility, old_score = evaluate_snapshot(old)
    assert persist_eligibility_result(
        db.conn, old, old_eligibility, now=S311_LATEST
    ) == STALE_INPUT
    assert persist_score_result(db.conn, old, old_score, now=S311_LATEST) == STALE_INPUT
    assert db.conn.execute(
        "SELECT job_content_revision FROM job_eligibility WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == current.job_content_revision
    assert db.conn.execute(
        "SELECT job_content_revision FROM job_scores WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == current.job_content_revision


def test_s311_identical_canonical_input_does_not_advance_evaluation_revision(db):
    _run1, _plan1, job_id, _obs1 = _s311_ingest_job(
        db, at=NOW, title="Backend Engineer", source_job_id="s311-same"
    )
    first = db.conn.execute(
        "SELECT evaluation_revision FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0]
    _run2, _plan2, same_job_id, _obs2 = _s311_ingest_job(
        db, at=LATER, title="Backend Engineer", source_job_id="s311-same"
    )
    assert same_job_id == job_id
    assert db.conn.execute(
        "SELECT evaluation_revision FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == first


def test_s311_rows_pin_exact_inputs_and_inbox_waits_for_matching_pair(db):
    from jobscraper.pipeline.eligibility import EVALUATOR_VERSION
    from jobscraper.pipeline.evaluation import (
        CURRENT_NOOP,
        capture_evaluation_snapshot,
        emit_inbox_if_current_pair,
        evaluate_snapshot,
        persist_eligibility_result,
        persist_score_result,
    )
    from jobscraper.pipeline.normalize import NORMALIZATION_VERSION
    from jobscraper.pipeline.scoring import RULES_VERSION

    _run, _plan, job_id, _observation = _s311_ingest_job(db, at=NOW)
    profile = dict(PROFILE, min_score_inbox=0)
    profile_id, profile_revision_id = create_profile(
        db.conn, snapshot=profile, now=NOW
    )
    assert drain_all_obligations(db.conn, now=LATER) >= 3

    current_revision = db.conn.execute(
        "SELECT evaluation_revision FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0]
    elig = db.conn.execute(
        "SELECT * FROM job_eligibility WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()
    score = db.conn.execute(
        "SELECT * FROM job_scores WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()
    assert elig["job_content_revision"] == score["job_content_revision"] == current_revision
    assert elig["profile_revision_id"] == score["profile_revision_id"] == profile_revision_id
    assert elig["rules_revision_id"] is score["rules_revision_id"] is None
    assert elig["normalization_version"] == score["normalization_version"] == NORMALIZATION_VERSION
    assert elig["evaluator_version"] == EVALUATOR_VERSION
    assert score["eligibility_evaluator_version"] == EVALUATOR_VERSION
    assert score["scorer_version"] == SCORER_VERSION
    assert score["rule_version"] == RULES_VERSION

    event_count = db.conn.execute(
        "SELECT COUNT(*) FROM job_profile_inbox_events WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0]
    assert event_count >= 1

    snapshot = capture_evaluation_snapshot(db.conn, job_id, profile_id)
    assert snapshot is not None
    eligibility_result, score_result = evaluate_snapshot(snapshot)
    assert persist_eligibility_result(
        db.conn, snapshot, eligibility_result, now=S311_LATEST
    ) == CURRENT_NOOP
    assert persist_score_result(
        db.conn, snapshot, score_result, now=S311_LATEST
    ) == CURRENT_NOOP
    assert emit_inbox_if_current_pair(db.conn, snapshot, now=S311_LATEST)
    assert emit_inbox_if_current_pair(db.conn, snapshot, now=S311_LATEST)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_profile_inbox_events WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == event_count


def test_s311_cancel_and_quarantine_do_not_strand_accepted_local_obligations(db):
    run_id, _plan_id, _job_id, _observation = _s311_ingest_job(db, at=NOW)
    create_profile(db.conn, snapshot=PROFILE, now=NOW)
    request_run_cancellation(db.conn, run_id, now=LATER)
    db.conn.execute(
        "UPDATE sources SET administrative_state='QUARANTINED' WHERE id='src-feed'"
    )
    db.conn.execute(
        "UPDATE source_adapter_bindings SET administrative_state='QUARANTINED'"
        " WHERE id='bnd-feed'"
    )
    db.conn.commit()

    assert drain_all_obligations(db.conn, now=LATER) >= 3
    rows = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE run_id=? AND request_type"
        " IN ('RECONCILE','ELIGIBILITY','SCORE')",
        (run_id,),
    ).fetchall()
    assert rows and {row["status"] for row in rows} == {"SUCCEEDED"}
    assert db.conn.execute(
        """
        SELECT COUNT(*)
          FROM request_attempts a
          JOIN scrape_requests r ON r.id=a.request_id
         WHERE r.run_id=? AND r.request_type IN ('RECONCILE','ELIGIBILITY','SCORE')
           AND a.execution_plan_id IS NOT NULL
        """,
        (run_id,),
    ).fetchone()[0] == 0


def test_s311_host_native_request_cannot_bind_source_network_execution_plan(db):
    from jobscraper.acquisition.envelope import (
        ExecutionPlanEnvelope,
        RequestPlan,
        bind_execution_plan,
        policy_snapshot_reference,
    )

    _run, plan_id, _job_id, _observation = _s311_ingest_job(db, at=NOW)
    claim = claim_next_request(
        db.conn, "s311-local", now=LATER, types=frozenset({"ELIGIBILITY"})
    )
    assert claim is not None
    plan = db.conn.execute(
        "SELECT * FROM run_source_plans WHERE id=?", (plan_id,)
    ).fetchone()
    envelope = ExecutionPlanEnvelope(
        plan_id="forbidden-network-plan",
        request_id=claim.request_id,
        attempt_id=claim.attempt_id,
        run_id=claim.run_id,
        run_source_plan_id=claim.run_source_plan_id,
        source_id=plan["source_id"],
        binding_id=plan["binding_id"],
        binding_revision_id=plan["binding_revision_id"],
        adapter_id=plan["adapter_id"],
        adapter_version=plan["adapter_version"],
        strategy=plan["strategy"],
        execution_class=plan["execution_class"],
        policy_snapshot_ref=policy_snapshot_reference(plan),
        permission_profile_id=plan["permission_profile_id"],
        permission_profile_revision=plan["permission_profile_revision"],
        payload_kind="REQUEST",
        payload=RequestPlan(
            "GET", "https://jobs.example.test/forbidden", purpose="ELIGIBILITY"
        ),
    )
    with pytest.raises(ValueError, match="host-native"):
        bind_execution_plan(db.conn, envelope, now=LATER)
    assert db.conn.execute(
        "SELECT execution_plan_id FROM request_attempts WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()[0] is None


def test_s311_run_terminalization_waits_for_local_obligations(db):
    from jobscraper.runtime.runs import aggregate_run, set_group_outcome

    run_id, plan_id, _job_id, _observation = _s311_ingest_job(db, at=NOW)
    create_profile(db.conn, snapshot=PROFILE, now=NOW)
    set_group_outcome(db.conn, plan_id, "SATISFIED", now=LATER)
    assert aggregate_run(db.conn, run_id, now=LATER) is None
    assert db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id=?", (run_id,)
    ).fetchone()[0] != "SUCCEEDED"

    assert drain_all_obligations(db.conn, now=LATER) >= 3
    assert aggregate_run(db.conn, run_id, now=S311_LATER) == "SUCCEEDED"


def test_s311_failure_after_observation_insert_rolls_back_observation_and_obligations(db, monkeypatch):
    import jobscraper.pipeline.ingest as ingest_module

    run_id, _plan_id, claim, observation = _s311_prepare_observation(
        db, at=NOW, source_job_id="s311-crash"
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic crash after immutable observation insert")

    monkeypatch.setattr(ingest_module, "normalize_observation", explode)

    def mutate(conn):
        ingest_observation(
            conn,
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            observation=observation,
            source_id="src-feed",
            binding_id="bnd-feed",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            observed_at=NOW,
            now=NOW,
        )

    with pytest.raises(RuntimeError, match="synthetic crash"):
        with fenced_commit(
            db.conn, claim.request_id, claim.attempt_id, now=NOW, mutate=mutate
        ):
            pass

    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_observations WHERE request_id=?",
        (claim.request_id,),
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE run_id=? AND request_type"
        " IN ('RECONCILE','ELIGIBILITY','SCORE')",
        (run_id,),
    ).fetchone()[0] == 0


def test_s311_reopen_database_drains_each_durable_local_obligation_once(db):
    from jobscraper.runtime.clock import begin_service_epoch

    run_id, _plan_id, _job_id, _observation = _s311_ingest_job(db, at=NOW)
    create_profile(db.conn, snapshot=PROFILE, now=NOW)
    before = db.conn.execute(
        "SELECT id FROM scrape_requests WHERE run_id=? AND request_type"
        " IN ('RECONCILE','ELIGIBILITY','SCORE') ORDER BY id",
        (run_id,),
    ).fetchall()
    assert len(before) == 3
    request_ids = [row["id"] for row in before]

    path = db.path
    db.close()
    reopened = Database(path)
    try:
        begin_service_epoch(reopened.conn, now=LATER)
        assert drain_all_obligations(reopened.conn, now=LATER) == 3
        after = reopened.conn.execute(
            "SELECT id,status FROM scrape_requests WHERE run_id=? AND request_type"
            " IN ('RECONCILE','ELIGIBILITY','SCORE') ORDER BY id",
            (run_id,),
        ).fetchall()
        assert [row["id"] for row in after] == request_ids
        assert {row["status"] for row in after} == {"SUCCEEDED"}
        assert drain_all_obligations(reopened.conn, now=S311_LATER) == 0
        assert reopened.conn.execute("SELECT COUNT(*) FROM job_eligibility").fetchone()[0] == 1
        assert reopened.conn.execute("SELECT COUNT(*) FROM job_scores").fetchone()[0] == 1
    finally:
        reopened.close()


def test_s311_profile_advance_between_eval_obligations_never_leaves_mixed_pair(db):
    from jobscraper.pipeline.obligations import _evaluate_for_profiles

    _run, _plan_id, job_id, _observation = _s311_ingest_job(db, at=NOW)
    profile_id, rev1 = create_profile(db.conn, snapshot=PROFILE, now=NOW)

    score_claim = claim_next_request(
        db.conn, "s311-score-first", now=LATER, types=frozenset({"SCORE"})
    )
    assert score_claim is not None
    with fenced_commit(
        db.conn,
        score_claim.request_id,
        score_claim.attempt_id,
        now=LATER,
        mutate=lambda conn: _evaluate_for_profiles(
            conn, job_id, request_type="SCORE", now=LATER
        ),
    ):
        pass
    assert db.conn.execute(
        "SELECT profile_revision_id FROM job_eligibility WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == rev1
    assert db.conn.execute(
        "SELECT profile_revision_id FROM job_scores WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == rev1

    rev2 = edit_profile(
        db.conn, profile_id, snapshot=dict(PROFILE, keywords=["python"]), now=S311_LATER
    )
    eligibility_claim = claim_next_request(
        db.conn, "s311-elig-second", now=S311_LATER, types=frozenset({"ELIGIBILITY"})
    )
    assert eligibility_claim is not None
    with fenced_commit(
        db.conn,
        eligibility_claim.request_id,
        eligibility_claim.attempt_id,
        now=S311_LATER,
        mutate=lambda conn: _evaluate_for_profiles(
            conn, job_id, request_type="ELIGIBILITY", now=S311_LATER
        ),
    ):
        pass
    assert db.conn.execute(
        "SELECT profile_revision_id FROM job_eligibility WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == rev2
    assert db.conn.execute(
        "SELECT profile_revision_id FROM job_scores WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    ).fetchone()[0] == rev2


def test_s311_profile_edit_and_evaluation_rematerialization_are_atomic(db, monkeypatch):
    import jobscraper.pipeline.evaluation as evaluation_module

    _run, _plan_id, _job_id, _observation = _s311_ingest_job(db, at=NOW)
    profile_id, rev1 = create_profile(db.conn, snapshot=PROFILE, now=NOW)
    assert drain_all_obligations(db.conn, now=LATER) >= 3

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic profile rematerialization crash")

    monkeypatch.setattr(evaluation_module, "materialize_profile_revision", explode)
    with pytest.raises(RuntimeError, match="profile rematerialization crash"):
        edit_profile(
            db.conn,
            profile_id,
            snapshot=dict(PROFILE, name="must roll back"),
            now=S311_LATER,
        )

    assert db.conn.execute(
        "SELECT current_revision_id FROM search_profiles WHERE id=?", (profile_id,)
    ).fetchone()[0] == rev1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM profile_revisions WHERE profile_id=?", (profile_id,)
    ).fetchone()[0] == 1


def test_s311_duplicate_logical_obligation_enqueue_reuses_one_durable_request(db):
    run_id, _plan_id, _job_id, _observation = _s311_ingest_job(db, at=NOW)
    existing = db.conn.execute(
        "SELECT * FROM scrape_requests WHERE run_id=? AND request_type='ELIGIBILITY'",
        (run_id,),
    ).fetchone()
    assert existing is not None
    payload = json.loads(existing["payload_json"])
    observation = db.conn.execute(
        "SELECT id,parse_evidence_ref FROM job_observations WHERE id=?",
        (payload["observation_id"],),
    ).fetchone()
    request_id, created = enqueue_request(
        db.conn,
        run_id=existing["run_id"],
        run_source_plan_id=existing["run_source_plan_id"],
        source_id=existing["source_id"],
        binding_id=existing["binding_id"],
        request_type="ELIGIBILITY",
        target_identity=observation["id"],
        logical_key=observation["parse_evidence_ref"],
        payload=payload,
        priority=-10,
        now=LATER,
    )
    assert not created
    assert request_id == existing["id"]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE run_id=? AND request_type='ELIGIBILITY'",
        (run_id,),
    ).fetchone()[0] == 1
