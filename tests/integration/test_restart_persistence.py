"""Restart persistence: the Slice 1 ship condition tail.

Everything durable must survive a full service restart (database closed and
reopened, engine reconstructed) — jobs, presence, dispositions, applications,
inbox events, scores — and the next collection run must continue correctly
(no duplicate identities, no duplicate inbox events, presence refreshed).
"""

import json

from jobscraper.acquisition.registry import AdapterRegistry
from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.domain.profiles import create_profile, current_profile_snapshot
from jobscraper.domain.sources import (
    create_binding,
    create_source,
    seed_builtin_permission_profile,
)
from jobscraper.runtime.engine import RunEngine
from jobscraper.security.netpolicy import make_test_fixture_policy
from jobscraper.workflow.applications import (
    create_application,
    get_application,
    set_application_status,
)
from jobscraper.workflow.inbox import inbox_events_for_profile, set_disposition
from tests.fixtures.fixture_server import FixtureServer

GREENHOUSE_JOBS = {
    "jobs": [
        {
            "id": 2001,
            "title": "Staff Python Engineer",
            "absolute_url": "https://job-boards.example/greenhouse/acme/jobs/2001",
            "updated_at": "2026-08-01T00:00:00Z",
            "first_published": "2026-07-15T00:00:00Z",
            "content": "<p>Staff python engineer. FastAPI, PostgreSQL. Visa sponsorship available."
                       " Berlin, Germany or Remote (EU).</p>",
            "location": {"name": "Berlin, Germany"},
            "offices": [], "departments": [], "metadata": [],
        },
        {
            "id": 2002,
            "title": "Support Engineer",
            "absolute_url": "https://job-boards.example/greenhouse/acme/jobs/2002",
            "updated_at": "2026-08-02T00:00:00Z",
            "first_published": "2026-07-20T00:00:00Z",
            "content": "<p>Customer support. Competitive salary.</p>",
            "location": {"name": "Austin, TX, USA"},
            "offices": [], "departments": [], "metadata": [],
        },
    ],
    "meta": {"total": 2},
}


def _engine(db, data_root):
    return RunEngine(
        AppConfig(data_root=data_root), db, AdapterRegistry(),
        policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
    )


def test_full_user_workflow_survives_restart(db, data_root):
    with FixtureServer() as server:
        server.add_json("/v1/boards/acme/jobs", GREENHOUSE_JOBS)
        seed_builtin_permission_profile(db)
        src = create_source(
            db, display_name="Acme", entry_url=server.base_url + "/v1/boards/acme/jobs",
            canonical_host="127.0.0.1", source_family="greenhouse",
        )
        create_binding(
            db, source_id=src, display_name="GH", adapter_id="greenhouse",
            adapter_version="1.0.0", strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            config={"board": "acme", "base_url": server.base_url},
        )
        profile_id = create_profile(
            db, name="Main", snapshot_overrides={
                "home_country": "DE", "eligible_countries": ["DE", "NL"],
                "keywords": ["python"], "must_keywords": ["python"], "min_score_inbox": 10,
            },
        )

        # --- Session 1: collect, triage, apply. ---
        run1 = _engine(db, data_root).create_run(profile_id=profile_id, source_ids=[src])
        report1 = _engine(db, data_root).execute_run(run1)
        assert report1.status == "SUCCEEDED"

        jobs = {j["title"]: j["id"] for j in db.query("SELECT id, title FROM jobs")}
        staff_job = jobs["Staff Python Engineer"]

        set_disposition(db, job_id=staff_job, profile_id=profile_id, disposition="SHORTLISTED")
        app_id = create_application(
            db, job_id=staff_job, profile_id=profile_id,
            applied_via_url="https://job-boards.example/greenhouse/acme/jobs/2001",
        )
        set_application_status(db, application_id=app_id, status="APPLIED")

        fingerprint_before = {
            "jobs": db.query_one("SELECT COUNT(*) c FROM jobs")["c"],
            "presence": db.query_one("SELECT COUNT(*) c FROM job_sources")["c"],
            "events": len(inbox_events_for_profile(db, profile_id)),
            "app_status": get_application(db, app_id)["status"],
            "disposition": db.query_one(
                "SELECT disposition FROM job_profile_state WHERE job_id=? AND profile_id=?",
                (staff_job, profile_id),
            )["disposition"],
        }
        assert fingerprint_before["jobs"] == 2
        assert fingerprint_before["disposition"] == "SHORTLISTED"

        # --- Restart: close the database, reopen, reconstruct the service. ---
        db.close()
        db2 = Database(db.path)
        try:
            # Forward-only migration is a no-op after restart.
            assert migrate_schema(db2.conn, LATEST_SCHEMA_VERSION) == []

            # All user and corpus state survived verbatim.
            assert db2.query_one("SELECT COUNT(*) c FROM jobs")["c"] == fingerprint_before["jobs"]
            assert db2.query_one("SELECT COUNT(*) c FROM job_sources")["c"] == fingerprint_before["presence"]
            assert len(inbox_events_for_profile(db2, profile_id)) == fingerprint_before["events"]
            assert get_application(db2, app_id)["status"] == "APPLIED"
            assert get_application(db2, app_id)["applied_at"] is not None
            assert db2.query_one(
                "SELECT disposition FROM job_profile_state WHERE job_id=? AND profile_id=?",
                (staff_job, profile_id),
            )["disposition"] == "SHORTLISTED"
            # Snapshot access still works from persisted revisions.
            snapshot = current_profile_snapshot(db2, profile_id)
            assert snapshot["keywords"] == ["python"]

            # --- Session 2: a fresh collection after restart. ---
            run2 = _engine(db2, data_root).create_run(profile_id=profile_id, source_ids=[src])
            report2 = _engine(db2, data_root).execute_run(run2)
            assert report2.status == "SUCCEEDED"

            # No duplicate identities and no duplicate inbox events.
            assert db2.query_one("SELECT COUNT(*) c FROM jobs")["c"] == 2
            assert len(inbox_events_for_profile(db2, profile_id)) == fingerprint_before["events"]
            # Presence refreshed (re-observed), not duplicated.
            assert db2.query_one(
                "SELECT COUNT(*) c FROM job_sources WHERE source_id=? AND source_job_id='2001'",
                (src,),
            )["c"] == 1
            js = db2.query_one(
                "SELECT * FROM job_sources WHERE source_job_id='2001'"
            )
            assert js["presence_state"] == "ACTIVE"
            assert js["last_observation_id"] is not None
            # Immutable history kept both runs' observations.
            assert db2.query_one("SELECT COUNT(*) c FROM job_observations")["c"] == 4

            # --- Restart again mid-workflow: application state still advances. ---
            db2.close()
            db3 = Database(db.path)
            try:
                set_application_status(
                    db=db3, application_id=app_id, status="SCREENING",
                    expected_row_revision=2,
                )
                assert get_application(db3, app_id)["status"] == "SCREENING"
                events = db3.query(
                    "SELECT kind FROM application_events WHERE application_id=? ORDER BY at",
                    (app_id,),
                )
                assert [e["kind"] for e in events] == ["CREATED", "STATUS_CHANGED", "STATUS_CHANGED"]
            finally:
                db3.close()
        finally:
            if db2.conn is not None:
                db2.close()
