"""Inbox: durable per-profile triage events and disposition transitions.

Authority: module 01 sections 42, PROD-01, PROD-02 (explicit predicate and
transition table); RUN-16.

The canonical Inbox predicate:

    eligible_for_inbox =
      trigger_is_due
      AND score >= profile.min_score_inbox
      AND eligibility != INELIGIBLE
      AND disposition NOT IN {DISMISSED, ARCHIVED}
      AND (disposition != SNOOZED OR snoozed_until <= now)

Durable resurfacing identity: each event carries a deterministic dedupe_key
so the same trigger occurrence never resurfaces after refresh/restart.
"""

from __future__ import annotations

import json
import secrets

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import utc_now_s

DISPOSITIONS = ("NONE", "SHORTLISTED", "DISMISSED", "SNOOZED", "ARCHIVED")

TRIGGER_KINDS = (
    "NEW_ELIGIBLE_APPEARANCE",
    "MEANINGFUL_CHANGE",
    "REOPENED",
    "SNOOZE_EXPIRED",
    "PROFILE_REVISION_ELIGIBLE",
)


def ensure_profile_state(db: Database, job_id: str, profile_id: str, *, now: str | None = None) -> None:
    now = now or utc_now_s()
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT OR IGNORE INTO job_profile_state(job_id, profile_id, disposition, created_at, updated_at)"
            " VALUES (?,?, 'NONE', ?, ?)",
            (job_id, profile_id, now, now),
        )


def get_profile_state(db: Database, job_id: str, profile_id: str):
    return db.query_one(
        "SELECT * FROM job_profile_state WHERE job_id=? AND profile_id=?", (job_id, profile_id)
    )


def set_disposition(
    db: Database,
    *,
    job_id: str,
    profile_id: str,
    disposition: str,
    dismissed_reason: str | None = None,
    snoozed_until: str | None = None,
    expected_row_revision: int | None = None,
    now: str | None = None,
) -> tuple[str, int]:
    """Set a profile-relative disposition with optimistic concurrency.

    ``expected_row_revision`` protects multi-tab edits from silent stale
    overwrites (PROD-02). Returns (disposition, row_revision).
    """
    if disposition not in DISPOSITIONS:
        raise ValueError(f"invalid disposition {disposition}")
    now = now or utc_now_s()
    ensure_profile_state(db, job_id, profile_id, now=now)
    with immediate_transaction(db.conn) as tx:
        row = tx.execute(
            "SELECT row_revision FROM job_profile_state WHERE job_id=? AND profile_id=?",
            (job_id, profile_id),
        ).fetchone()
        current_rev = row["row_revision"]
        if expected_row_revision is not None and expected_row_revision != current_rev:
            raise StaleRowRevision(
                f"row revision mismatch: expected {expected_row_revision}, current {current_rev}"
            )
        next_rev = current_rev + 1
        tx.execute(
            "UPDATE job_profile_state SET disposition=?, dismissed_reason=?, snoozed_until=?,"
            " triaged_at=CASE WHEN ? != 'NONE' THEN ? ELSE triaged_at END,"
            " archived_at=CASE WHEN ? = 'ARCHIVED' THEN ? ELSE archived_at END,"
            " row_revision=?, updated_at=? WHERE job_id=? AND profile_id=?",
            (disposition, dismissed_reason, snoozed_until, disposition, now,
             disposition, now, next_rev, now, job_id, profile_id),
        )
    return disposition, next_rev


class StaleRowRevision(Exception):
    pass


def inbox_eligible(
    *,
    score: float | None,
    min_score_inbox: float,
    eligibility: str | None,
    disposition: str | None,
    snoozed_until: str | None,
    now: str,
) -> bool:
    """The canonical predicate (module 01 section 42)."""
    if disposition in ("DISMISSED", "ARCHIVED"):
        return False
    if disposition == "SNOOZED" and snoozed_until and snoozed_until > now:
        return False
    if eligibility == "INELIGIBLE":
        return False
    if score is None or score < min_score_inbox:
        return False
    return True


def emit_inbox_event(
    db: Database,
    *,
    job_id: str,
    profile_id: str,
    event_kind: str,
    dedupe_key: str,
    trigger_content_revision: int | None = None,
    trigger_listing_state: str | None = None,
    trigger_history_id: str | None = None,
    detail: dict | None = None,
    now: str | None = None,
) -> str | None:
    """Emit one durable Inbox event; duplicate triggers surface zero extra events."""
    now = now or utc_now_s()
    if event_kind not in TRIGGER_KINDS:
        raise ValueError(f"invalid event kind {event_kind}")
    event_id = "iev-" + secrets.token_hex(10)
    try:
        with immediate_transaction(db.conn) as tx:
            tx.execute(
                "INSERT INTO job_profile_inbox_events(id, job_id, profile_id, event_kind,"
                " trigger_history_id, trigger_content_revision, trigger_listing_state, created_at,"
                " surfaced_at, dedupe_key, detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, job_id, profile_id, event_kind, trigger_history_id,
                 trigger_content_revision, trigger_listing_state, now, now, dedupe_key,
                 json.dumps(detail or {}, sort_keys=True)),
            )
    except Exception as exc:
        if "idx_inbox_events_dedupe" in str(exc) or "UNIQUE" in str(exc).upper():
            return None  # same trigger occurrence already surfaced
        raise
    with immediate_transaction(db.conn) as tx:
        cur = tx.execute(
            "UPDATE job_profile_state SET first_inbox_at=COALESCE(first_inbox_at, ?),"
            " last_inbox_at=?, updated_at=? WHERE job_id=? AND profile_id=?",
            (now, now, now, job_id, profile_id),
        )
        if cur.rowcount == 0:
            tx.execute(
                "INSERT INTO job_profile_state(job_id, profile_id, disposition, first_inbox_at,"
                " last_inbox_at, created_at, updated_at) VALUES (?,?, 'NONE', ?, ?, ?, ?)",
                (job_id, profile_id, now, now, now, now),
            )
    return event_id


def evaluate_inbox_triggers(
    db: Database,
    *,
    job_id: str,
    profile_id: str,
    score: float | None,
    eligibility: str | None,
    min_score_inbox: float,
    content_revision: int,
    profile_revision_id: str | None,
    listing_status: str | None = None,
    now: str | None = None,
) -> str | None:
    """Evaluate all trigger kinds for one (job, profile) under PROD-02 rules.

    Returns the event id emitted (if any). Refresh/restart/duplicate delivery
    create zero additional events.
    """
    now = now or utc_now_s()
    ensure_profile_state(db, job_id, profile_id, now=now)
    state = get_profile_state(db, job_id, profile_id)
    disposition = state["disposition"] if state else "NONE"
    snoozed_until = state["snoozed_until"] if state else None
    unexpired_snooze = disposition == "SNOOZED" and snoozed_until and snoozed_until > now

    # Snooze expiry occurrence: stable occurrence identity per snooze window.
    if disposition == "SNOOZED" and snoozed_until and snoozed_until <= now:
        if _passes_filters(score, min_score_inbox, eligibility):
            event = emit_inbox_event(
                db,
                job_id=job_id,
                profile_id=profile_id,
                event_kind="SNOOZE_EXPIRED",
                dedupe_key=f"snooze-expiry:{job_id}:{profile_id}:{snoozed_until}",
                trigger_content_revision=content_revision,
                trigger_listing_state=listing_status,
                detail={"snoozed_until": snoozed_until},
                now=now,
            )
            if event:
                return event

    if disposition in ("DISMISSED", "ARCHIVED"):
        return None  # sticky until explicit user change

    if unexpired_snooze:
        return None  # suppressed/coalesced under policy

    # First eligible appearance for this profile (may fire for an old job
    # after a deliberate profile revision).
    if _passes_filters(score, min_score_inbox, eligibility):
        first_key = f"first-eligible:{job_id}:{profile_id}"
        already = db.query_one(
            "SELECT id FROM job_profile_inbox_events WHERE job_id=? AND profile_id=? AND dedupe_key=?",
            (job_id, profile_id, first_key),
        )
        if already is None:
            return emit_inbox_event(
                db,
                job_id=job_id,
                profile_id=profile_id,
                event_kind="NEW_ELIGIBLE_APPEARANCE",
                dedupe_key=first_key,
                trigger_content_revision=content_revision,
                trigger_listing_state=listing_status,
                now=now,
            )
        # Not the first appearance: meaningful changes get their own identity.
        # A change fires only when the content revision differs from the last
        # surfaced revision; exactly-once per revision via the dedupe key, so
        # a refresh or restart re-evaluating the same revision emits nothing.
        last_event = db.query_one(
            "SELECT trigger_content_revision FROM job_profile_inbox_events"
            " WHERE job_id=? AND profile_id=? ORDER BY rowid DESC LIMIT 1",
            (job_id, profile_id),
        )
        last_rev = last_event["trigger_content_revision"] if last_event else None
        change_key = f"change:{job_id}:{profile_id}:rev{content_revision}"
        prior_revs = db.query(
            "SELECT dedupe_key FROM job_profile_inbox_events WHERE job_id=? AND profile_id=?"
            " AND dedupe_key LIKE 'change:%'",
            (job_id, profile_id),
        )
        seen_revs = {r["dedupe_key"] for r in prior_revs}
        if (
            last_rev != content_revision
            and change_key not in seen_revs
            and _passes_filters(score, min_score_inbox, eligibility)
        ):
            return emit_inbox_event(
                db,
                job_id=job_id,
                profile_id=profile_id,
                event_kind="MEANINGFUL_CHANGE",
                dedupe_key=change_key,
                trigger_content_revision=content_revision,
                trigger_listing_state=listing_status,
                now=now,
            )
        # Reopen trigger when a non-active canonical listing becomes active.
        if listing_status == "ACTIVE":
            reopen_key = f"reopen:{job_id}:{profile_id}"
            had_nonactive = db.query_one(
                "SELECT id FROM job_history WHERE job_id=? AND change_class='JOB_CLOSED' LIMIT 1",
                (job_id,),
            )
            if had_nonactive is not None:
                reopened = db.query_one(
                    "SELECT id FROM job_profile_inbox_events WHERE job_id=? AND profile_id=? AND dedupe_key=?",
                    (job_id, profile_id, reopen_key),
                )
                if reopened is None:
                    return emit_inbox_event(
                        db,
                        job_id=job_id,
                        profile_id=profile_id,
                        event_kind="REOPENED",
                        dedupe_key=reopen_key,
                        trigger_content_revision=content_revision,
                        trigger_listing_state=listing_status,
                        now=now,
                    )
    return None



def _passes_filters(score: float | None, min_score: float, eligibility: str | None) -> bool:
    return eligibility != "INELIGIBLE" and score is not None and score >= min_score


def inbox_events_for_profile(db: Database, profile_id: str, *, limit: int = 100, unacknowledged_only: bool = False):
    sql = (
        "SELECT e.*, j.title, j.company_id FROM job_profile_inbox_events e"
        " JOIN jobs j ON j.id = e.job_id WHERE e.profile_id=?"
    )
    params: list = [profile_id]
    if unacknowledged_only:
        sql += " AND e.acknowledged_at IS NULL"
    sql += " ORDER BY e.created_at DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


def inbox_queue(db: Database, profile_id: str, *, limit: int = 100, now: str | None = None,
                min_score: float = 0.0):
    """Current Inbox queue: eligible jobs with their score/eligibility/disposition."""
    now = now or utc_now_s()
    return db.query(
        "SELECT j.id, j.title, j.listing_status, j.last_changed_at, j.content_revision,"
        " s.disposition, s.snoozed_until, s.row_revision, sc.score, sc.breakdown_json,"
        " el.verdict AS eligibility, c.name AS company_name"
        " FROM jobs j"
        " JOIN job_profile_state s ON s.job_id = j.id AND s.profile_id = ?"
        " LEFT JOIN job_scores sc ON sc.job_id = j.id AND sc.profile_id = ?"
        " LEFT JOIN job_eligibility el ON el.job_id = j.id AND el.profile_id = ?"
        " LEFT JOIN companies c ON c.id = j.company_id"
        " WHERE j.listing_status = 'ACTIVE'"
        " AND s.disposition NOT IN ('DISMISSED','ARCHIVED')"
        " AND (s.disposition != 'SNOOZED' OR s.snoozed_until IS NULL OR s.snoozed_until <= ?)"
        " AND COALESCE(el.verdict, 'UNCLEAR') != 'INELIGIBLE'"
        " AND sc.score IS NOT NULL AND sc.score >= ?"
        " ORDER BY sc.score DESC LIMIT ?",
        (profile_id, profile_id, profile_id, now, min_score, limit),
    )
