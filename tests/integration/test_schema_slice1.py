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
    10: "053df479f2c0f1dcf431b73e38aa000c2fa8878b630552c528787759534f5136",
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
    # Slice 1 shipped through v10.  The *exact* current head is owned by the
    # currently executing slice's schema test (Slice 2:
    # tests/integration/test_schema_slice2.py), which keeps "every appended
    # step is a deliberate, pinned change" intact.
    assert LATEST_SCHEMA_VERSION == SCHEMA_VERSION
    assert LATEST_SCHEMA_VERSION >= 10
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
    # Slice 1 owns the byte pins for v1-v10; later slices append their own
    # pinned steps in their own schema test (Slice 2:
    # tests/integration/test_schema_slice2.py::test_slice1_released_bytes_are_untouched
    # re-pins the exact same digests so nobody can quietly edit this range).
    for version, _name, sql in MIGRATION_STEPS:
        if version not in RELEASED_STEP_SHA256:
            continue
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


# ================================ S1.1 corrective (v10) regression tests
#
# Written RED against schema v9 for the two architectural-review findings and
# the three referential-integrity questions, each verified against its owning
# normative requirement before implementation:
#
# * companies: 01 §33.1 — normalized name is one resolution signal among six;
#   "Weak evidence must not aggressively merge companies." A global
#   UNIQUE(normalized_name) is schema-level forced merging.
# * job_sources: 03 RUN-15 — "(source_id, source_job_id) is strong identity
#   with a reuse guard"; the generation separates *genuine* reuse, so one
#   current generation may never attach to two canonical jobs.
# * permission pins: 02 §9.1 + 03 RUN-02 rule 8 — pinned permission-profile
#   revisions must resolve to an immutable adapter_permission_profile_revisions
#   row ("Retired historical bindings/versions remain resolvable").
# * field_evidence: 03 §30 — field evidence is a link in the evidence chain
#   supporting a JobObservation; orphan evidence is not representable.
# * job_sources.last_absence_coverage_id: 03 RUN-13 — each absence-driven
#   presence transition records the enumeration_coverage.id that authorized it;
#   that attribution must reference a real coverage generation.


def test_two_companies_may_share_normalized_name(db):
    """Finding 1: normalized company name is a lookup signal, not identity."""
    _job_family(db)  # creates co-1 "Fixture Corp" / "fixture corp"
    _row(
        db,
        "companies",
        id="co-2",
        name="Fixture Corp SRL",
        normalized_name="fixture corp",  # same normalized name, distinct company
        first_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    # Lookup performance is preserved via a non-unique index.
    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM companies WHERE normalized_name = ?",
        ("fixture corp",),
    ).fetchall()
    assert any("idx_companies_normalized_name" in (p[3] or "") for p in plan)
    rows = db.conn.execute(
        "SELECT id FROM companies WHERE normalized_name = 'fixture corp' ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == ["co-1", "co-2"]


def _second_job(db):
    _row(
        db,
        "jobs",
        id="job-2",
        title="Other Role",
        normalized_title="other role",
        discovered_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _presence(db, row_id, job_id, source_job_id, generation=None):
    cols = dict(
        id=row_id,
        job_id=job_id,
        source_id="src-1",
        binding_id="bnd-1",
        source_job_id=source_job_id,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    if generation is not None:
        cols["source_identity_generation"] = generation
    _row(db, "job_sources", **cols)


def test_native_identity_generation_cannot_attach_to_two_canonical_jobs(db):
    """Finding 2 RED case 1: one current generation belongs to one job."""
    _src_family(db)
    _job_family(db)
    _second_job(db)
    _presence(db, "js-1", "job-1", "fx-100", generation=1)
    # A second canonical job claiming the SAME (source, native id, generation)
    # is a silent identity split — RUN-15 forbids it.
    with pytest.raises(sqlite3.IntegrityError):
        _presence(db, "js-2", "job-2", "fx-100", generation=1)


def test_native_identity_reuse_via_new_generation_is_allowed(db):
    """Finding 2 RED case 2: genuine reuse increments the generation."""
    _src_family(db)
    _job_family(db)
    _second_job(db)
    _presence(db, "js-1", "job-1", "fx-100", generation=1)
    # Reuse-guard split: same native id, next generation, different job — legal.
    _presence(db, "js-2", "job-2", "fx-100", generation=2)
    states = db.conn.execute(
        "SELECT job_id, source_identity_generation FROM job_sources"
        " WHERE source_job_id = 'fx-100' ORDER BY source_identity_generation"
    ).fetchall()
    assert [tuple(r) for r in states] == [("job-1", 1), ("job-2", 2)]


def test_binding_revision_permission_pin_must_resolve(db):
    """Q1: a binding revision's permission pin must reference a real
    immutable permission-profile revision (02 §9.1, RUN-02 rule 8)."""
    _src_family(db)  # creates perm-1 revision 1 only
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "source_adapter_binding_revisions",
            id="bndrev-x",
            binding_id="bnd-1",
            revision=2,
            adapter_id="fixture_feed",
            adapter_version="1.0.0",
            strategy="HTTP_HTML",
            execution_class="HTTP",
            permission_profile_id="perm-1",
            permission_profile_revision=7,  # never created
            created_at=NOW,
        )


def test_run_source_plan_permission_pin_must_resolve(db):
    """Q1 (same invariant at the plan pin site): run_source_plans cannot pin
    an unresolvable permission-profile revision (RUN-02 rule 8)."""
    _src_family(db)
    _run_family(db)  # rsp-1 pins (perm-1, revision 1) — valid
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE run_source_plans SET permission_profile_revision = 99"
            " WHERE id = 'rsp-1'"
        )


def test_field_evidence_requires_real_observation(db):
    """Q2: field evidence is bound to its JobObservation (03 §30)."""
    _src_family(db)
    _run_family(db)
    with pytest.raises(sqlite3.IntegrityError):
        _row(
            db,
            "field_evidence",
            id="fe-x",
            observation_id="missing-observation",
            field_name="title",
            created_at=NOW,
        )


def test_absence_attribution_requires_real_coverage(db):
    """Q3: an absence-driven presence transition must attribute a real
    enumeration_coverage generation (03 RUN-13)."""
    _src_family(db)
    _job_family(db)
    _presence(db, "js-1", "job-1", "fx-100", generation=1)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE job_sources SET last_absence_coverage_id = 'missing-cov'"
            " WHERE id = 'js-1'"
        )


def test_v10_rebuild_preserves_existing_v9_data(tmp_path):
    """Migration compatibility: a populated v9 database migrates to v10 with
    every rebuilt table's rows preserved, FK integrity clean, and the new
    invariants active afterwards."""
    db = Database(tmp_path / "v9.db")
    migrate_schema(db.conn, 9)
    conn = db.conn
    # A coherent populated family across all five rebuilt tables.
    for table, cols in [
        ("sources", dict(id="src-1", display_name="Feed", source_family="PUBLIC_FEED",
                         entry_url="https://example.test/jobs.json", created_at=NOW, updated_at=NOW)),
        ("adapter_definitions", dict(adapter_id="fixture_feed", adapter_version="1.0.0",
                                     adapter_api_version="1", manifest_json="{}", created_at=NOW)),
        ("adapter_permission_profiles", dict(id="perm-1", display_name="default", created_at=NOW)),
        ("adapter_permission_profile_revisions", dict(id="permrev-1", permission_profile_id="perm-1",
                                                      revision=1, policy_json="{}", created_at=NOW)),
        ("source_adapter_bindings", dict(id="bnd-1", source_id="src-1", display_name="api",
                                         created_at=NOW)),
        ("companies", dict(id="co-1", name="Fixture Corp", normalized_name="fixture corp",
                           first_seen_at=NOW, created_at=NOW, updated_at=NOW)),
        ("jobs", dict(id="job-1", title="Backend Engineer", normalized_title="backend engineer",
                      discovered_at=NOW, first_seen_at=NOW, last_seen_at=NOW,
                      created_at=NOW, updated_at=NOW)),
        ("scrape_runs", dict(id="run-1", created_at=NOW)),
    ]:
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions (id, binding_id, revision,"
        " adapter_id, adapter_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at) VALUES"
        " ('bndrev-1','bnd-1',1,'fixture_feed','1.0.0',"
        "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO run_source_plans (id, run_id, source_id, source_plan_group_id,"
        " fallback_rank, binding_id, binding_revision_id, adapter_id, adapter_version,"
        " adapter_api_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at) VALUES"
        " ('rsp-1','run-1','src-1','grp-1',0,'bnd-1','bndrev-1','fixture_feed','1.0.0',"
        "'1','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO scrape_requests (id, run_id, run_source_plan_id, source_id,"
        " binding_id, request_type, request_unique_key, created_at, updated_at)"
        " VALUES ('req-1','run-1','rsp-1','src-1','bnd-1','LIST_FETCH','rk-1',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO request_attempts (attempt_id, request_id, started_at, created_at)"
        " VALUES ('att-1','req-1',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO job_observations (id, run_id, request_id, attempt_id, source_id,"
        " binding_id, adapter_id, adapter_version, strategy, execution_class,"
        " observed_at, observation_unique_key, source_job_id)"
        " VALUES ('obs-1','run-1','req-1','att-1','src-1','bnd-1','fixture_feed','1.0.0',"
        "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP',?,'ouk-1','fx-100')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO field_evidence (id, observation_id, field_name, created_at)"
        " VALUES ('fe-1','obs-1','title',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO job_sources (id, job_id, source_id, binding_id, source_job_id,"
        " first_seen_at, last_seen_at, created_at, updated_at)"
        " VALUES ('js-1','job-1','src-1','bnd-1','fx-100',?,?,?,?)",
        (NOW, NOW, NOW, NOW),
    )
    conn.commit()

    # v9 defect shape was legal then: same normalized name would fail.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO companies (id, name, normalized_name, first_seen_at,"
            " created_at, updated_at) VALUES ('co-2','Other','fixture corp',?,?,?)",
            (NOW, NOW, NOW),
        )
        conn.commit()

    applied = migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    assert applied[0] == 10
    assert applied == list(range(10, LATEST_SCHEMA_VERSION + 1))

    # Rows survived the rebuilds.
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_adapter_binding_revisions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM run_source_plans").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM field_evidence").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # jobs.company_id still references the rebuilt companies table.
    conn.execute("UPDATE jobs SET company_id='co-1' WHERE id='job-1'")
    conn.commit()

    # New invariants are active post-migration.
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, first_seen_at,"
        " created_at, updated_at) VALUES ('co-2','Other','fixture corp',?,?,?)",
        (NOW, NOW, NOW),
    )
    conn.commit()  # duplicate normalized names now legal (01 §33.1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_sources (id, job_id, source_id, binding_id, source_job_id,"
            " first_seen_at, last_seen_at, created_at, updated_at)"
            " VALUES ('js-2','job-1','src-1','bnd-1','fx-100',?,?,?,?)",
            (NOW, NOW, NOW, NOW),
        )
        conn.commit()  # same (source, native id, generation) — RUN-15 violation
    db.close()


def test_v10_aborts_fail_closed_on_duplicate_native_identity(tmp_path):
    """Fail-closed proof: a v9 database that already contains two presence
    rows claiming the same (source_id, source_job_id, generation) cannot
    silently migrate — v10 aborts, the database stays at v9 with data and
    FK enforcement intact, and an operator must resolve the duplicates."""
    db = Database(tmp_path / "dup.db")
    migrate_schema(db.conn, 9)
    conn = db.conn
    for table, cols in [
        ("sources", dict(id="src-1", display_name="Feed", source_family="PUBLIC_FEED",
                         entry_url="https://example.test/jobs.json", created_at=NOW, updated_at=NOW)),
        ("adapter_definitions", dict(adapter_id="fixture_feed", adapter_version="1.0.0",
                                     adapter_api_version="1", manifest_json="{}", created_at=NOW)),
        ("adapter_permission_profiles", dict(id="perm-1", display_name="default", created_at=NOW)),
        ("adapter_permission_profile_revisions", dict(id="permrev-1", permission_profile_id="perm-1",
                                                      revision=1, policy_json="{}", created_at=NOW)),
        ("source_adapter_bindings", dict(id="bnd-1", source_id="src-1", display_name="api",
                                         created_at=NOW)),
    ]:
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values()))
    for job_id in ("job-1", "job-2"):
        conn.execute(
            "INSERT INTO jobs (id, title, normalized_title, discovered_at, first_seen_at,"
            " last_seen_at, created_at, updated_at) VALUES (?,?,?, ?,?,?,?,?)",
            (job_id, "T", "t", NOW, NOW, NOW, NOW, NOW),
        )
    for row_id, job_id in (("js-1", "job-1"), ("js-2", "job-2")):
        conn.execute(
            "INSERT INTO job_sources (id, job_id, source_id, binding_id, source_job_id,"
            " first_seen_at, last_seen_at, created_at, updated_at)"
            " VALUES (?,?,?,?, 'fx-100', ?,?,?,?)",
            (row_id, job_id, "src-1", "bnd-1", NOW, NOW, NOW, NOW),
        )
    conn.commit()

    from jobscraper.db.migrations import current_schema_version

    with pytest.raises(sqlite3.IntegrityError):
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION)

    # Nothing committed: still at v9, duplicate rows intact for manual
    # resolution, FK enforcement restored and functional.
    assert current_schema_version(conn) == 9
    assert conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 2
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_sources (id, job_id, source_id, binding_id, first_seen_at,"
            " last_seen_at, created_at, updated_at)"
            " VALUES ('js-x','job-1','missing-source','bnd-1',?,?,?,?)",
            (NOW, NOW, NOW, NOW),
        )
        conn.commit()
    db.close()
