"""Post-S3.13 Corrective A5: durable semantic run effects and honest counters.

Authority: docs/reviews/post-s3.13-corrective-a-enumeration-lifecycle-authority-2026-09-13.md,
docs/reviews/post-s3.13-corrective-a-design-corrective-review-2026-09-13.md,
tests/contract/fixtures/post_s313_corrective_a_contract_v1.json (A5 locked names).

Proves every new observation receives one immutable semantic run effect inside
the ingestion fence (NEW_JOB / UPDATED_JOB / UNCHANGED_JOB / STALE_IGNORED)
and that run counters derive only from those durable effects.
"""

from __future__ import annotations

import pytest

from jobscraper.adapters.contract import FieldEvidenceRecord, ObservationRecord
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.canonical import refresh_canonical_presentation
from jobscraper.pipeline.driver import _update_run_counters
from jobscraper.pipeline.ingest import (
    ObservationEffectIntegrityError,
    _bind_observation_effect,
    ingest_observation,
)
from jobscraper.pipeline.normalize import normalize_observation
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T09:00:00.000000Z"
LATER = "2026-09-08T10:00:00.000000Z"
EARLIER = "2026-09-08T08:00:00.000000Z"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "a5.db")
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
        """
    )
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    yield database
    database.close()


def _plan(source_id="src-feed", binding_id="bnd-feed"):
    return dict(
        source_id=source_id,
        source_plan_group_id=f"grp-{source_id}",
        fallback_rank=0,
        binding_id=binding_id,
        binding_revision_id="bndrev-feed",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
    )


def _new_run(db, now=NOW):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=now)
    return run_id, plans[0]


def _new_request(db, run_id, plan_id, target="https://jobs.example.test/api/jobs?page=1",
                 now=NOW, request_type="LIST_FETCH"):
    rid, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-feed",
        binding_id="bnd-feed",
        request_type=request_type,
        target_identity=target,
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
    )
    claim = claim_next_request(db.conn, "worker-1", now=now)
    assert claim is not None and claim.request_id == rid, (
        f"expected claim on {rid}, got {claim.request_id if claim else None}"
    )
    return claim.request_id, claim.attempt_id


def _obs(
    source_job_id="fx-100",
    title="Backend Engineer",
    company="Fixture Corp",
    description="<p>Build reliable things</p>",
    apply_url="https://jobs.example.test/jobs/fx-100/apply",
    job_url="https://jobs.example.test/jobs/fx-100",
    posted_at="2026-09-01T00:00:00.000000Z",
    **field_overrides,
):
    fields = {
        "source_job_id": source_job_id,
        "title": title,
        "company": company,
        "description": description,
        "job_url": job_url,
        "apply_url": apply_url,
        "posted_at": posted_at,
    }
    fields.update(field_overrides)
    return ObservationRecord(
        source_job_id=source_job_id,
        raw_url="https://jobs.example.test/api/jobs?page=1",
        canonical_url_candidate=job_url,
        application_url_candidate=apply_url,
        fields=fields,
        field_evidence=(
            FieldEvidenceRecord("title", "json_path", "title", "h1", title[:50]),
        ),
    )


def _ingest(db, observation, request_id, attempt_id, now=NOW, observed_at=None,
            outcome="SUCCEEDED", retry_delay_s=30.0, **evidence):
    """Ingest inside a fenced commit and return the ingest_observation summary."""
    out = {}

    def mutate(conn):
        out["result"] = ingest_observation(
            conn,
            request_id=request_id,
            attempt_id=attempt_id,
            observation=observation,
            source_id="src-feed",
            binding_id="bnd-feed",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            observed_at=observed_at or now,
            now=now,
            **evidence,
        )

    with fenced_commit(
        db.conn, request_id, attempt_id, now=now, outcome=outcome,
        retry_delay_s=retry_delay_s, mutate=mutate
    ):
        pass
    return out["result"]


def _counters(db, run_id):
    _update_run_counters(db.conn, run_id)
    return db.conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (run_id,)).fetchone()


def _effects(db, run_id):
    return {
        r["run_effect"]: r["n"]
        for r in db.conn.execute(
            "SELECT run_effect, COUNT(*) AS n FROM job_observations"
            " WHERE run_id = ? GROUP BY run_effect",
            (run_id,),
        ).fetchall()
    }


# ---------------------------------------------------------------- locked A5


def test_unchanged_reobservation_reports_zero_jobs_updated(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW)
    assert first["run_effect"] == "NEW_JOB"
    c1 = _counters(db, run1)
    assert c1["jobs_saved"] == 1

    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(db, run2, plan2, now=LATER)
    second = _ingest(db, _obs(), rid2, att2, now=LATER, observed_at=LATER)
    assert second["run_effect"] == "UNCHANGED_JOB"
    assert second["resolved_job_id"] == first["resolved_job_id"]

    # immutable re-observation evidence grew, identity did not
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    row = db.conn.execute(
        "SELECT resolved_job_id, run_effect FROM job_observations WHERE id = ?",
        (second["observation_id"],),
    ).fetchone()
    assert row["run_effect"] == "UNCHANGED_JOB"

    c2 = _counters(db, run2)
    assert c2["jobs_saved"] == 0
    assert c2["jobs_updated"] == 0


def test_meaningful_existing_job_change_reports_one_job_updated(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW)
    _counters(db, run1)

    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(db, run2, plan2, now=LATER)
    changed = _obs(title="Senior Backend Engineer")
    second = _ingest(db, changed, rid2, att2, now=LATER, observed_at=LATER)
    assert second["resolved_job_id"] == first["resolved_job_id"]
    assert second["run_effect"] == "UPDATED_JOB"
    job = db.conn.execute("SELECT * FROM jobs WHERE id = ?", (first["job_id"],)).fetchone()
    assert job["title"] == "Senior Backend Engineer"

    c2 = _counters(db, run2)
    assert c2["jobs_saved"] == 0
    assert c2["jobs_updated"] == 1


def test_new_job_is_saved_not_double_counted_as_updated(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW)
    assert first["run_effect"] == "NEW_JOB"

    # later accepted evidence for the same job in the same run
    rid2, _ = enqueue_request(
        db.conn,
        run_id=run1,
        run_source_plan_id=plan1,
        source_id="src-feed",
        binding_id="bnd-feed",
        request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=2",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
    )
    claim2 = claim_next_request(db.conn, "worker-1", now=LATER)
    second = _ingest(
        db, _obs(title="Senior Backend Engineer"), claim2.request_id, claim2.attempt_id,
        now=LATER, observed_at=LATER,
    )
    assert second["resolved_job_id"] == first["resolved_job_id"]
    assert second["run_effect"] == "UPDATED_JOB"

    effects = _effects(db, run1)
    assert effects.get("NEW_JOB") == 1
    assert effects.get("UPDATED_JOB") == 1
    counters = _counters(db, run1)
    assert counters["jobs_saved"] == 1
    # the same job must not be double-counted as updated
    assert counters["jobs_updated"] == 0


def test_stale_observation_has_no_saved_or_updated_current_effect(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1, now=LATER)
    first = _ingest(db, _obs(), rid1, att1, now=LATER, observed_at=LATER)
    assert first["run_effect"] == "NEW_JOB"
    before_presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    before_job = db.conn.execute("SELECT * FROM jobs WHERE id = ?", (first["job_id"],)).fetchone()
    _counters(db, run1)

    # an older worker finishes late with older effective time
    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(
        db, run2, plan2, target="https://jobs.example.test/api/jobs?page=9",
        now=LATER,
    )
    stale = _ingest(db, _obs(), rid2, att2, now=LATER, observed_at=EARLIER)
    assert stale["run_effect"] == "STALE_IGNORED"

    # immutable history grew, current projection did not move
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 2
    after_presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    assert after_presence["last_seen_at"] == before_presence["last_seen_at"]
    after_job = db.conn.execute("SELECT * FROM jobs WHERE id = ?", (first["job_id"],)).fetchone()
    assert after_job["title"] == before_job["title"]
    assert after_job["description_hash"] == before_job["description_hash"]

    c2 = _counters(db, run2)
    assert c2["jobs_saved"] == 0
    assert c2["jobs_updated"] == 0


# ------------------------------------------------------- supporting A5


def test_observation_effect_binding_is_idempotent(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW)
    # rebinding the same effect is a no-op
    _bind_observation_effect(
        db.conn,
        observation_id=first["observation_id"],
        job_id=first["job_id"],
        run_effect=first["run_effect"],
    )
    db.conn.commit()
    row = db.conn.execute(
        "SELECT resolved_job_id, run_effect FROM job_observations WHERE id = ?",
        (first["observation_id"],),
    ).fetchone()
    assert row["run_effect"] == "NEW_JOB"


def test_conflicting_observation_effect_binding_fails_closed(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW)
    with pytest.raises(ObservationEffectIntegrityError):
        _bind_observation_effect(
            db.conn,
            observation_id=first["observation_id"],
            job_id=first["job_id"],
            run_effect="UPDATED_JOB",
        )
    db.conn.rollback()


def test_idempotent_observation_replay_returns_existing_effect(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    # PARTIAL/RETRY_WAIT commits the observation but leaves the request
    # retryable, exactly like a crash after a partial parse.
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW,
                    outcome="RETRY_WAIT")
    assert first["run_effect"] == "NEW_JOB"

    # the retry is a NEW attempt of the SAME request re-ingesting the same
    # observation: request-scoped idempotency must return the bound effect.
    claim2 = claim_next_request(db.conn, "worker-1", now=LATER)
    assert claim2 is not None and claim2.request_id == rid1
    replay = _ingest(db, _obs(), claim2.request_id, claim2.attempt_id,
                     now=LATER, observed_at=NOW)
    assert replay["idempotent"] is True
    assert replay["resolved_job_id"] == first["resolved_job_id"]
    assert replay["run_effect"] == "NEW_JOB"
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_timestamp_only_reverification_is_unchanged(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW)
    rev_before = db.conn.execute(
        "SELECT content_revision FROM job_sources"
    ).fetchone()[0]
    _counters(db, run1)

    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(db, run2, plan2, now=LATER)
    second = _ingest(db, _obs(), rid2, att2, now=LATER, observed_at=LATER)
    assert second["run_effect"] == "UNCHANGED_JOB"
    rev_after = db.conn.execute(
        "SELECT content_revision FROM job_sources"
    ).fetchone()[0]
    assert rev_after == rev_before
    assert db.conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0] == 0
    c2 = _counters(db, run2)
    assert c2["jobs_saved"] == 0
    assert c2["jobs_updated"] == 0


def test_missing_candidate_does_not_create_false_semantic_or_apply_url_change(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW)
    stored_apply = db.conn.execute(
        "SELECT application_url FROM job_sources"
    ).fetchone()[0]
    assert stored_apply == "https://jobs.example.test/jobs/fx-100/apply"
    _counters(db, run1)

    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(db, run2, plan2, now=LATER)
    import dataclasses as _dc

    missing = _dc.replace(_obs(), application_url_candidate=None)
    second = _ingest(db, missing, rid2, att2, now=LATER, observed_at=LATER)
    assert second["run_effect"] == "UNCHANGED_JOB"
    assert db.conn.execute(
        "SELECT application_url FROM job_sources"
    ).fetchone()[0] == stored_apply
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM job_history WHERE change_class = 'APPLY_URL_CHANGED'"
        ).fetchone()[0]
        == 0
    )
    c2 = _counters(db, run2)
    assert c2["jobs_saved"] == 0
    assert c2["jobs_updated"] == 0


# ------------------------------------------------------- A5.1 canonical


def _first_job(db):
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, observed_at=NOW)
    presence = db.conn.execute("SELECT * FROM job_sources").fetchone()
    return first, presence


def test_canonical_refresh_identical_observation_reports_no_change(db):
    first, presence = _first_job(db)
    row = db.conn.execute(
        "SELECT raw_payload_ref FROM job_observations WHERE id = ?",
        (first["observation_id"],),
    ).fetchone()
    import json as _json

    class _Shim:
        fields = _json.loads(row["raw_payload_ref"])

    normalized = normalize_observation(_Shim(), observed_at=NOW)
    result = refresh_canonical_presentation(
        db.conn, first["job_id"], now=LATER, normalized=normalized,
        fresh_presence_id=presence["id"],
    )
    assert result.projection_changed is False


def test_canonical_refresh_title_change_reports_change(db):
    first, presence = _first_job(db)
    changed = _obs(title="Senior Backend Engineer")
    normalized = normalize_observation(changed, observed_at=LATER)
    result = refresh_canonical_presentation(
        db.conn, first["job_id"], now=LATER, normalized=normalized,
        fresh_presence_id=presence["id"],
    )
    assert result.projection_changed is True
    assert "TITLE_CHANGED" in result.change_classes
    db.conn.rollback()


def test_canonical_refresh_content_change_reports_change(db):
    first, presence = _first_job(db)
    changed = _obs(description="<p>Something completely different</p>")
    normalized = normalize_observation(changed, observed_at=LATER)
    result = refresh_canonical_presentation(
        db.conn, first["job_id"], now=LATER, normalized=normalized,
        fresh_presence_id=presence["id"],
    )
    assert result.projection_changed is True
    db.conn.rollback()


def test_canonical_refresh_location_change_reports_change(db):
    first, presence = _first_job(db)
    changed = _obs(location=["Paris, France"])
    normalized = normalize_observation(changed, observed_at=LATER)
    result = refresh_canonical_presentation(
        db.conn, first["job_id"], now=LATER, normalized=normalized,
        fresh_presence_id=presence["id"],
    )
    assert result.projection_changed is True
    db.conn.rollback()


def test_canonical_refresh_semantic_field_change_reports_change(db):
    first, presence = _first_job(db)
    changed = _obs(employment_type="full time")
    normalized = normalize_observation(changed, observed_at=LATER)
    assert normalized.employment_type == "FULL_TIME"
    result = refresh_canonical_presentation(
        db.conn, first["job_id"], now=LATER, normalized=normalized,
        fresh_presence_id=presence["id"],
    )
    assert result.projection_changed is True
    db.conn.rollback()


def test_canonical_refresh_timestamp_only_verification_reports_no_change(db):
    first, presence = _first_job(db)
    row = db.conn.execute(
        "SELECT raw_payload_ref FROM job_observations WHERE id = ?",
        (first["observation_id"],),
    ).fetchone()
    import json as _json

    class _Shim:
        fields = _json.loads(row["raw_payload_ref"])

    normalized = normalize_observation(_Shim(), observed_at=LATER)
    result = refresh_canonical_presentation(
        db.conn, first["job_id"], now=LATER, normalized=normalized,
        fresh_presence_id=presence["id"],
    )
    assert result.projection_changed is False
    assert result.evaluation_changed is False


def _view(db, run_id, plan_id, observation, kind, at):
    rid, att = _new_request(
        db, run_id, plan_id, target=f"https://jobs.example.test/{kind}/{at}",
        now=at, request_type=kind,
    )
    return _ingest(db, observation, rid, att, now=at)


def _presentation(db):
    job = db.conn.execute(
        "SELECT id, title, description_text, remote_mode, employment_type,"
        " salary_min, canonical_provenance_id, evaluation_revision FROM jobs"
    ).fetchone()
    locations = db.conn.execute(
        "SELECT raw_text, country, region, city, remote FROM job_locations"
        " ORDER BY raw_text"
    ).fetchall()
    return tuple(job), [tuple(row) for row in locations]


def _seed_views(db):
    listing = _obs(location=["Berlin, Germany"])
    detail = _obs(location=[{"city": "Berlin", "region": "Berlin", "country": "DE", "type": "onsite"}])
    run_id, plan_id = _new_run(db)
    first = _view(db, run_id, plan_id, listing, "LIST_FETCH", NOW)
    _view(db, run_id, plan_id, detail, "DETAIL_FETCH", "2026-09-08T09:01:00.000000Z")
    return listing, detail, first


def test_unchanged_listing_retains_detail_projection_through_repeated_runs(db):
    listing, detail, first = _seed_views(db)
    before = _presentation(db)
    assert before[0][3] == "ONSITE"
    revision = db.conn.execute("SELECT content_revision FROM job_sources").fetchone()[0]
    history = db.conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
    for hour in (10, 11):
        at = f"2026-09-08T{hour}:00:00.000000Z"
        run_id, plan_id = _new_run(db, now=at)
        for minute, (kind, observation) in enumerate((("LIST_FETCH", listing), ("DETAIL_FETCH", detail))):
            result = _view(db, run_id, plan_id, observation, kind,
                           f"2026-09-08T{hour}:0{minute}:00.000000Z")
            assert _presentation(db) == before
            assert result["run_effect"] == "UNCHANGED_JOB"
            assert result["job_id"] == first["job_id"]
            # A non-fresh winning presence must also re-project its retained
            # effective detail evidence, even while its latest sighting is LIST.
            db.conn.execute("BEGIN IMMEDIATE")
            db.conn.execute("UPDATE jobs SET title = 'DRIFTED'")
            refresh_canonical_presentation(
                db.conn, first["job_id"], now=at, normalized=None,
                fresh_presence_id="another-presence",
            )
            assert db.conn.execute("SELECT remote_mode FROM jobs").fetchone()[0] == "ONSITE"
            # Reset only diagnostic drift effects before checking acquisition history.
            db.conn.rollback()
        counters = _counters(db, run_id)
        assert (counters["jobs_saved"], counters["jobs_updated"]) == (0, 0)
    assert db.conn.execute("SELECT content_revision FROM job_sources").fetchone()[0] == revision
    assert db.conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0] == history
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 6


def test_changed_listing_and_detail_projection_evidence_still_updates(db):
    listing, detail, first = _seed_views(db)
    run_id, plan_id = _new_run(db, now=LATER)
    changed_listing = _obs(location=["Paris, France"])
    changed = _view(db, run_id, plan_id, changed_listing, "LIST_FETCH", LATER)
    assert changed["job_id"] == first["job_id"]
    assert changed["run_effect"] == "UPDATED_JOB"
    assert db.conn.execute("SELECT country FROM job_locations").fetchone()[0] == "FR"
    # An unchanged detail must not undo a real, newer listing change.
    before = _presentation(db)
    unchanged = _view(db, run_id, plan_id, detail, "DETAIL_FETCH", "2026-09-08T10:01:00.000000Z")
    assert _presentation(db) == before
    assert unchanged["run_effect"] == "UNCHANGED_JOB"
    changed_detail = _obs(location=[{"city": "Paris", "country": "FR", "type": "onsite"}], employment_type="full time")
    changed = _view(db, run_id, plan_id, changed_detail, "DETAIL_FETCH", "2026-09-08T10:02:00.000000Z")
    assert changed["job_id"] == first["job_id"]
    assert changed["run_effect"] == "UPDATED_JOB"
    assert db.conn.execute("SELECT remote_mode, employment_type FROM jobs").fetchone()[:] == ("ONSITE", "FULL_TIME")
    counters = _counters(db, run_id)
    assert (counters["jobs_saved"], counters["jobs_updated"]) == (0, 1)


@pytest.mark.parametrize("content_kind", ["HTML", "STRUCTURED"])
@pytest.mark.parametrize("missing", [True, False], ids=["missing", "unresolved"])
@pytest.mark.parametrize("changed", [False, True], ids=["unchanged", "changed-title"])
def test_later_unresolved_origin_retains_resolved_presence_evidence(db, missing, changed, content_kind):
    from jobscraper.acquisition.origin import OriginResolution, OriginStatus

    origin = OriginResolution(
        status=OriginStatus.RESOLVED, confidence=0.9, origin_provider="LEVER",
        origin_board="fixture", origin_job_id="fx-100", resolved_at=NOW,
    )
    context = dict(content_kind=content_kind, same_host_as_source=True, source_family="EMPLOYER_CAREERS")
    run1, plan1 = _new_run(db)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, origin=origin, **context)
    origin_columns = ("origin_provider", "origin_board", "origin_job_id",
                      "origin_resolution_confidence", "origin_resolution_evidence_json", "origin_resolved_at")
    query = "SELECT " + ", ".join(origin_columns) + " FROM job_sources"
    before = tuple(db.conn.execute(query).fetchone())
    assert before[4] != "{}"
    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(db, run2, plan2, now=LATER)
    later_origin = None if missing else OriginResolution(status=OriginStatus.UNRESOLVED, confidence=0.0)
    observation = _obs(title="Senior Backend Engineer") if changed else _obs()
    second = _ingest(db, observation, rid2, att2, now=LATER, origin=later_origin, **context)
    assert tuple(db.conn.execute(query).fetchone()) == before
    assert second["job_id"] == first["job_id"]
    assert second["run_effect"] == ("UPDATED_JOB" if changed else "UNCHANGED_JOB")
    counters = _counters(db, run2)
    assert (counters["jobs_saved"], counters["jobs_updated"]) == (0, int(changed))


def test_stale_company_signal_does_not_fill_current_job_association(db):
    # A real payload without an employer name legitimately creates a job
    # without a company association; no manual NULL/reset is needed.
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1, now=NOW)
    first = _ingest(db, _obs(company=None), rid1, att1, now=NOW)
    before_job = dict(db.conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (first["job_id"],)
    ).fetchone())
    before_presence = dict(db.conn.execute(
        "SELECT * FROM job_sources WHERE job_id = ?", (first["job_id"],)
    ).fetchone())
    assert before_job["company_id"] is None
    assert first["run_effect"] == "NEW_JOB"

    # An older snapshot with the company name finishes only after the newer
    # nameless snapshot. Its evidence is retained, but cannot fill this job.
    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(db, run2, plan2, now=LATER)
    stale = _ingest(db, _obs(), rid2, att2, now=LATER, observed_at=EARLIER)
    assert stale["job_id"] == first["job_id"]
    assert stale["run_effect"] == "STALE_IGNORED"
    after_job = dict(db.conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (first["job_id"],)
    ).fetchone())
    assert after_job["company_id"] is None
    assert after_job == before_job
    assert dict(db.conn.execute(
        "SELECT * FROM job_sources WHERE job_id = ?", (first["job_id"],)
    ).fetchone()) == before_presence
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 2
    assert db.conn.execute(
        "SELECT COUNT(*) FROM company_resolution_events WHERE observation_id = ?",
        (stale["observation_id"],),
    ).fetchone()[0] == 1
    counters = _counters(db, run2)
    assert counters["jobs_saved"] == 0
    assert counters["jobs_updated"] == 0


def test_unchanged_content_that_reopens_closed_presence_is_an_update(db):
    from jobscraper.pipeline.availability import apply_trusted_detail_closure
    from jobscraper.pipeline.obligations import reconcile_job

    # The fetched posting is structured and on the source host, so the
    # ordinary classifier records employer quality and permits trusted close.
    quality_evidence = {"content_kind": "STRUCTURED", "same_host_as_source": True}
    run1, plan1 = _new_run(db, now=NOW)
    rid1, att1 = _new_request(db, run1, plan1, now=NOW)
    first = _ingest(db, _obs(), rid1, att1, now=NOW, **quality_evidence)
    before_presence = db.conn.execute(
        "SELECT * FROM job_sources WHERE job_id = ?", (first["job_id"],)
    ).fetchone()
    assert before_presence["source_quality_class"] == "EMPLOYER_STRUCTURED_API"
    before_revision = before_presence["content_revision"]
    assert reconcile_job(db.conn, first["job_id"], now=NOW) == "ACTIVE"

    # Use the production closure owner: native target lookup, trust guard,
    # effective-time gate, and canonical reconciliation all run normally.
    closed_at = "2026-09-08T09:30:00.000000Z"
    closure = apply_trusted_detail_closure(
        db.conn,
        source_id="src-feed",
        target_reference="fx-100",
        effective_at=closed_at,
        received_at=closed_at,
        evidence_ref="detail:fx-100:JOB_CLOSED",
    )
    assert closure is not None and closure.accepted and closure.state_changed
    assert closure.new_state == "CLOSED"
    assert reconcile_job(db.conn, first["job_id"], now=closed_at) == "CLOSED"
    db.conn.commit()

    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(db, run2, plan2, now=LATER)
    reopened = _ingest(db, _obs(), rid2, att2, now=LATER, **quality_evidence)
    assert reopened["job_id"] == first["job_id"]
    after_presence = db.conn.execute(
        "SELECT * FROM job_sources WHERE job_id = ?", (first["job_id"],)
    ).fetchone()
    assert after_presence["presence_state"] == "ACTIVE"
    assert after_presence["content_revision"] == before_revision
    assert reconcile_job(db.conn, first["job_id"], now=LATER) == "ACTIVE"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_history WHERE job_id = ? AND change_class = 'JOB_REOPENED'",
        (first["job_id"],),
    ).fetchone()[0] == 1
    assert reopened["run_effect"] == "UPDATED_JOB"
    counters = _counters(db, run2)
    assert counters["jobs_saved"] == 0
    assert counters["jobs_updated"] == 1


def test_same_timestamp_reobservation_uses_latest_accepted_content(db, monkeypatch):
    import jobscraper.pipeline.ingest as ingest_module

    ordinary_id = ingest_module.new_id
    observation_ids = iter(("obs_z", "obs_a", "obs_m"))
    monkeypatch.setattr(ingest_module, "new_id", lambda prefix:
                        next(observation_ids) if prefix == "obs" else ordinary_id(prefix))
    run_id, plan_id = _new_run(db)
    _view(db, run_id, plan_id, _obs(), "LIST_FETCH", NOW)
    changed = _obs(title="Senior Backend Engineer")
    rid, att = _new_request(db, run_id, plan_id, target="https://jobs.example.test/second", now=NOW)
    _ingest(db, changed, rid, att, now=NOW)
    revision = db.conn.execute("SELECT content_revision FROM job_sources").fetchone()[0]
    run2, plan2 = _new_run(db, now=LATER)
    repeated = _view(db, run2, plan2, changed, "LIST_FETCH", LATER)
    assert repeated["run_effect"] == "UNCHANGED_JOB"
    assert db.conn.execute("SELECT content_revision FROM job_sources").fetchone()[0] == revision


def test_missing_native_identity_does_not_share_projection_history(db):
    listing = _obs(source_job_id=None, title="Backend Engineer")
    detail = _obs(source_job_id=None, title="Senior Backend Engineer")
    run_id, plan_id = _new_run(db)
    first = _view(db, run_id, plan_id, listing, "LIST_FETCH", NOW)
    _view(db, run_id, plan_id, detail, "DETAIL_FETCH", "2026-09-08T09:01:00.000000Z")
    # Each sighting without a native id creates a separate presence. Only
    # its job-specific URL establishes their common canonical job.
    run2, plan2 = _new_run(db, now=LATER)
    latest = _view(db, run2, plan2, listing, "LIST_FETCH", LATER)
    assert latest["job_id"] == first["job_id"]
    assert db.conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 3
    assert db.conn.execute("SELECT title FROM jobs").fetchone()[0] == "Backend Engineer"


def test_changed_origin_corroboration_value_is_a_semantic_update(db):
    from jobscraper.acquisition.origin import resolve_origin

    def origin(family, at):
        return resolve_origin(
            discovery_url="https://jobs.example.test/api/jobs?page=1",
            raw_source_url="https://jobs.example.test/api/jobs?page=1",
            canonical_job_url="https://boards.greenhouse.io/fixture/jobs/123",
            application_url=None, final_url=None, observed_at=at,
            fingerprint_family=family,
        )

    run1, plan1 = _new_run(db)
    rid1, att1 = _new_request(db, run1, plan1)
    first = _ingest(db, _obs(), rid1, att1, origin=origin("LEVER", NOW))
    run2, plan2 = _new_run(db, now=LATER)
    rid2, att2 = _new_request(db, run2, plan2, now=LATER)
    second = _ingest(db, _obs(), rid2, att2, now=LATER, origin=origin("ASHBY", LATER))
    assert second["job_id"] == first["job_id"]
    assert second["run_effect"] == "UPDATED_JOB"
    counters = _counters(db, run2)
    assert (counters["jobs_saved"], counters["jobs_updated"]) == (0, 1)
