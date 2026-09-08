"""Durable inbox events (01 PROD-02, §42).

The canonical inbox predicate (§42):

    eligible_for_inbox =
      trigger_is_due
      AND score >= profile.min_score_inbox
      AND eligibility != INELIGIBLE
      AND disposition NOT IN {DISMISSED, ARCHIVED}
      AND (disposition != SNOOZED OR snoozed_until <= now)

The PROD-02 transition matrix is enforced on top:

* NONE/SHORTLISTED — a due trigger may emit one deduplicated event;
* SNOOZED and unexpired — ordinary triggers are suppressed/coalesced;
  snooze expiry emits at most one occurrence event;
* DISMISSED/ARCHIVED — suppressed until explicit user reversal.

``dedupe_key`` deterministically identifies the profile-relative trigger
occurrence (content revision / reopen history / snooze occurrence /
profile revision), so duplicate delivery creates zero extra rows.
"""

from __future__ import annotations

import sqlite3

from jobscraper.ids import new_id
from jobscraper.inbox.state import get_state
from jobscraper.profiles.core import current_snapshot

EVENT_KINDS = frozenset(
    {
        "NEW_ELIGIBLE_APPEARANCE",
        "MEANINGFUL_CHANGE",
        "REOPENED",
        "SNOOZE_EXPIRED",
        "PROFILE_REVISION_ELIGIBLE",
    }
)


def _dedupe_key(
    event_kind: str,
    *,
    trigger_content_revision: int | None,
    trigger_history_id: str | None,
    snooze_occurrence: int | None,
    profile_revision_id: str | None,
) -> str:
    if event_kind == "NEW_ELIGIBLE_APPEARANCE":
        return "nea"
    if event_kind == "MEANINGFUL_CHANGE":
        return f"mc:{trigger_content_revision}"
    if event_kind == "REOPENED":
        return f"re:{trigger_history_id}"
    if event_kind == "SNOOZE_EXPIRED":
        return f"se:{snooze_occurrence}"
    if event_kind == "PROFILE_REVISION_ELIGIBLE":
        return f"pre:{profile_revision_id}"
    raise ValueError(f"unknown event kind: {event_kind!r}")


def maybe_emit_inbox_event(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    profile_id: str,
    event_kind: str,
    now: str,
    trigger_content_revision: int | None = None,
    trigger_history_id: str | None = None,
    snooze_occurrence: int | None = None,
    profile_revision_id: str | None = None,
    commit: bool = True,
) -> tuple[str | None, str]:
    """Apply the §42 predicate + PROD-02 matrix and emit at most one
    deduplicated event. Returns ``(event_id | None, outcome)``."""
    if event_kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {event_kind!r}")

    state = get_state(conn, job_id, profile_id)
    disposition = state["disposition"] if state else "NONE"
    snoozed_until = state["snoozed_until"] if state else None

    # PROD-02 matrix: sticky suppressions first.
    if disposition in ("DISMISSED", "ARCHIVED"):
        return None, "DISPOSITION_SUPPRESSED"
    if event_kind != "SNOOZE_EXPIRED" and disposition == "SNOOZED":
        if not snoozed_until or snoozed_until > now:
            return None, "SNOOZED_SUPPRESSED"
        # expired snooze: ordinary triggers may surface again

    if event_kind == "SNOOZE_EXPIRED" and disposition != "SNOOZED":
        return None, "NOT_SNOOZED"

    # §42 predicate: score floor + eligibility exclusion.
    score_row = conn.execute(
        "SELECT score FROM job_scores WHERE job_id = ? AND profile_id = ?",
        (job_id, profile_id),
    ).fetchone()
    eligibility_row = conn.execute(
        "SELECT verdict FROM job_eligibility WHERE job_id = ? AND profile_id = ?",
        (job_id, profile_id),
    ).fetchone()
    if eligibility_row and eligibility_row["verdict"] == "INELIGIBLE":
        return None, "INELIGIBLE_EXCLUDED"
    profile = current_snapshot(conn, profile_id) or {}
    min_score = float(profile.get("min_score_inbox") or 0)
    score = float(score_row["score"]) if score_row else 0.0
    if score < min_score:
        return None, "BELOW_SCORE_FLOOR"

    key = _dedupe_key(
        event_kind,
        trigger_content_revision=trigger_content_revision,
        trigger_history_id=trigger_history_id,
        snooze_occurrence=snooze_occurrence,
        profile_revision_id=profile_revision_id,
    )
    event_id = new_id("ibe")
    inserted = conn.execute(
        """
        INSERT INTO job_profile_inbox_events (
            id, job_id, profile_id, event_kind, trigger_history_id,
            trigger_content_revision, trigger_listing_state, created_at,
            surfaced_at, dedupe_key)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        ON CONFLICT (job_id, profile_id, dedupe_key) DO NOTHING
        """,
        (
            event_id,
            job_id,
            profile_id,
            event_kind,
            trigger_history_id,
            trigger_content_revision,
            now,
            now,
            key,
        ),
    )
    if inserted.rowcount == 0:
        return None, "DEDUPLICATED"

    # Current-state convenience fields (PROD-02: not a substitute for the
    # durable ledger, which is why the event row above is authoritative).
    if state is None:
        conn.execute(
            """
            INSERT INTO job_profile_state (
                job_id, profile_id, first_inbox_at, last_inbox_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, profile_id, now, now, now, now),
        )
    else:
        conn.execute(
            """
            UPDATE job_profile_state SET
                first_inbox_at = COALESCE(first_inbox_at, ?),
                last_inbox_at = ?,
                updated_at = ?
            WHERE job_id = ? AND profile_id = ?
            """,
            (now, now, now, job_id, profile_id),
        )
    if commit:
        conn.commit()
    return event_id, "EMITTED"


def expire_snoozes(conn: sqlite3.Connection, *, now: str) -> list[tuple[str, str]]:
    """Sweep expired snoozes and emit at most one SNOOZE_EXPIRED
    occurrence per (job, profile, snooze occurrence)."""
    expired_rows = conn.execute(
        """
        SELECT job_id, profile_id, row_revision, snoozed_until
        FROM job_profile_state
        WHERE disposition = 'SNOOZED' AND snoozed_until IS NOT NULL AND snoozed_until <= ?
        """,
        (now,),
    ).fetchall()
    emitted: list[tuple[str, str]] = []
    for row in expired_rows:
        event_id, outcome = maybe_emit_inbox_event(
            conn,
            job_id=row["job_id"],
            profile_id=row["profile_id"],
            event_kind="SNOOZE_EXPIRED",
            snooze_occurrence=int(row["row_revision"]),
            now=now,
        )
        if event_id is not None:
            # only newly emitted occurrences are reported; a deduplicated
            # sweep touch is an idempotent no-op
            emitted.append((row["job_id"], row["profile_id"]))
    return emitted


def inbox_feed(conn: sqlite3.Connection, profile_id: str, *, limit: int = 100):
    """Current inbox items for a profile: one row per job (its latest
    surfaced event), joined with the current score/eligibility/disposition,
    newest first."""
    return conn.execute(
        """
        SELECT e.id, e.job_id, e.event_kind, e.created_at, e.surfaced_at,
               j.title, j.listing_status,
               s.score, el.verdict,
               st.disposition, st.snoozed_until
        FROM (
            SELECT id, job_id, event_kind, created_at, surfaced_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY job_id ORDER BY created_at DESC, id DESC) AS rn
            FROM job_profile_inbox_events
            WHERE profile_id = ?
        ) e
        JOIN jobs j ON j.id = e.job_id
        LEFT JOIN job_scores s ON s.job_id = e.job_id AND s.profile_id = ?
        LEFT JOIN job_eligibility el ON el.job_id = e.job_id AND el.profile_id = ?
        LEFT JOIN job_profile_state st ON st.job_id = e.job_id AND st.profile_id = ?
        WHERE e.rn = 1
          AND COALESCE(st.disposition, 'NONE') NOT IN ('DISMISSED', 'ARCHIVED')
          AND (COALESCE(st.disposition, 'NONE') != 'SNOOZED'
               OR st.snoozed_until IS NULL OR st.snoozed_until <= ?
               OR e.event_kind = 'SNOOZE_EXPIRED')
        ORDER BY e.created_at DESC, e.id DESC
        LIMIT ?
        """,
        (profile_id, profile_id, profile_id, profile_id, now_utc(conn), int(limit)),
    ).fetchall()


def now_utc(conn: sqlite3.Connection) -> str:
    from jobscraper.runtime.clock import db_utc_now

    return db_utc_now(conn)


__all__ = [
    "EVENT_KINDS",
    "expire_snoozes",
    "inbox_feed",
    "maybe_emit_inbox_event",
]
