"""S1.6 integration tests: observation → canonical pipeline.

Authority: 03 §29 (provenance-first observation model), §30 (evidence
chain), §40 + RUN-13 (absence authority), RUN-11 (canonical creation
ordering), RUN-12 (provenance fields), RUN-15 (source-native ID reuse),
RUN-21 (projection ordering / stale-update rejection); 01 §38/PROD-03
(reuse guard), §39 (canonical source selection); 02 §31 (URL fields).

Proves:

* collectors never write canonical jobs directly — every job enters via
  job_observations → normalization → entity resolution → job_sources;
* request-scoped observation idempotency (retry after PARTIAL/crash
  cannot duplicate);
* same native identity across runs resolves to ONE canonical job;
* PROD-03 reuse guard splits identity on incompatible evidence with a new
  source_identity_generation (RUN-15);
* RUN-21: an older observation processed late cannot regress newer
  canonical state;
* meaningful change classes recorded in job_history;
* canonical provenance selection prefers the higher-quality presence
  (§39) and preserves the best application URL;
* downstream obligations are created atomically with the observation;
* enumeration coverage: COMPLETE + absence-authoritative drives
  UNCERTAIN → EXPIRED; PARTIAL never generates absence evidence; the
  finalization barrier refuses COMPLETE while contributing requests run.
"""

from __future__ import annotations

import json

import pytest

from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    FieldEvidenceRecord,
    ObservationRecord,
)
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.canonical import select_canonical_provenance
from jobscraper.pipeline.coverage import (
    CoverageFinalizationError,
    degrade_coverage,
    finalize_coverage,
    is_coverage_degraded,
    open_coverage,
    record_seen_identity,
)
from jobscraper.pipeline.entity import find_current_presence
from jobscraper.pipeline.ingest import ingest_observation
from jobscraper.pipeline.normalize import normalize_observation
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T09:00:00.000000Z"
LATER = "2026-09-08T10:00:00.000000Z"
MUCH_LATER = "2026-09-08T12:00:00.000000Z"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "pipeline.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-feed','Fixture Feed','PUBLIC_FEED','https://jobs.example.test/api/jobs', '{NOW}', '{NOW}');
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
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-agg','Aggregator Board','PUBLIC_BOARD','https://agg.example.test', '{NOW}', '{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-agg','src-agg','html','{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, created_at)
        VALUES ('bndrev-agg','bnd-agg',1,'json_api_feed','1.0.0','HTTP_HTML','HTTP','perm-1',1,'{NOW}');
        """
    )
    # Model the service lifetime: the coordinator opens its service epoch
    # before any claiming (03 §50; claims fail closed without one).
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    yield database
    database.close()


def _run_and_request(db, source_id="src-feed", binding_id="bnd-feed"):
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
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=NOW)
    rid, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plans[0],
        source_id=source_id,
        binding_id=binding_id,
        request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim is not None and claim.request_id == rid
    return run_id, plans[0], rid, claim.attempt_id


def _obs(
    source_job_id="fx-100",
    title="Backend Engineer",
    company="Fixture Corp",
    description="<p>Build reliable things</p>",
    apply_url="https://jobs.example.test/jobs/fx-100/apply",
    job_url="https://jobs.example.test/jobs/fx-100",
    posted_at="2026-09-01T00:00:00.000000Z",
):
    return ObservationRecord(
        source_job_id=source_job_id,
        raw_url="https://jobs.example.test/api/jobs?page=1",
        canonical_url_candidate=job_url,
        application_url_candidate=apply_url,
        fields={
            "source_job_id": source_job_id,
            "title": title,
            "company": company,
            "description": description,
            "job_url": job_url,
            "apply_url": apply_url,
            "posted_at": posted_at,
        },
        field_evidence=(
            FieldEvidenceRecord("title", "json_path", "title", "h1", title[:50]),
            FieldEvidenceRecord("company", "json_path", "company.name", "h2", company),
        ),
    )


def _ingest(db, observation, request_id, attempt_id, now=NOW, run_id=None,
            outcome="SUCCEEDED", retry_delay_s=30.0, observed_at=None, source_id="src-feed",
            binding_id="bnd-feed", strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT"):
    """Ingest inside a fenced commit, exactly as the run driver does."""

    def mutate(conn):
        ingest_observation(
            conn,
            request_id=request_id,
            attempt_id=attempt_id,
            observation=observation,
            source_id=source_id,
            binding_id=binding_id,
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy=strategy,
            execution_class="HTTP",
            observed_at=observed_at or now,
            now=now,
        )

    with fenced_commit(db.conn, request_id, attempt_id, now=now, outcome=outcome,
                       retry_delay_s=retry_delay_s, mutate=mutate):
        pass


# ------------------------------------------------------------- provenance spine


def test_observation_creates_canonical_job_through_the_spine(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att, run_id=run_id)

    obs = db.conn.execute("SELECT * FROM job_observations").fetchall()
    assert len(obs) == 1
    assert obs[0]["observation_unique_key"]
    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    assert job is not None
    assert job["title"] == "Backend Engineer"
    assert job["normalized_title"] == "backend engineer"
    assert job["description_text"] and "Build reliable things" in job["description_text"]
    assert job["description_hash"]
    presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    assert presence["job_id"] == job["id"]
    assert presence["source_id"] == "src-feed"
    assert presence["source_job_id"] == "fx-100"
    assert presence["canonical_job_url"] == "https://jobs.example.test/jobs/fx-100"
    assert presence["application_url"] == "https://jobs.example.test/jobs/fx-100/apply"
    # field evidence persisted
    fe = db.conn.execute("SELECT * FROM field_evidence").fetchall()
    assert {r["field_name"] for r in fe} == {"title", "company"}
    # entity resolution event recorded
    ere = db.conn.execute("SELECT * FROM entity_resolution_events").fetchall()
    assert len(ere) == 1 and ere[0]["decision"] in ("CREATED", "MATCHED")


def test_downstream_obligations_created_atomically(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att, run_id=run_id)
    kinds = [
        r["request_type"]
        for r in db.conn.execute(
            "SELECT request_type FROM scrape_requests WHERE run_id = ? AND request_type"
            " IN ('RECONCILE','ELIGIBILITY','SCORE') ORDER BY request_type",
            (run_id,),
        )
    ]
    assert kinds == ["ELIGIBILITY", "RECONCILE", "SCORE"]
    # all still PENDING (host-native obligations drain later)
    statuses = {
        r["status"]
        for r in db.conn.execute(
            "SELECT status FROM scrape_requests WHERE request_type IN ('RECONCILE','ELIGIBILITY','SCORE')"
        )
    }
    assert statuses == {"PENDING"}


def test_request_scoped_idempotency_same_request_no_duplicate(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    # a PARTIAL parse commits its valid observations and stays retryable
    _ingest(db, _obs(), rid, att, run_id=run_id, outcome="RETRY_WAIT")
    row = db.conn.execute("SELECT status FROM scrape_requests WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "RETRY_WAIT"
    # the retry is a NEW attempt of the SAME request re-ingesting the same
    # observation: request-scoped idempotency must hold
    claim2 = claim_next_request(db.conn, "worker-1", now="2026-09-08T09:10:00.000000Z")
    assert claim2.request_id == rid and claim2.attempt_id != att
    _ingest(db, _obs(), claim2.request_id, claim2.attempt_id, run_id=run_id)
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_reobservation_same_native_identity_one_canonical_job(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att, run_id=run_id)
    # a later run observes the same job again
    rid2_added = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-feed",
        binding_id="bnd-feed",
        request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=2",
        logical_key="page=2",
    )
    claim2 = claim_next_request(db.conn, "worker-1", now=LATER)
    _ingest(db, _obs(), claim2.request_id, claim2.attempt_id, now=LATER, run_id=run_id)
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 2
    presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    assert presence["last_seen_at"] == LATER
    # unchanged content does not bump content_revision or invent changes
    presence = db.conn.execute("SELECT content_revision FROM job_sources").fetchone()
    assert presence["content_revision"] == 1
    changes = db.conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
    assert changes == 0


def test_an_unknown_posted_at_is_filled_and_a_known_one_never_rewritten(db):
    """``jobs.posted_at`` is a stable fact: filled once, never flapped.

    The provider-native shape (S2.5) makes this reachable in production: a
    board listing states no publication time, and the detail payload that
    follows does.  The canonical row must take the winner's value while it has
    none, and must never rewrite an established posted time with a later
    disagreement — no change class exists for that, so it must not happen.
    """
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(posted_at=None), rid, att, run_id=run_id)
    job = db.conn.execute("SELECT id, posted_at FROM jobs").fetchone()
    assert job["posted_at"] is None

    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-feed",
        binding_id="bnd-feed",
        request_type="DETAIL_FETCH",
        target_identity="https://jobs.example.test/jobs/fx-100",
        logical_key="detail=fx-100",
    )
    claim2 = claim_next_request(db.conn, "worker-1", now=LATER)
    _ingest(
        db,
        _obs(posted_at="2026-08-18T06:00:00.000000Z"),
        claim2.request_id,
        claim2.attempt_id,
        now=LATER,
        run_id=run_id,
    )
    filled = db.conn.execute(
        "SELECT posted_at FROM jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    assert filled["posted_at"] == "2026-08-18T06:00:00.000000Z"

    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-feed",
        binding_id="bnd-feed",
        request_type="DETAIL_FETCH",
        target_identity="https://jobs.example.test/jobs/fx-100",
        logical_key="detail=fx-100-again",
    )
    claim3 = claim_next_request(db.conn, "worker-1", now=MUCH_LATER)
    _ingest(
        db,
        _obs(posted_at="2020-01-01T00:00:00.000000Z"),
        claim3.request_id,
        claim3.attempt_id,
        now=MUCH_LATER,
        run_id=run_id,
    )
    kept = db.conn.execute(
        "SELECT posted_at FROM jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    assert kept["posted_at"] == "2026-08-18T06:00:00.000000Z"
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_change_classes_recorded(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att, run_id=run_id)
    claim2 = claim_next_request(db.conn, "w", now=LATER) if False else None
    rid2, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plan_id, source_id="src-feed",
        binding_id="bnd-feed", request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=2", logical_key="page=2",
    )
    claim2 = claim_next_request(db.conn, "worker-1", now=LATER)
    assert claim2.request_id == rid2
    _ingest(
        db,
        _obs(title="Senior Backend Engineer", apply_url="https://jobs.example.test/jobs/fx-100/apply-v2"),
        claim2.request_id,
        claim2.attempt_id,
        now=LATER,
        run_id=run_id,
    )
    changes = [
        r["change_class"]
        for r in db.conn.execute("SELECT change_class FROM job_history ORDER BY at")
    ]
    assert "TITLE_CHANGED" in changes and "APPLY_URL_CHANGED" in changes
    job = db.conn.execute("SELECT title FROM jobs").fetchone()
    assert job["title"] == "Senior Backend Engineer"
    presence = db.conn.execute("SELECT content_revision FROM job_sources").fetchone()
    assert presence["content_revision"] == 2


def test_reuse_guard_splits_identity_on_incompatible_evidence(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att, now="2026-01-01T00:00:00.000000Z", run_id=run_id)
    # close the old presence explicitly (direct provider evidence)
    db.conn.execute(
        "UPDATE job_sources SET presence_state = 'CLOSED', last_seen_at = ?",
        ("2026-03-01T00:00:00.000000Z",),
    )
    db.conn.commit()
    # much later, the SAME native id carries a different company entirely
    rid3, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plan_id, source_id="src-feed",
        binding_id="bnd-feed", request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=9", logical_key="page=9",
    )
    claim3 = claim_next_request(db.conn, "worker-1", now=MUCH_LATER)
    _ingest(
        db,
        _obs(title="Marketing Lead", company="Completely Different GmbH"),
        claim3.request_id,
        claim3.attempt_id,
        now=MUCH_LATER,
        run_id=run_id,
    )
    # two canonical jobs; the second presence uses generation 2 (RUN-15)
    jobs = db.conn.execute("SELECT id, title FROM jobs ORDER BY created_at").fetchall()
    assert len(jobs) == 2
    presences = db.conn.execute(
        "SELECT job_id, source_identity_generation, presence_state FROM job_sources"
        " WHERE source_job_id = 'fx-100' ORDER BY source_identity_generation"
    ).fetchall()
    assert [p["source_identity_generation"] for p in presences] == [1, 2]
    assert presences[0]["presence_state"] == "CLOSED"
    assert presences[1]["presence_state"] == "ACTIVE"
    # a reuse-detection event was recorded for provenance
    events = db.conn.execute(
        "SELECT decision FROM entity_resolution_events WHERE stage = 'native_identity_reuse_guard'"
    ).fetchall()
    assert len(events) == 1


def test_run21_older_late_observation_cannot_regress_newer_state(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(title="Newest Title"), rid, att, now=NOW, observed_at=LATER,
            run_id=run_id)
    rid2, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plan_id, source_id="src-feed",
        binding_id="bnd-feed", request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=0", logical_key="page=0",
    )
    claim2 = claim_next_request(db.conn, "worker-1", now=MUCH_LATER)
    # an OLDER observation (observed_at earlier) is processed later
    _ingest(db, _obs(title="Stale Old Title"), claim2.request_id, claim2.attempt_id,
            now="2026-09-08T09:30:00.000000Z", run_id=run_id)
    job = db.conn.execute("SELECT title, updated_at FROM jobs").fetchone()
    assert job["title"] == "Newest Title", "older evidence must not overwrite newer"
    presence = db.conn.execute("SELECT last_seen_at FROM job_sources").fetchone()
    assert presence["last_seen_at"] == LATER, "presence must not move backwards"


def test_canonical_provenance_selection_prefers_higher_quality(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att, run_id=run_id)
    # a second, lower-quality (aggregator HTTP_HTML) source observes the same job
    plan2 = dict(
        source_id="src-agg",
        source_plan_group_id="grp-src-agg",
        fallback_rank=0,
        binding_id="bnd-agg",
        binding_revision_id="bndrev-agg",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="HTTP_HTML",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
    )
    run2, plans2 = create_run(db.conn, profile_id=None, plans=[plan2], now=LATER)
    rid2, _ = enqueue_request(
        db.conn, run_id=run2, run_source_plan_id=plans2[0], source_id="src-agg",
        binding_id="bnd-agg", request_type="LIST_FETCH",
        target_identity="https://agg.example.test/jobs?utm_source=x",
    )
    claim2 = claim_next_request(db.conn, "worker-1", now=LATER)
    _ingest(
        db,
        _obs(
            title="Backend Engineer (via aggregator)",
            apply_url="https://agg.example.test/apply/fx-100",
            # the aggregator links the employer-canonical job URL (§38 stage 3)
            job_url="https://jobs.example.test/jobs/fx-100",
        ),
        claim2.request_id,
        claim2.attempt_id,
        now=LATER,
        run_id=run2,
        source_id="src-agg",
        binding_id="bnd-agg",
        strategy="HTTP_HTML",
    )
    # exactly one canonical job: the URL stage attached the aggregator's
    # presence to the employer-side job (no merge of two jobs occurred)
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    # canonical fields prefer the structured employer-side provenance (§39)
    assert job["title"] == "Backend Engineer"
    winner = db.conn.execute(
        "SELECT s.source_id FROM job_sources s WHERE s.id = ?", (job["canonical_provenance_id"],)
    ).fetchone()
    assert winner["source_id"] == "src-feed"
    # both presences remain fully inspectable
    assert db.conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 2


# ------------------------------------------------------ enumeration coverage


def _coverage_setup(db, *, authority="AUTHORITATIVE_FULL_SOURCE", request_running=False):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(source_job_id="fx-1"), rid, att, run_id=run_id)
    rid_b, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plan_id, source_id="src-feed",
        binding_id="bnd-feed", request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=2", logical_key="page=2",
    )
    claim_b = claim_next_request(db.conn, "worker-1", now=LATER)
    _ingest(db, _obs(source_job_id="fx-2"), claim_b.request_id, claim_b.attempt_id,
            now=LATER, run_id=run_id)
    coverage_id = open_coverage(
        db.conn,
        run_source_plan_id=plan_id,
        source_id="src-feed",
        binding_id="bnd-feed",
        scope_key="full-source",
        generation_key="gen-1",
        coverage_authority=authority,
        now=LATER,
    )
    record_seen_identity(db.conn, coverage_id, "fx-1")
    record_seen_identity(db.conn, coverage_id, "fx-2")
    db.conn.execute(
        "INSERT INTO coverage_contributing_request (coverage_id, request_id) VALUES (?, ?)",
        (coverage_id, rid),
    )
    db.conn.execute(
        "INSERT INTO coverage_contributing_request (coverage_id, request_id) VALUES (?, ?)",
        (coverage_id, claim_b.request_id),
    )
    db.conn.commit()
    if request_running:
        # simulate a third contributing request still running
        rid_c, _ = enqueue_request(
            db.conn, run_id=run_id, run_source_plan_id=plan_id, source_id="src-feed",
            binding_id="bnd-feed", request_type="LIST_FETCH",
            target_identity="https://jobs.example.test/api/jobs?page=3", logical_key="page=3",
        )
        claim_c = claim_next_request(db.conn, "worker-1", now=LATER)
        db.conn.execute(
            "INSERT INTO coverage_contributing_request (coverage_id, request_id) VALUES (?, ?)",
            (coverage_id, claim_c.request_id),
        )
        db.conn.commit()
    return run_id, plan_id, coverage_id, claim_b.request_id


def test_coverage_complete_drives_absence_evidence(db):
    run_id, plan_id, cov, rid_b = _coverage_setup(db)
    finalize_coverage(
        db.conn, cov, completion_state="COMPLETE", stop_reason="terminal cursor",
        terminal_enumeration_proven=True, now=MUCH_LATER,
    )
    row = db.conn.execute("SELECT * FROM enumeration_coverage WHERE id = ?", (cov,)).fetchone()
    assert row["completion_state"] == "COMPLETE"
    assert row["finalized_at"] == MUCH_LATER and row["applied_at"] == MUCH_LATER
    # fx-1/fx-2 were seen; nothing absent in this generation


def test_absence_uncertain_then_expired_under_repeated_complete_generations(db):
    run_id, plan_id, cov, rid_b = _coverage_setup(db)
    # generation 1 sees only fx-1 (fx-2 absent)
    db.conn.execute("DELETE FROM coverage_seen_identity WHERE stable_source_identity = 'fx-2'")
    db.conn.commit()
    finalize_coverage(
        db.conn, cov, completion_state="COMPLETE", stop_reason="terminal cursor",
        terminal_enumeration_proven=True, now=MUCH_LATER,
    )
    fx2 = db.conn.execute(
        "SELECT presence_state, last_absence_coverage_id FROM job_sources WHERE source_job_id = 'fx-2'"
    ).fetchone()
    assert fx2["presence_state"] == "UNCERTAIN"
    assert fx2["last_absence_coverage_id"] == cov
    # generation 2 (a later complete authoritative generation) still misses fx-2
    cov2 = open_coverage(
        db.conn, run_source_plan_id=plan_id, source_id="src-feed", binding_id="bnd-feed",
        scope_key="full-source", generation_key="gen-2",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE", now=MUCH_LATER,
    )
    record_seen_identity(db.conn, cov2, "fx-1")
    db.conn.execute(
        "INSERT INTO coverage_contributing_request (coverage_id, request_id) VALUES (?, ?)",
        (cov2, rid_b),
    )
    db.conn.commit()
    finalize_coverage(
        db.conn, cov2, completion_state="COMPLETE", stop_reason="terminal cursor",
        terminal_enumeration_proven=True, now="2026-09-09T00:00:00.000000Z",
    )
    fx2 = db.conn.execute(
        "SELECT presence_state FROM job_sources WHERE source_job_id = 'fx-2'"
    ).fetchone()
    assert fx2["presence_state"] == "EXPIRED"
    # seen job stays ACTIVE
    fx1 = db.conn.execute(
        "SELECT presence_state FROM job_sources WHERE source_job_id = 'fx-1'"
    ).fetchone()
    assert fx1["presence_state"] == "ACTIVE"


def test_partial_coverage_never_generates_absence_evidence(db):
    run_id, plan_id, cov, rid_b = _coverage_setup(db)
    db.conn.execute("DELETE FROM coverage_seen_identity WHERE stable_source_identity = 'fx-2'")
    db.conn.commit()
    finalize_coverage(
        db.conn, cov, completion_state="PARTIAL", stop_reason="budget exhausted",
        terminal_enumeration_proven=False, now=MUCH_LATER,
    )
    fx2 = db.conn.execute(
        "SELECT presence_state, last_absence_coverage_id FROM job_sources WHERE source_job_id = 'fx-2'"
    ).fetchone()
    assert fx2["presence_state"] == "ACTIVE"
    assert fx2["last_absence_coverage_id"] is None


def test_non_authoritative_coverage_never_generates_absence(db):
    run_id, plan_id, cov, rid_b = _coverage_setup(db, authority="NON_AUTHORITATIVE_QUERY")
    db.conn.execute("DELETE FROM coverage_seen_identity WHERE stable_source_identity = 'fx-2'")
    db.conn.commit()
    finalize_coverage(
        db.conn, cov, completion_state="COMPLETE", stop_reason="terminal cursor",
        terminal_enumeration_proven=True, now=MUCH_LATER,
    )
    fx2 = db.conn.execute(
        "SELECT presence_state FROM job_sources WHERE source_job_id = 'fx-2'"
    ).fetchone()
    assert fx2["presence_state"] == "ACTIVE"


def test_a_resumed_pass_continues_an_unfinished_generation(db):
    """Restart recovery must not crash on the durable generation key.

    ``enumeration_coverage`` is unique per (plan, scope, generation), so a
    second pass over the same run either continues the unfinished generation
    or opens a distinctly named one — never a UNIQUE violation, and never a
    rewrite of a finalized generation (S2.5, RUN-07/§18).
    """
    from jobscraper.pipeline.coverage import open_or_resume_coverage

    run_id, plan_id, _rid, _att = _run_and_request(db)
    kwargs = dict(
        run_source_plan_id=plan_id,
        source_id="src-feed",
        binding_id="bnd-feed",
        scope_key="full-source",
        generation_key=f"run-{run_id}",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
    )
    first, resumed = open_or_resume_coverage(db.conn, now=NOW, **kwargs)
    assert resumed is False

    # an unfinished generation is continued, not duplicated
    again, resumed = open_or_resume_coverage(db.conn, now=LATER, **kwargs)
    assert again == first
    assert resumed is True
    assert db.conn.execute("SELECT COUNT(*) FROM enumeration_coverage").fetchone()[0] == 1

    finalize_coverage(
        db.conn, first, completion_state="PARTIAL", stop_reason="driver stop",
        terminal_enumeration_proven=False, now=LATER,
    )

    # a finalized generation is immutable: the next pass opens its own,
    # deterministically named
    third, resumed = open_or_resume_coverage(db.conn, now=MUCH_LATER, **kwargs)
    assert resumed is False
    assert third != first
    rows = db.conn.execute(
        "SELECT generation_key, finalized_at FROM enumeration_coverage"
        " ORDER BY created_at, id"
    ).fetchall()
    assert [row["generation_key"] for row in rows] == [
        f"run-{run_id}", f"run-{run_id}#pass-2",
    ]
    assert rows[0]["finalized_at"] is not None
    assert rows[1]["finalized_at"] is None

    # and resuming that one continues it rather than naming a third
    fourth, resumed = open_or_resume_coverage(db.conn, now=MUCH_LATER, **kwargs)
    assert (fourth, resumed) == (third, True)
    assert db.conn.execute("SELECT COUNT(*) FROM enumeration_coverage").fetchone()[0] == 2


def test_finalization_barrier_refuses_complete_for_a_degraded_generation(db):
    """Pre-S2.7 corrective A: ``degrade_coverage`` is the durable
    ``PARTIAL ⇒ degraded`` primitive (ACQ-03, RUN-13).  Once applied to an
    open generation, ``finalize_coverage(COMPLETE)`` is refused even with
    terminal enumeration proven and every contributing request closed."""
    run_id, plan_id, cov, rid_b = _coverage_setup(db)
    assert is_coverage_degraded(db.conn, cov) is False

    degrade_coverage(db.conn, cov, reason="degraded by PARTIAL acquisition unit")
    assert is_coverage_degraded(db.conn, cov) is True
    row = db.conn.execute("SELECT * FROM enumeration_coverage WHERE id = ?", (cov,)).fetchone()
    assert row["absence_inference_allowed"] == 0
    assert row["finalized_at"] is None  # degrading is not finalizing

    # idempotent and irreversible: a second call changes nothing, and there
    # is no API that sets the flag back
    degrade_coverage(db.conn, cov, reason="again")
    assert db.conn.execute(
        "SELECT absence_inference_allowed, stop_reason FROM enumeration_coverage WHERE id = ?", (cov,)
    ).fetchone()[0] == 0

    with pytest.raises(CoverageFinalizationError):
        finalize_coverage(
            db.conn, cov, completion_state="COMPLETE", stop_reason="terminal cursor",
            terminal_enumeration_proven=True, now=MUCH_LATER,
        )
    assert db.conn.execute(
        "SELECT finalized_at FROM enumeration_coverage WHERE id = ?", (cov,)
    ).fetchone()["finalized_at"] is None

    # PARTIAL finalization is what remains possible; it applies no absence
    finalize_coverage(
        db.conn, cov, completion_state="PARTIAL", stop_reason="degraded by PARTIAL acquisition unit",
        terminal_enumeration_proven=False, now=MUCH_LATER,
    )
    row = db.conn.execute("SELECT * FROM enumeration_coverage WHERE id = ?", (cov,)).fetchone()
    assert row["completion_state"] == "PARTIAL"
    assert row["applied_at"] is not None
    for presence in db.conn.execute("SELECT presence_state, last_absence_coverage_id FROM job_sources"):
        assert presence["presence_state"] == "ACTIVE"
        assert presence["last_absence_coverage_id"] != cov


def test_degrade_coverage_leaves_a_finalized_generation_untouched(db):
    """Generations are immutable once finalized (03 §40); degradation of a
    later PARTIAL never rewrites an earlier COMPLETE proof."""
    run_id, plan_id, cov, rid_b = _coverage_setup(db)
    db.conn.execute("UPDATE scrape_requests SET status = 'SUCCEEDED' WHERE id = ?", (rid_b,))
    db.conn.commit()
    finalize_coverage(
        db.conn, cov, completion_state="COMPLETE", stop_reason="terminal cursor",
        terminal_enumeration_proven=True, now=MUCH_LATER,
    )
    degrade_coverage(db.conn, cov, reason="too late")
    row = db.conn.execute("SELECT * FROM enumeration_coverage WHERE id = ?", (cov,)).fetchone()
    assert row["completion_state"] == "COMPLETE"
    assert row["absence_inference_allowed"] == 1
    assert is_coverage_degraded(db.conn, cov) is False


def test_degrade_coverage_is_a_no_op_for_non_absence_authorities(db):
    """A NON_AUTHORITATIVE_QUERY generation never had absence authority to
    lose; degrading it neither flips anything nor reports degraded."""
    run_id, plan_id, cov, rid_b = _coverage_setup(db, authority="NON_AUTHORITATIVE_QUERY")
    before = db.conn.execute("SELECT * FROM enumeration_coverage WHERE id = ?", (cov,)).fetchone()
    assert before["absence_inference_allowed"] == 0
    degrade_coverage(db.conn, cov, reason="x")
    assert is_coverage_degraded(db.conn, cov) is False


def test_finalization_barrier_refuses_complete_while_requests_running(db):
    run_id, plan_id, cov, rid_b = _coverage_setup(db, request_running=True)
    with pytest.raises(CoverageFinalizationError):
        finalize_coverage(
            db.conn, cov, completion_state="COMPLETE", stop_reason="terminal cursor",
            terminal_enumeration_proven=True, now=MUCH_LATER,
        )
    row = db.conn.execute("SELECT finalized_at FROM enumeration_coverage WHERE id = ?", (cov,)).fetchone()
    assert row["finalized_at"] is None


def test_select_canonical_provenance_requires_a_winner(db):
    with pytest.raises(ValueError):
        select_canonical_provenance([])
