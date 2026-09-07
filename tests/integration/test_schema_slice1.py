"""Slice 1 S1.1: domain schema (migrations v3–v9) tests.

Proves:
* migrations 1–9 apply forward-only on a fresh database and are idempotent
  to re-run at latest;
* RUN-18 uniqueness invariants are enforced by the physical schema;
* FK integrity is enforced (with foreign_keys=ON) on the main spine;
* CHECK constraints reject invalid state-plane values (state planes are
  never conflated: run status, request status, disposition, listing
  status, application status);
* downgrade is rejected;
* released migration steps are byte-stable (forward-only discipline).
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    migrate_schema,
)
from jobscraper.db.schema_sql import MIGRATION_STEPS
from jobscraper.ids import new_id
from jobscraper.version import SCHEMA_VERSION

NOW = "2026-09-07T12:00:00.000000Z"

# sha256 of each released step's SQL, enforcing the forward-only rule
# ("a step may never be edited after being committed to a release").
RELEASED_STEP_SHA256 = {
    1: "a9d335d680150958aee9d8169034721c24d171696b42c6260052e5c62ba87550",
    2: "22ff8b0feb9bed5e132a5b3d393faee60f5cfcb0df671c00fd1b764dd5e6312e",
    3: "e96853bf636e090f626b8013c23153a5fdbc746e9de02bd93d0b0ad7da5ae1f1",
    4: "2ed4e2c33778dbb320794da4d9e5499a06d103526e804e85e20c9b57a2e0e7f8",
    5: "7e8fbdf2e13b2820017bb27bf55ec9a6fc8e6ed46eaa1315dfb3a6cbf226f77e",
    6: "886c588512ef16dbb5a06c00fc5d11063fabb193315d6f06c3f902eb4d91e9e6",
    7: "2a125e1e573b1d5001f1e742cc628f9432d43fab0fa6da40e670cbb44d1e7333",
    8: "a8401fddd96de6b4e5d1f25724e62fa1148ccc616fec488e3f711502f59f0ceb",
    9: "2085d628491a5cac0044f9195c0c3c43fa5dce4927b43d2db0ba567aae5455b0",
}


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "s11.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    yield database
    database.close()


def _row(db, table, **cols):
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    db.conn.execute(
        f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values())
    )


def _src_family(db, source_id="src-1", binding_id="bnd-1"):
    """Minimal valid source → binding → revision chain."""
    _row(
        db,
        "sources",
        id=source_id,
        display_name="Fixture Feed",
        source_family="PUBLIC_FEED",
        entry_url="https://example.test/jobs.json",
        canonical_host="example.test",
        created_at=NOW,
        updated_at=NOW,
    )
    _row(
        db,
        "adapter_definitions",
        adapter_id="fixture_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        manifest_json="{}",
        created_at=NOW,
    )
    _row(
        db,
        "adapter_permission_profiles",
        id="perm-1",
        display_name="default",
        created_at=NOW,
    )
    _row(
        db,
        "adapter_permission_profile_revisions",
        id="permrev-1",
        permission_profile_id="perm-1",
        revision=1,
        policy_json="{}",
        created_at=NOW,
    )
    _row(
        db,
        "source_adapter_bindings",
        id=binding_id,
        source_id=source_id,
        display_name="fixture api",
        created_at=NOW,
    )
    _row(
        db,
        "source_adapter_binding_revisions",
        id="bndrev-1",
        binding_id=binding_id,
        revision=1,
        adapter_id="fixture_feed",
        adapter_version="1.0.0",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
        created_at=NOW,
    )


def _run_family(db, run_id="run-1", request_id="req-1"):
    _row(db, "scrape_runs", id=run_id, created_at=NOW)
    _row(
        db,
        "run_source_plans",
        id="rsp-1",
        run_id=run_id,
        source_id="src-1",
        source_plan_group_id="grp-1",
        fallback_rank=0,
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="fixture_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
        created_at=NOW,
    )
    _row(
        db,
        "scrape_requests",
        id=request_id,
        run_id=run_id,
        run_source_plan_id="rsp-1",
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        request_unique_key="rk-1",
        created_at=NOW,
        updated_at=NOW,
    )


def _job_family(db, job_id="job-1"):
    _row(
        db,
        "companies",
        id="co-1",
        name="Fixture Corp",
        normalized_name="fixture corp",
        first_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    _row(
        db,
        "jobs",
        id=job_id,
        title="Backend Engineer",
        normalized_title="backend engineer",
        discovered_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


# ------------------------------------------------------------- application


def test_migrations_apply_and_versions_recorded(tmp_path):
    db = Database(tmp_path / "fresh.db")
    applied = migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    assert applied == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert LATEST_SCHEMA_VERSION == SCHEMA_VERSION == 9
    # Re-running at latest is a no-op.
    assert migrate_schema(db.conn, LATEST_SCHEMA_VERSION) == []
    db.close()


def test_downgrade_rejected(tmp_path):
    db = Database(tmp_path / "down.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    with pytest.raises(MigrationError):
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION - 1)
    db.close()


def test_released_steps_are_byte_stable():
    for version, _name, sql in MIGRATION_STEPS:
        digest = hashlib.sha256(sql.encode()).hexdigest()
        pinned = RELEASED_STEP_SHA256[version]
        assert digest == pinned, f"migration step {version} was edited"


# ------------------------------------------------- RUN-18 uniqueness invariants


def test_request_unique_key_scoped_to_run(db):
    _src_family(db)
    _run_family(db, request_id="req-1")
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "scrape_requests",
            id="req-2",
            run_id="run-1",
            source_id="src-1",
            binding_id="bnd-1",
            request_type="LIST_FETCH",
            request_unique_key="rk-1",
            created_at=NOW,
            updated_at=NOW,
        )


def test_observation_unique_key_scoped_to_request(db):
    _src_family(db)
    _run_family(db)
    _row(db, "request_attempts", attempt_id="att-1", request_id="req-1", started_at=NOW, created_at=NOW)
    _row(
        db,
        "job_observations",
        id="obs-1",
        run_id="run-1",
        request_id="req-1",
        attempt_id="att-1",
        source_id="src-1",
        binding_id="bnd-1",
        adapter_id="fixture_feed",
        adapter_version="1.0.0",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        observed_at=NOW,
        observation_unique_key="ouk-1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "job_observations",
            id="obs-3",
            run_id="run-1",
            request_id="req-1",
            source_id="src-1",
            binding_id="bnd-1",
            adapter_id="fixture_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            observed_at=NOW,
            observation_unique_key="ouk-1",
        )


def test_binding_and_permission_revision_uniqueness(db):
    _src_family(db)
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "source_adapter_binding_revisions",
            id="bndrev-2",
            binding_id="bnd-1",
            revision=1,
            adapter_id="fixture_feed",
            adapter_version="1.0.0",
            strategy="HTTP_HTML",
            execution_class="HTTP",
            permission_profile_id="perm-1",
            permission_profile_revision=1,
            created_at=NOW,
        )
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "adapter_permission_profile_revisions",
            id="permrev-2",
            permission_profile_id="perm-1",
            revision=1,
            policy_json="{}",
            created_at=NOW,
        )


def test_run_source_plan_fallback_rank_uniqueness(db):
    _src_family(db)
    _row(db, "scrape_runs", id="run-1", created_at=NOW)
    _row(
        db,
        "run_source_plans",
        id="rsp-1",
        run_id="run-1",
        source_id="src-1",
        source_plan_group_id="grp-1",
        fallback_rank=0,
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="fixture_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
        created_at=NOW,
    )
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "run_source_plans",
            id="rsp-2",
            run_id="run-1",
            source_id="src-1",
            source_plan_group_id="grp-1",
            fallback_rank=0,
            binding_id="bnd-1",
            binding_revision_id="bndrev-1",
            adapter_id="fixture_feed",
            adapter_version="1.0.0",
            adapter_api_version="1",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            permission_profile_id="perm-1",
            permission_profile_revision=1,
            created_at=NOW,
        )


def test_profile_relative_uniqueness(db):
    _src_family(db)
    _job_family(db)
    _row(db, "search_profiles", id="prof-1", name="Default", created_at=NOW, updated_at=NOW)
    _row(
        db,
        "job_profile_state",
        job_id="job-1",
        profile_id="prof-1",
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "job_profile_state",
            job_id="job-1",
            profile_id="prof-1",
            created_at=NOW,
            updated_at=NOW,
        )
    _row(
        db,
        "job_profile_inbox_events",
        id="ibe-1",
        job_id="job-1",
        profile_id="prof-1",
        event_kind="NEW_ELIGIBLE_APPEARANCE",
        created_at=NOW,
        dedupe_key="dk-1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "job_profile_inbox_events",
            id="ibe-2",
            job_id="job-1",
            profile_id="prof-1",
            event_kind="MEANINGFUL_CHANGE",
            created_at=NOW,
            dedupe_key="dk-1",
        )


def test_enumeration_coverage_uniqueness(db):
    _src_family(db)
    _row(db, "scrape_runs", id="run-1", created_at=NOW)
    _row(
        db,
        "run_source_plans",
        id="rsp-1",
        run_id="run-1",
        source_id="src-1",
        source_plan_group_id="grp-1",
        fallback_rank=0,
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="fixture_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
        created_at=NOW,
    )
    _row(
        db,
        "enumeration_coverage",
        id="cov-1",
        run_source_plan_id="rsp-1",
        source_id="src-1",
        binding_id="bnd-1",
        scope_key="full",
        generation_key="gen-1",
        created_at=NOW,
    )
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "enumeration_coverage",
            id="cov-2",
            run_source_plan_id="rsp-1",
            source_id="src-1",
            binding_id="bnd-1",
            scope_key="full",
            generation_key="gen-1",
            created_at=NOW,
        )


def test_job_sources_identity_uniqueness(db):
    _src_family(db)
    _job_family(db)
    _row(
        db,
        "job_sources",
        id="js-1",
        job_id="job-1",
        source_id="src-1",
        binding_id="bnd-1",
        source_job_id="fx-100",
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "job_sources",
            id="js-2",
            job_id="job-1",
            source_id="src-1",
            binding_id="bnd-1",
            source_job_id="fx-100",
            first_seen_at=NOW,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )


# ------------------------------------------------------- FK integrity / checks


def test_fk_integrity_on_the_spine(db):
    _src_family(db)
    _run_family(db)
    # observation must reference a real request
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "job_observations",
            id="obs-x",
            run_id="run-1",
            request_id="missing-request",
            source_id="src-1",
            binding_id="bnd-1",
            adapter_id="fixture_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            observed_at=NOW,
            observation_unique_key="k",
        )
    # job_sources must reference a real job
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "job_sources",
            id="js-x",
            job_id="missing-job",
            source_id="src-1",
            binding_id="bnd-1",
            first_seen_at=NOW,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    # binding revision must reference a declared adapter definition
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "source_adapter_binding_revisions",
            id="bndrev-x",
            binding_id="bnd-1",
            revision=2,
            adapter_id="unknown-adapter",
            adapter_version="9.9.9",
            strategy="HTTP_HTML",
            execution_class="HTTP",
            permission_profile_id="perm-1",
            permission_profile_revision=1,
            created_at=NOW,
        )


def test_state_planes_are_constrained_and_separate(db):
    _src_family(db)
    _run_family(db)
    # invalid run status
    with pytest.raises(sqlite3.IntegrityError):
        _row(db, "scrape_runs", id="run-9", status="SHORTLISTED", created_at=NOW)
    # invalid request status
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE scrape_requests SET status='ELIGIBLE' WHERE id='req-1'"
        )
    _job_family(db)
    _row(db, "search_profiles", id="prof-1", name="Default", created_at=NOW, updated_at=NOW)
    # invalid disposition value
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "job_profile_state",
            job_id="job-1",
            profile_id="prof-1",
            disposition="RUNNING",
            created_at=NOW,
            updated_at=NOW,
        )
    # invalid listing status on canonical job
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE jobs SET listing_status='APPLIED' WHERE id='job-1'")
    # invalid application status
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "applications",
            id="app-x",
            job_id="job-1",
            profile_id="prof-1",
            status="SHORTLISTED",
            created_at=NOW,
            updated_at=NOW,
        )


def test_ids_are_prefixed_time_sortable_and_unique():
    seen = set()
    last = ""
    for _ in range(500):
        value = new_id("run")
        assert value.startswith("run_")
        assert len(value) == len("run_") + 26
        assert value not in seen
        seen.add(value)
        # the timestamp portion (first 10 chars after the prefix) is
        # non-decreasing within a tight loop; the random suffix is not ordered
        assert value[4:14] >= last
        last = value[4:14]
    with pytest.raises(ValueError):
        new_id("bad prefix!")
