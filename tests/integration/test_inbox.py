"""S1.8 integration tests: profile-relative Inbox semantics.

Authority: 01 PROD-01 (per-profile state), PROD-02 (durable inbox events,
dedupe, transition matrix), §42 (inbox predicate and triggers).

Proves:

* optimistic concurrency on job_profile_state (stale overwrites rejected);
* the full inbox predicate (score floor, eligibility exclusion,
  disposition suppression, snooze window);
* the PROD-02 transition matrix incl. sticky DISMISSED/ARCHIVED, snooze
  suppression + one-shot expiry per occurrence;
* deterministic dedupe: refresh/restart/duplicate trigger delivery create
  zero additional events;
* per-profile independence (PROD-01);
* snooze-expiry sweep and REOPENED trigger with per-history identity.
"""

from __future__ import annotations

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.inbox.events import (
    expire_snoozes,
    maybe_emit_inbox_event,
)
from jobscraper.inbox.state import DispositionConflict, set_disposition
from jobscraper.pipeline.obligations import reconcile_job
from jobscraper.profiles.core import create_profile

NOW = "2026-09-08T09:00:00.000000Z"
LATER = "2026-09-08T10:00:00.000000Z"
EVEN_LATER = "2026-09-09T09:00:00.000000Z"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "inbox.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    conn.executescript(
        f"""
        INSERT INTO companies (id, name, normalized_name, first_seen_at, created_at, updated_at)
        VALUES ('co-1','Fixture Corp','fixture corp','{NOW}','{NOW}','{NOW}');
        INSERT INTO jobs (id, title, normalized_title, discovered_at, first_seen_at,
            last_seen_at, created_at, updated_at)
        VALUES ('job-1','Backend','backend','{NOW}','{NOW}','{NOW}','{NOW}','{NOW}');
        INSERT INTO jobs (id, title, normalized_title, discovered_at, first_seen_at,
            last_seen_at, created_at, updated_at)
        VALUES ('job-2','Other','other','{NOW}','{NOW}','{NOW}','{NOW}','{NOW}');
        """
    )
    yield database
    database.close()


def _profile(db, name="P1", min_score=25, snapshot=None):
    snap = snapshot or {
        "name": name,
        "keywords": ["backend"],
        "eligible_countries": ["RO", "DE"],
        "remote_rules": {"remote_ok": True},
        "min_score_inbox": min_score,
    }
    pid, _ = create_profile(db.conn, snapshot=snap, now=NOW)
    return pid


def _evaluate(db, job_id, profile_id, *, score=50.0, verdict="ELIGIBLE"):
    db.conn.execute(
        """
        INSERT INTO job_scores (job_id, profile_id, score, breakdown_json, scored_at)
        VALUES (?, ?, ?, '[]', ?)
        ON CONFLICT (job_id, profile_id) DO UPDATE SET score = excluded.score
        """,
        (job_id, profile_id, score, NOW),
    )
    db.conn.execute(
        """
        INSERT INTO job_eligibility (job_id, profile_id, verdict, reason_codes_json,
            evidence_json, evaluated_at)
        VALUES (?, ?, ?, '[]', '{{}}', ?)
        ON CONFLICT (job_id, profile_id) DO UPDATE SET verdict = excluded.verdict
        """,
        (job_id, profile_id, verdict, NOW),
    )
    db.conn.commit()


# ---------------------------------------------------- optimistic concurrency


def test_disposition_optimistic_concurrency(db):
    pid = _profile(db)
    row = set_disposition(
        db.conn, job_id="job-1", profile_id=pid, disposition="SHORTLISTED", now=NOW
    )
    revision = row["row_revision"]
    # two tabs: both hold revision 1; the second write must be rejected
    set_disposition(
        db.conn, job_id="job-1", profile_id=pid, disposition="DISMISSED",
        now=LATER, expected_row_revision=revision,
    )
    with pytest.raises(DispositionConflict):
        set_disposition(
            db.conn, job_id="job-1", profile_id=pid, disposition="ARCHIVED",
            now=EVEN_LATER, expected_row_revision=revision,  # stale
        )
    current = db.conn.execute(
        "SELECT disposition, row_revision FROM job_profile_state WHERE job_id='job-1'"
        " AND profile_id=?", (pid,),
    ).fetchone()
    assert current["disposition"] == "DISMISSED" and current["row_revision"] == 2


# ------------------------------------------------------ predicate + first event


def test_first_eligible_appearance_emits_once_and_dedupes(db):
    pid = _profile(db)
    _evaluate(db, "job-1", pid)
    event_id, outcome = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid,
        event_kind="NEW_ELIGIBLE_APPEARANCE", now=NOW,
    )
    assert event_id is not None and outcome == "EMITTED"
    # refresh / restart / duplicate delivery: zero additional events
    for _ in range(3):
        event_id2, outcome2 = maybe_emit_inbox_event(
            db.conn, job_id="job-1", profile_id=pid,
            event_kind="NEW_ELIGIBLE_APPEARANCE", now=LATER,
        )
        assert event_id2 is None and outcome2 == "DEDUPLICATED"
    count = db.conn.execute(
        "SELECT COUNT(*) FROM job_profile_inbox_events WHERE job_id='job-1' AND profile_id=?",
        (pid,),
    ).fetchone()[0]
    assert count == 1
    state = db.conn.execute(
        "SELECT first_inbox_at, last_inbox_at FROM job_profile_state"
        " WHERE job_id='job-1' AND profile_id=?", (pid,),
    ).fetchone()
    assert state["first_inbox_at"] == NOW and state["last_inbox_at"] == NOW


def test_predicate_gates_on_score_and_eligibility(db):
    pid = _profile(db, min_score=60)
    _evaluate(db, "job-1", pid, score=30.0)  # below floor
    event_id, outcome = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid,
        event_kind="NEW_ELIGIBLE_APPEARANCE", now=NOW,
    )
    assert event_id is None and outcome == "BELOW_SCORE_FLOOR"
    _evaluate(db, "job-1", pid, score=80.0, verdict="INELIGIBLE")
    event_id, outcome = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid,
        event_kind="NEW_ELIGIBLE_APPEARANCE", now=NOW,
    )
    assert event_id is None and outcome == "INELIGIBLE_EXCLUDED"


# --------------------------------------------------------- transition matrix


def test_meaningful_change_dedupes_per_content_revision(db):
    pid = _profile(db)
    _evaluate(db, "job-1", pid)
    e1, _ = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="NEW_ELIGIBLE_APPEARANCE", now=NOW
    )
    assert e1
    e2, out2 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=2, now=LATER,
    )
    assert e2 is not None and out2 == "EMITTED"
    # same content revision re-delivered -> deduplicated
    e3, out3 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=2, now=EVEN_LATER,
    )
    assert e3 is None and out3 == "DEDUPLICATED"
    # a NEW content revision -> a new event
    e4, out4 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=3, now=EVEN_LATER,
    )
    assert e4 is not None and out4 == "EMITTED"


def test_snooze_suppresses_and_expiry_emits_once_per_occurrence(db):
    pid = _profile(db)
    _evaluate(db, "job-1", pid)
    maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="NEW_ELIGIBLE_APPEARANCE", now=NOW
    )
    row = set_disposition(
        db.conn, job_id="job-1", profile_id=pid, disposition="SNOOZED",
        snoozed_until="2026-09-08T09:30:00.000000Z", now=NOW,
    )
    snooze_revision = row["row_revision"]
    # ordinary triggers are suppressed while snoozed (unexpired)
    e, out = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=5, now="2026-09-08T09:10:00.000000Z",
    )
    assert e is None and out == "SNOOZED_SUPPRESSED"
    # expiry: at most one occurrence event
    expired = expire_snoozes(db.conn, now="2026-09-08T09:30:00.000000Z")
    assert ("job-1", pid) in expired
    again = expire_snoozes(db.conn, now="2026-09-08T11:00:00.000000Z")
    assert ("job-1", pid) not in again  # same occurrence never re-fires
    kinds = db.conn.execute(
        "SELECT event_kind FROM job_profile_inbox_events WHERE profile_id=?", (pid,)
    ).fetchall()
    assert [k["event_kind"] for k in kinds].count("SNOOZE_EXPIRED") == 1
    # after expiry a NEW meaningful change may surface again
    e, out = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=6, now="2026-09-08T12:00:00.000000Z",
    )
    assert e is not None and out == "EMITTED"
    # a NEW snooze occurrence gets its own expiry identity
    set_disposition(
        db.conn, job_id="job-1", profile_id=pid, disposition="SNOOZED",
        snoozed_until="2026-09-10T00:00:00.000000Z", now="2026-09-08T12:00:00.000000Z",
    )
    expired2 = expire_snoozes(db.conn, now="2026-09-10T01:00:00.000000Z")
    assert ("job-1", pid) in expired2
    kinds = db.conn.execute(
        "SELECT event_kind FROM job_profile_inbox_events WHERE profile_id=?", (pid,)
    ).fetchall()
    assert [k["event_kind"] for k in kinds].count("SNOOZE_EXPIRED") == 2
    assert snooze_revision  # occurrence identity is grounded in the row revision


def test_dismissed_and_archived_are_sticky(db):
    pid = _profile(db)
    _evaluate(db, "job-1", pid)
    set_disposition(db.conn, job_id="job-1", profile_id=pid, disposition="DISMISSED", now=NOW)
    for kind, kwargs in (
        ("MEANINGFUL_CHANGE", {"trigger_content_revision": 2}),
        ("REOPENED", {"trigger_history_id": "jh-1"}),
        ("PROFILE_REVISION_ELIGIBLE", {"profile_revision_id": "profrev-2"}),
    ):
        e, out = maybe_emit_inbox_event(
            db.conn, job_id="job-1", profile_id=pid, event_kind=kind, now=LATER, **kwargs
        )
        assert e is None and out == "DISPOSITION_SUPPRESSED", kind
    # user reversal re-enables surfacing
    set_disposition(db.conn, job_id="job-1", profile_id=pid, disposition="NONE", now=LATER)
    e, out = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=2, now=LATER,
    )
    assert e is not None and out == "EMITTED"
    # archived is equally sticky
    set_disposition(db.conn, job_id="job-1", profile_id=pid, disposition="ARCHIVED", now=LATER)
    e, out = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=3, now=EVEN_LATER,
    )
    assert e is None and out == "DISPOSITION_SUPPRESSED"


def test_profile_revision_eligible_dedupes_per_revision(db):
    pid = _profile(db)
    _evaluate(db, "job-1", pid)
    e1, out1 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="PROFILE_REVISION_ELIGIBLE",
        profile_revision_id="profrev-9", now=NOW,
    )
    assert e1 is not None and out1 == "EMITTED"
    e2, out2 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="PROFILE_REVISION_ELIGIBLE",
        profile_revision_id="profrev-9", now=LATER,
    )
    assert e2 is None and out2 == "DEDUPLICATED"


def test_reopened_uses_history_identity(db):
    pid = _profile(db)
    _evaluate(db, "job-1", pid)
    e1, _ = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="REOPENED",
        trigger_history_id="jh-77", now=NOW,
    )
    assert e1
    e2, out2 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="REOPENED",
        trigger_history_id="jh-77", now=LATER,
    )
    assert e2 is None and out2 == "DEDUPLICATED"
    e3, out3 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=pid, event_kind="REOPENED",
        trigger_history_id="jh-78", now=LATER,
    )
    assert e3 is not None and out3 == "EMITTED"


# ------------------------------------------------------ per-profile isolation


def test_same_job_independent_state_per_profile(db):
    p1 = _profile(db, name="P1")
    p2 = _profile(db, name="P2")
    _evaluate(db, "job-1", p1)
    _evaluate(db, "job-1", p2)
    set_disposition(db.conn, job_id="job-1", profile_id=p1, disposition="SHORTLISTED", now=NOW)
    set_disposition(db.conn, job_id="job-1", profile_id=p2, disposition="DISMISSED", now=NOW)
    e1, out1 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=p1, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=2, now=LATER,
    )
    e2, out2 = maybe_emit_inbox_event(
        db.conn, job_id="job-1", profile_id=p2, event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=2, now=LATER,
    )
    assert e1 is not None and out1 == "EMITTED"  # shortlisted may surface
    assert e2 is None and out2 == "DISPOSITION_SUPPRESSED"  # dismissed may not
