"""S1.9 integration tests: applications, events, direct apply (01 §43, PROD-05/06).

Proves:

* application records are separate from canonical jobs (no pipeline
  status in jobs);
* status pipeline transitions with durable application_events, terminal
  statuses closing the record;
* documents are references only (never uploaded);
* a canonical listing closure while an application is active is recorded
  as an application event;
* the direct-apply boundary (PROD-06): opening the best application URL
  is supported, automated submission does not exist; only safelink-approved
  URLs are ever opened (PROD-05).
"""

from __future__ import annotations

import pytest

from jobscraper.applications.applylink import (
    best_application_url,
    open_apply_url,
)
from jobscraper.applications.core import (
    ApplicationStatusError,
    create_application,
    list_applications,
    record_listing_closed_if_applicable,
    update_application,
)
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.obligations import reconcile_job
from jobscraper.profiles.core import create_profile

NOW = "2026-09-08T09:00:00.000000Z"
LATER = "2026-09-08T10:00:00.000000Z"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "apps.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-1','Feed','PUBLIC_FEED','https://jobs.example.test', '{NOW}', '{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}','{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-1','src-1','api','{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, created_at)
        VALUES ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,'{NOW}');
        INSERT INTO companies (id, name, normalized_name, first_seen_at, created_at, updated_at)
        VALUES ('co-1','Fixture Corp','fixture corp','{NOW}','{NOW}','{NOW}');
        INSERT INTO jobs (id, title, normalized_title, discovered_at, first_seen_at, last_seen_at,
            created_at, updated_at)
        VALUES ('job-1','Backend','backend','{NOW}','{NOW}','{NOW}','{NOW}','{NOW}');
        INSERT INTO job_sources (id, job_id, source_id, binding_id, source_job_id, application_url,
            canonical_job_url, first_seen_at, last_seen_at, created_at, updated_at)
        VALUES ('js-1','job-1','src-1','bnd-1','fx-1','https://jobs.example.test/jobs/fx-1/apply',
            'https://jobs.example.test/jobs/fx-1','{NOW}','{NOW}','{NOW}','{NOW}');
        UPDATE jobs SET canonical_provenance_id = 'js-1';
        """
    )
    yield database
    database.close()


def _profile(db):
    return create_profile(
        db.conn, snapshot={"name": "P", "keywords": [], "eligible_countries": [],
                           "remote_rules": {}, "min_score_inbox": 0},
        now=NOW,
    )[0]


# ------------------------------------------------------------- applications


def test_application_lifecycle_with_events(db):
    pid = _profile(db)
    app = create_application(db.conn, job_id="job-1", profile_id=pid, now=NOW)
    assert app["status"] == "PREPARING" and app["closed_at"] is None
    events = db.conn.execute(
        "SELECT kind FROM application_events WHERE application_id = ? ORDER BY at",
        (app["id"],),
    ).fetchall()
    assert [e["kind"] for e in events] == ["CREATED"]

    app2 = update_application(db.conn, app["id"], status="APPLIED", now=LATER)
    assert app2["status"] == "APPLIED"
    app3 = update_application(
        db.conn, app["id"], status="INTERVIEWING", now=LATER,
        next_action_at="2026-09-10T09:00:00.000000Z", next_action_text="Prep for round 2",
    )
    assert app3["next_action_text"] == "Prep for round 2"
    kinds = [
        e["kind"]
        for e in db.conn.execute(
            "SELECT kind FROM application_events WHERE application_id = ? ORDER BY at, id",
            (app["id"],),
        ).fetchall()
    ]
    assert kinds == ["CREATED", "STATUS_CHANGED", "STATUS_CHANGED"]

    terminal = update_application(db.conn, app["id"], status="REJECTED", now=LATER,
                                  outcome="no_fit", outcome_reason="scope")
    assert terminal["closed_at"] is not None
    with pytest.raises(ApplicationStatusError):
        update_application(db.conn, app["id"], status="PREPARING", now=LATER)  # no resurrection
    with pytest.raises(ApplicationStatusError):
        update_application(db.conn, app["id"], status="SHORTLISTED", now=LATER)  # not a status


def test_documents_are_references_only(db):
    pid = _profile(db)
    doc_id = "doc-resume-1"
    db.conn.execute(
        "INSERT INTO documents (id, kind, label, path, sha256, created_at)"
        " VALUES (?, 'RESUME', 'resume v2', 'C:/Users/me/Documents/resume.pdf', ?, ?)",
        (doc_id, "a" * 64, NOW),
    )
    db.conn.commit()
    app = create_application(
        db.conn, job_id="job-1", profile_id=pid, now=NOW, resume_doc_id=doc_id
    )
    assert app["resume_doc_id"] == doc_id
    with pytest.raises(Exception):
        create_application(
            db.conn, job_id="job-1", profile_id=pid, now=LATER, resume_doc_id="doc-missing"
        )


def test_listing_closed_records_application_event(db):
    pid = _profile(db)
    app = create_application(db.conn, job_id="job-1", profile_id=pid, now=NOW)
    update_application(db.conn, app["id"], status="INTERVIEWING", now=LATER)
    # the canonical listing closes (all presences closed)
    db.conn.execute(
        "UPDATE job_sources SET presence_state = 'CLOSED' WHERE job_id = 'job-1'"
    )
    db.conn.commit()
    reconcile_job(db.conn, "job-1", now=LATER)
    assert db.conn.execute("SELECT listing_status FROM jobs WHERE id='job-1'").fetchone()[
        "listing_status"
    ] == "CLOSED"
    kinds = [
        e["kind"]
        for e in db.conn.execute(
            "SELECT kind FROM application_events WHERE application_id = ? ORDER BY at, id",
            (app["id"],),
        ).fetchall()
    ]
    assert "APPLICATION_LISTING_CLOSED" in kinds
    # idempotent: reconciling again does not duplicate the event
    reconcile_job(db.conn, "job-1", now=LATER)
    kinds = [
        e["kind"]
        for e in db.conn.execute(
            "SELECT kind FROM application_events WHERE application_id = ?",
            (app["id"],),
        ).fetchall()
    ]
    assert kinds.count("APPLICATION_LISTING_CLOSED") == 1


def test_list_applications_scoped(db):
    pid = _profile(db)
    create_application(db.conn, job_id="job-1", profile_id=pid, now=NOW)
    rows = list_applications(db.conn, profile_id=pid)
    assert len(rows) == 1 and rows[0]["job_id"] == "job-1"
    assert list_applications(db.conn, profile_id="prof-other") == []


# ------------------------------------------------------------- direct apply


def test_best_application_url_from_canonical_provenance(db):
    url = best_application_url(db.conn, "job-1")
    assert url == "https://jobs.example.test/jobs/fx-1/apply"


def test_unsafe_application_urls_are_never_selected_or_opened(db):
    pid = _profile(db)
    db.conn.execute(
        "UPDATE job_sources SET application_url = 'javascript:alert(1)'"
        " WHERE job_id = 'job-1'"
    )
    db.conn.commit()
    assert best_application_url(db.conn, "job-1") is None
    opened = []
    # PROD-05 + PROD-06: only safe URLs are opened; the only action is opening
    assert open_apply_url("javascript:alert(1)", opener=opened.append) is False
    assert opened == []
    assert open_apply_url("https://jobs.example.test/apply", opener=opened.append) is True
    assert opened == ["https://jobs.example.test/apply"]


def test_applylink_module_has_no_submission_capability():
    # PROD-06 structural boundary: the apply link module cannot submit forms
    import inspect

    from jobscraper.applications import applylink

    source = inspect.getsource(applylink)
    for forbidden in ("requests.post", "http.client", "submit_form", "fill_form"):
        assert forbidden not in source
