"""Enumeration coverage and absence authority (03 §40, RUN-13, RUN-14A).

Absence evidence may be created ONLY when:

1. ``completion_state = COMPLETE``;
2. ``coverage_authority`` is AUTHORITATIVE_FULL_SOURCE or
   AUTHORITATIVE_DECLARED_SCOPE;
3. ``absence_inference_allowed`` is true;
4. the source-presence record belongs to the same declared scope;
5. the run was not invalidated by challenge, auth failure, policy denial
   or cancellation.

Each ``enumeration_coverage.id`` is applied at most once (idempotent);
overlapping generations are ordered by their own finalized_at under
RUN-21 so an older generation cannot regress newer presence.

The finalization barrier refuses COMPLETE while any contributing request
is still open, or when terminal enumeration is not proven.
"""

from __future__ import annotations

import sqlite3

from jobscraper.ids import new_id

_ABSENCE_AUTHORITIES = frozenset(
    {"AUTHORITATIVE_FULL_SOURCE", "AUTHORITATIVE_DECLARED_SCOPE"}
)
_TERMINAL_REQUEST_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class CoverageFinalizationError(Exception):
    """The coverage generation cannot be finalized as requested."""


def open_coverage(
    conn: sqlite3.Connection,
    *,
    run_source_plan_id: str,
    source_id: str,
    binding_id: str,
    scope_key: str,
    generation_key: str,
    coverage_authority: str,
    now: str,
) -> str:
    presence_id = new_id("cov")
    absence_allowed = coverage_authority in _ABSENCE_AUTHORITIES
    conn.execute(
        """
        INSERT INTO enumeration_coverage (
            id, run_source_plan_id, source_plan_group_id, source_id, binding_id,
            scope_key, generation_key, coverage_authority,
            absence_inference_allowed, started_at, created_at)
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            presence_id,
            run_source_plan_id,
            source_id,
            binding_id,
            scope_key,
            generation_key,
            coverage_authority,
            1 if absence_allowed else 0,
            now,
            now,
        ),
    )
    conn.commit()
    return presence_id


def record_seen_identity(
    conn: sqlite3.Connection,
    coverage_id: str,
    stable_source_identity: str,
    generation: int = 1,
    evidence_ref: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO coverage_seen_identity (
            coverage_id, stable_source_identity, source_identity_generation,
            observation_or_listing_evidence_ref)
        VALUES (?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (coverage_id, stable_source_identity, generation, evidence_ref),
    )
    conn.commit()


def finalize_coverage(
    conn: sqlite3.Connection,
    coverage_id: str,
    *,
    completion_state: str,
    stop_reason: str,
    terminal_enumeration_proven: bool,
    now: str,
    pages_completed: int | None = None,
    items_observed: int | None = None,
) -> None:
    """Finalize one coverage generation and (when absence-authoritative and
    COMPLETE) apply absence evidence to the scope's presences exactly once."""
    row = conn.execute(
        "SELECT * FROM enumeration_coverage WHERE id = ?", (coverage_id,)
    ).fetchone()
    if row is None:
        raise CoverageFinalizationError(f"unknown coverage {coverage_id!r}")
    if row["finalized_at"] is not None:
        raise CoverageFinalizationError(
            f"coverage {coverage_id!r} is already finalized (generations are immutable)"
        )
    if completion_state not in (
        "COMPLETE", "PARTIAL", "CANCELLED", "FAILED", "BUDGET_EXHAUSTED", "UNKNOWN"
    ):
        raise CoverageFinalizationError(f"invalid completion state {completion_state!r}")

    if completion_state == "COMPLETE":
        if not terminal_enumeration_proven:
            raise CoverageFinalizationError(
                "COMPLETE requires proven terminal enumeration (cursor reached end)"
            )
        open_requests = conn.execute(
            """
            SELECT r.id, r.status FROM coverage_contributing_request c
            JOIN scrape_requests r ON r.id = c.request_id
            WHERE c.coverage_id = ? AND r.status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
            """,
            (coverage_id,),
        ).fetchall()
        if open_requests:
            raise CoverageFinalizationError(
                "COMPLETE refuses to finalize while contributing requests are open: "
                + ", ".join(f"{r['id']}={r['status']}" for r in open_requests[:5])
            )

    contributing = conn.execute(
        "SELECT COUNT(*) FROM coverage_contributing_request WHERE coverage_id = ?",
        (coverage_id,),
    ).fetchone()[0]
    seen_count = conn.execute(
        "SELECT COUNT(*) FROM coverage_seen_identity WHERE coverage_id = ?",
        (coverage_id,),
    ).fetchone()[0]

    conn.execute(
        """
        UPDATE enumeration_coverage SET
            completion_state = ?, stop_reason = ?,
            pages_completed = COALESCE(?, pages_completed),
            items_observed = COALESCE(?, items_observed),
            cursor_terminal = ?, terminal_enumeration_proven = ?,
            contributing_request_count = ?, finalized_at = ?, applied_at = ?
        WHERE id = ?
        """,
        (
            completion_state,
            stop_reason,
            pages_completed,
            items_observed,
            1 if terminal_enumeration_proven else 0,
            1 if terminal_enumeration_proven else 0,
            contributing,
            now,
            now,
            coverage_id,
        ),
    )

    if (
        completion_state == "COMPLETE"
        and row["absence_inference_allowed"]
        and row["coverage_authority"] in _ABSENCE_AUTHORITIES
    ):
        _apply_absence(conn, row, coverage_id, now)

    conn.commit()


def _apply_absence(
    conn: sqlite3.Connection, row: sqlite3.Row, coverage_id: str, now: str
) -> None:
    """One complete absence-authoritative generation: unseen presences in
    scope go UNCERTAIN; previously-UNCERTAIN presences still unseen go
    EXPIRED (RUN-13 policy). Seen presences stay untouched."""
    seen = {
        (r["stable_source_identity"], r["source_identity_generation"])
        for r in conn.execute(
            "SELECT stable_source_identity, source_identity_generation"
            " FROM coverage_seen_identity WHERE coverage_id = ?",
            (coverage_id,),
        )
    }
    scope_presences = conn.execute(
        """
        SELECT id, source_job_id, source_identity_generation, presence_state,
               last_absence_coverage_id
        FROM job_sources
        WHERE source_id = ? AND source_job_id IS NOT NULL
        """,
        (row["source_id"],),
    ).fetchall()
    for presence in scope_presences:
        identity = (presence["source_job_id"], presence["source_identity_generation"])
        if identity in seen:
            continue
        state = presence["presence_state"]
        if state == "ACTIVE":
            new_state = "UNCERTAIN"
        elif state == "UNCERTAIN" and presence["last_absence_coverage_id"] != coverage_id:
            new_state = "EXPIRED"
        else:
            continue  # CLOSED/WITHDRAWN/EXPIRED stay; same-generation replay is a no-op
        conn.execute(
            "UPDATE job_sources SET presence_state = ?, last_absence_coverage_id = ?,"
            " updated_at = ? WHERE id = ?",
            (new_state, coverage_id, now, presence["id"]),
        )


__all__ = [
    "CoverageFinalizationError",
    "finalize_coverage",
    "open_coverage",
    "record_seen_identity",
]
