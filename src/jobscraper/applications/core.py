"""Application records and events (01 §43)."""

from __future__ import annotations

import json
import sqlite3

from jobscraper.ids import new_id

STATUSES = frozenset(
    {
        "PREPARING", "APPLIED", "SCREENING", "INTERVIEWING", "OFFER",
        "ACCEPTED", "REJECTED", "WITHDRAWN", "GHOSTED", "CLOSED",
    }
)
TERMINAL_STATUSES = frozenset({"ACCEPTED", "REJECTED", "WITHDRAWN", "GHOSTED", "CLOSED"})
ACTIVE_STATUSES = STATUSES - TERMINAL_STATUSES


class ApplicationStatusError(ValueError):
    """Invalid status or illegal transition."""


def create_application(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    profile_id: str,
    now: str,
    resume_doc_id: str | None = None,
    cover_letter_doc_id: str | None = None,
    notes_md: str | None = None,
) -> sqlite3.Row:
    app_id = new_id("app")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO applications (
                id, job_id, profile_id, status, resume_doc_id,
                cover_letter_doc_id, notes_md, created_at, updated_at)
            VALUES (?, ?, ?, 'PREPARING', ?, ?, ?, ?, ?)
            """,
            (app_id, job_id, profile_id, resume_doc_id, cover_letter_doc_id,
             notes_md, now, now),
        )
        conn.execute(
            "INSERT INTO application_events (id, application_id, at, kind, detail_json)"
            " VALUES (?, ?, ?, 'CREATED', '{}')",
            (new_id("appev"), app_id, now),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get_application(conn, app_id)


def get_application(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM applications WHERE id = ?", (app_id,)
    ).fetchone()


def update_application(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    now: str,
    status: str | None = None,
    applied_at: str | None = None,
    applied_via_url: str | None = None,
    resume_doc_id: str | None = None,
    cover_letter_doc_id: str | None = None,
    salary_asked: str | None = None,
    notes_md: str | None = None,
    next_action_at: str | None = None,
    next_action_text: str | None = None,
    outcome: str | None = None,
    outcome_reason: str | None = None,
) -> sqlite3.Row:
    current = get_application(conn, app_id)
    if current is None:
        raise ApplicationStatusError(f"unknown application {app_id!r}")
    if status is not None:
        if status not in STATUSES:
            raise ApplicationStatusError(f"unknown application status: {status!r}")
        if current["status"] in TERMINAL_STATUSES and status != current["status"]:
            raise ApplicationStatusError(
                f"terminal application {app_id} cannot move from"
                f" {current['status']} to {status}"
            )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE applications SET
                status = COALESCE(?, status),
                applied_at = COALESCE(?, applied_at),
                applied_via_url = COALESCE(?, applied_via_url),
                resume_doc_id = COALESCE(?, resume_doc_id),
                cover_letter_doc_id = COALESCE(?, cover_letter_doc_id),
                salary_asked = COALESCE(?, salary_asked),
                notes_md = COALESCE(?, notes_md),
                next_action_at = ?,
                next_action_text = ?,
                outcome = COALESCE(?, outcome),
                outcome_reason = COALESCE(?, outcome_reason),
                closed_at = CASE WHEN ? IN ('ACCEPTED','REJECTED','WITHDRAWN','GHOSTED','CLOSED')
                                 THEN COALESCE(closed_at, ?) ELSE closed_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (status, applied_at, applied_via_url, resume_doc_id, cover_letter_doc_id,
             salary_asked, notes_md, next_action_at, next_action_text, outcome,
             outcome_reason, status, now, now, app_id),
        )
        if status is not None and status != current["status"]:
            conn.execute(
                "INSERT INTO application_events (id, application_id, at, kind, detail_json)"
                " VALUES (?, ?, ?, 'STATUS_CHANGED', ?)",
                (new_id("appev"), app_id, now,
                 json.dumps({"from": current["status"], "to": status})),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get_application(conn, app_id)


def list_applications(conn: sqlite3.Connection, *, profile_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM applications WHERE profile_id = ? ORDER BY created_at",
        (profile_id,),
    ).fetchall()


def record_listing_closed_if_applicable(
    conn: sqlite3.Connection, job_id: str, *, now: str, commit: bool = True
) -> int:
    """If the canonical listing closed while applications are active,
    record one application event per active application (§43)."""
    inserted = 0
    rows = conn.execute(
        "SELECT a.id FROM applications a WHERE a.job_id = ? AND a.status IN"
        " ('PREPARING','APPLIED','SCREENING','INTERVIEWING','OFFER')",
        (job_id,),
    ).fetchall()
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM application_events WHERE application_id = ?"
            " AND kind = 'APPLICATION_LISTING_CLOSED'",
            (row["id"],),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO application_events (id, application_id, at, kind, detail_json)"
            " VALUES (?, ?, ?, 'APPLICATION_LISTING_CLOSED', '{}')",
            (new_id("appev"), row["id"], now),
        )
        inserted += 1
    if commit and inserted:
        conn.commit()
    return inserted


__all__ = [
    "ACTIVE_STATUSES",
    "ApplicationStatusError",
    "STATUSES",
    "TERMINAL_STATUSES",
    "create_application",
    "get_application",
    "list_applications",
    "record_listing_closed_if_applicable",
    "update_application",
]
