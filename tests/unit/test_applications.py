"""Application lifecycle tests (module 01 section 7.3).

Key invariants: application status is independent of listing status; only
http/https links; every transition audited; optimistic concurrency; merge
re-points but never collapses records.
"""

import json

import pytest

from jobscraper.domain.profiles import create_profile
from jobscraper.domain.sources import create_source
from jobscraper.normalize.company import resolve_company
from jobscraper.runtime.entity_resolution import (
    merge_jobs,
    resolve_observation,
    undo_merge,
)
from jobscraper.workflow.applications import (
    InvalidTransition,
    StaleRowRevision,
    applications_for_job,
    application_events,
    create_application,
    get_application,
    set_application_status,
    update_application,
)


def _mk_job(db):
    src = create_source(
        db, display_name="S", entry_url="https://boards.example/x",
        canonical_host="boards.example", source_family="greenhouse",
    )
    company_id = resolve_company(db, name="Acme", domain=None)
    decision = resolve_observation(
        db,
        observation_row={"source_id": src, "source_job_id": "j-1", "canonical_url_candidate": None},
        normalized={"title": "Senior Python Engineer", "company": "Acme"},
        company_id=company_id,
    )
    return decision.job_id


def test_lifecycle_with_audit_trail(db):
    job_id = _mk_job(db)
    app_id = create_application(
        db, job_id=job_id, applied_via_url="https://boards.example/x/jobs/1/apply",
        notes_md="referral",
    )
    assert get_application(db, app_id)["status"] == "PREPARING"

    status, rev = set_application_status(db, application_id=app_id, status="APPLIED")
    assert status == "APPLIED" and rev == 2
    row = get_application(db, app_id)
    assert row["applied_at"] is not None

    set_application_status(db, application_id=app_id, status="SCREENING")
    set_application_status(db, application_id=app_id, status="INTERVIEWING")
    set_application_status(db, application_id=app_id, status="OFFER")
    set_application_status(db, application_id=app_id, status="ACCEPTED")

    events = application_events(db, app_id)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "CREATED"
    assert kinds.count("STATUS_CHANGED") == 5
    first_change = json.loads(events[1]["detail_json"])
    assert first_change == {"from": "PREPARING", "to": "APPLIED"}


def test_invalid_transition_rejected(db):
    job_id = _mk_job(db)
    app_id = create_application(db, job_id=job_id)
    with pytest.raises(InvalidTransition):
        set_application_status(db, application_id=app_id, status="INTERVIEWING")  # PREPARING -> INTERVIEWING
    assert get_application(db, app_id)["status"] == "PREPARING"
    set_application_status(db, application_id=app_id, status="APPLIED")
    with pytest.raises(InvalidTransition):
        # ACCEPTED requires OFFER first.
        set_application_status(db, application_id=app_id, status="ACCEPTED")
    # Terminal states only close.
    set_application_status(db, application_id=app_id, status="GHOSTED")
    with pytest.raises(InvalidTransition):
        set_application_status(db, application_id=app_id, status="APPLIED")
    set_application_status(db, application_id=app_id, status="CLOSED")
    with pytest.raises(InvalidTransition):
        set_application_status(db, application_id=app_id, status="APPLIED")


def test_application_independent_of_listing_status(db):
    """A closed listing does not touch the application (spec 7.3)."""
    job_id = _mk_job(db)
    app_id = create_application(db, job_id=job_id)
    set_application_status(db, application_id=app_id, status="APPLIED")
    set_application_status(db, application_id=app_id, status="INTERVIEWING")
    db.execute("UPDATE jobs SET listing_status='CLOSED' WHERE id=?", (job_id,))
    row = get_application(db, app_id)
    assert row["status"] == "INTERVIEWING"  # unchanged
    # And the job row carries no application state and vice versa.
    job = db.query_one("SELECT listing_status FROM jobs WHERE id=?", (job_id,))
    assert job["listing_status"] == "CLOSED"


def test_only_http_https_application_urls(db):
    job_id = _mk_job(db)
    with pytest.raises(ValueError):
        create_application(db, job_id=job_id, applied_via_url="ftp://boards.example/apply")
    with pytest.raises(ValueError):
        create_application(db, job_id=job_id, applied_via_url="javascript:alert(1)")
    app_id = create_application(
        db, job_id=job_id, applied_via_url="https://boards.example/apply"
    )
    assert get_application(db, app_id)["applied_via_url"] == "https://boards.example/apply"


def test_optimistic_concurrency(db):
    job_id = _mk_job(db)
    app_id = create_application(db, job_id=job_id)
    _, rev = set_application_status(db, application_id=app_id, status="APPLIED")
    with pytest.raises(StaleRowRevision):
        set_application_status(
            db, application_id=app_id, status="SCREENING", expected_row_revision=rev - 1
        )
    # A stale edit must not have written an event.
    assert [e["kind"] for e in application_events(db, app_id)] == ["CREATED", "STATUS_CHANGED"]


def test_user_field_updates_never_touch_status(db):
    job_id = _mk_job(db)
    app_id = create_application(db, job_id=job_id)
    rev = update_application(
        db, application_id=app_id, notes_md="pinged recruiter",
        next_action_at="2026-09-10T09:00:00Z", next_action_text="follow up",
        salary_asked="EUR 95k",
    )
    assert rev == 2
    row = get_application(db, app_id)
    assert row["status"] == "PREPARING"  # status untouched
    assert row["next_action_text"] == "follow up"


def test_multiple_applications_same_job_never_collapsed(db):
    job_id = _mk_job(db)
    profile_id = create_profile(db, name="P")
    a1 = create_application(db, job_id=job_id, profile_id=profile_id)
    a2 = create_application(db, job_id=job_id, profile_id=profile_id)
    assert a1 != a2
    assert len(applications_for_job(db, job_id)) == 2


def test_merge_repoints_applications_and_undo_restores(db):
    job_a = _mk_job(db)
    job_b = _mk_job(db)
    app_b = create_application(db, job_id=job_b)
    set_application_status(db, application_id=app_b, status="APPLIED")

    merge_id = merge_jobs(db, kept_job_id=job_a, absorbed_job_id=job_b, stage="user", reason_code="duplicate")
    # Re-pointed to the kept job, record intact.
    assert get_application(db, app_b)["job_id"] == job_a
    assert get_application(db, app_b)["status"] == "APPLIED"

    assert undo_merge(db, merge_id) is True
    assert get_application(db, app_b)["job_id"] == job_b
    assert get_application(db, app_b)["status"] == "APPLIED"
