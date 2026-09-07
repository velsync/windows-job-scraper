"""Application lifecycle — user owned (module 01 section 7.3).

An application record is separate from the canonical job: a job can be
``listing_status=CLOSED`` while its application remains ``INTERVIEWING``.
No single status column combines listing, disposition and application state.
Every status change is recorded as a durable application event.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import utc_now_s

STATUSES = (
    "PREPARING", "APPLIED", "SCREENING", "INTERVIEWING", "OFFER",
    "ACCEPTED", "REJECTED", "WITHDRAWN", "GHOSTED", "CLOSED",
)

# Deterministic transition table. Terminal states accept only CLOSED
# (administrative closure); nothing leaves CLOSED.
TRANSITIONS: dict[str, frozenset[str]] = {
    "PREPARING": frozenset({"APPLIED", "WITHDRAWN", "CLOSED"}),
    "APPLIED": frozenset({"SCREENING", "INTERVIEWING", "OFFER", "REJECTED", "GHOSTED", "WITHDRAWN", "CLOSED"}),
    "SCREENING": frozenset({"INTERVIEWING", "OFFER", "REJECTED", "GHOSTED", "WITHDRAWN", "CLOSED"}),
    "INTERVIEWING": frozenset({"OFFER", "REJECTED", "GHOSTED", "WITHDRAWN", "CLOSED"}),
    "OFFER": frozenset({"ACCEPTED", "REJECTED", "WITHDRAWN", "CLOSED"}),
    "ACCEPTED": frozenset({"CLOSED"}),
    "REJECTED": frozenset({"CLOSED"}),
    "WITHDRAWN": frozenset({"CLOSED"}),
    "GHOSTED": frozenset({"CLOSED"}),
    "CLOSED": frozenset(),
}


class InvalidTransition(ValueError):
    pass


class StaleRowRevision(Exception):
    pass


def _validate_http_url(url: str | None) -> str | None:
    """Only http/https URLs may be stored as application links."""
    if url is None or url == "":
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"application URL must be absolute http(s): {url!r}")
    return url


def create_application(
    db: Database,
    *,
    job_id: str,
    profile_id: str | None = None,
    applied_via_url: str | None = None,
    notes_md: str | None = None,
    now: str | None = None,
) -> str:
    """Create an application record in PREPARING (user-owned, per job+profile)."""
    now = now or utc_now_s()
    application_id = "app-" + secrets.token_hex(10)
    url = _validate_http_url(applied_via_url)
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO applications(id, job_id, profile_id, status, applied_via_url,"
            " notes_md, row_revision, created_at, updated_at)"
            " VALUES (?,?,?, 'PREPARING', ?, ?, 1, ?, ?)",
            (application_id, job_id, profile_id, url, notes_md, now, now),
        )
        tx.execute(
            "INSERT INTO application_events(id, application_id, at, kind, detail_json)"
            " VALUES (?,?,?,'CREATED',?)",
            ("aev-" + secrets.token_hex(10), application_id, now,
             f'{{"status": "PREPARING"}}'),
        )
    return application_id


def get_application(db: Database, application_id: str):
    return db.query_one("SELECT * FROM applications WHERE id=?", (application_id,))


def set_application_status(
    db: Database,
    *,
    application_id: str,
    status: str,
    expected_row_revision: int | None = None,
    outcome: str | None = None,
    outcome_reason: str | None = None,
    now: str | None = None,
) -> tuple[str, int]:
    """Transition the application status with optimistic concurrency.

    Every accepted transition records a durable application event. ``applied_at``
    is stamped exactly once, when the record first reaches APPLIED.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid application status {status}")
    now = now or utc_now_s()
    with immediate_transaction(db.conn) as tx:
        row = tx.execute(
            "SELECT status, row_revision FROM applications WHERE id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise KeyError(application_id)
        current_status = row["status"]
        if status not in TRANSITIONS[current_status]:
            raise InvalidTransition(
                f"application {application_id}: {current_status} -> {status} is not a valid transition"
            )
        if expected_row_revision is not None and expected_row_revision != row["row_revision"]:
            raise StaleRowRevision(
                f"row revision mismatch: expected {expected_row_revision}, current {row['row_revision']}"
            )
        next_rev = row["row_revision"] + 1
        closed_at = now if status == "CLOSED" else None
        tx.execute(
            "UPDATE applications SET status=?, applied_at=COALESCE(applied_at, CASE WHEN ?='APPLIED'"
            " THEN ? ELSE NULL END), closed_at=COALESCE(closed_at, ?), outcome=COALESCE(?, outcome),"
            " outcome_reason=COALESCE(?, outcome_reason), row_revision=?, updated_at=? WHERE id=?",
            (status, status, now, closed_at, outcome, outcome_reason, next_rev, now, application_id),
        )
        tx.execute(
            "INSERT INTO application_events(id, application_id, at, kind, detail_json)"
            " VALUES (?,?,?,'STATUS_CHANGED',?)",
            ("aev-" + secrets.token_hex(10), application_id, now,
             f'{{"from": "{current_status}", "to": "{status}"}}'),
        )
    return status, next_rev


def update_application(
    db: Database,
    *,
    application_id: str,
    notes_md: str | None = None,
    next_action_at: str | None = None,
    next_action_text: str | None = None,
    salary_asked: str | None = None,
    expected_row_revision: int | None = None,
    now: str | None = None,
) -> int:
    """Update user fields with optimistic concurrency (never the status)."""
    now = now or utc_now_s()
    with immediate_transaction(db.conn) as tx:
        row = tx.execute(
            "SELECT row_revision FROM applications WHERE id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise KeyError(application_id)
        if expected_row_revision is not None and expected_row_revision != row["row_revision"]:
            raise StaleRowRevision(
                f"row revision mismatch: expected {expected_row_revision}, current {row['row_revision']}"
            )
        next_rev = row["row_revision"] + 1
        tx.execute(
            "UPDATE applications SET notes_md=COALESCE(?, notes_md),"
            " next_action_at=COALESCE(?, next_action_at), next_action_text=COALESCE(?, next_action_text),"
            " salary_asked=COALESCE(?, salary_asked), row_revision=?, updated_at=? WHERE id=?",
            (notes_md, next_action_at, next_action_text, salary_asked, next_rev, now, application_id),
        )
        tx.execute(
            "INSERT INTO application_events(id, application_id, at, kind, detail_json)"
            " VALUES (?,?,?,'UPDATED',?)",
            ("aev-" + secrets.token_hex(10), application_id, now, '{"fields": "user_edit"}'),
        )
    return next_rev


def applications_for_job(db: Database, job_id: str):
    return db.query(
        "SELECT * FROM applications WHERE job_id=? ORDER BY created_at", (job_id,)
    )


def application_events(db: Database, application_id: str):
    return db.query(
        "SELECT * FROM application_events WHERE application_id=? ORDER BY at, id", (application_id,)
    )
