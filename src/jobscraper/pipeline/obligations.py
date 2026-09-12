"""Downstream obligation draining (03 RUN-08, §18, RUN-21; 01 §36/§41).

Every accepted immutable observation atomically created host-native
downstream obligations (RECONCILE / ELIGIBILITY / SCORE — enqueued inside
the ingest fence). This module drains them under the ownership fence:

* RECONCILE derives the canonical listing status from current
  source-presence evidence (RUN-14: an aggregator disappearance cannot
  close a job an employer still reports active);
* ELIGIBILITY / SCORE evaluate the canonical job against every profile
  (single-user product; profiles are few) and upsert evaluation rows that
  record the content/profile/evaluator versions they evaluated.

Obligations survive cancellation (§18) and crashes (the S1.3 claim/retry
machinery): they are ordinary durable requests that never initiate
source I/O.
"""

from __future__ import annotations

import json
import sqlite3

from jobscraper.inbox.events import maybe_emit_inbox_event
from jobscraper.pipeline.evaluation import (
    capture_evaluation_snapshot,
    emit_inbox_if_current_pair,
    evaluate_snapshot,
    persist_eligibility_result,
    persist_score_result,
)
from jobscraper.profiles.core import list_profiles
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.fence import fenced_commit

OBLIGATION_TYPES = frozenset({"RECONCILE", "ELIGIBILITY", "SCORE"})

def reconcile_job(conn: sqlite3.Connection, job_id: str, *, now: str) -> str:
    """Derive ``jobs.listing_status`` under RUN-14/RUN-14A.

    Source-presence state remains fully inspectable.  The canonical projection
    is conservative and source-quality aware: a weak aggregator disappearance
    cannot close a job that current trusted employer/ATS evidence still says is
    active, while temporally newer explicit trusted closure may defeat older
    trusted active evidence.
    """

    from jobscraper.ids import new_id
    from jobscraper.pipeline.availability import resolve_canonical_availability

    prior = conn.execute(
        "SELECT listing_status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    prior_status = prior["listing_status"] if prior else "UNKNOWN"

    resolution = resolve_canonical_availability(conn, job_id)
    derived = resolution.status
    # The physical jobs CHECK has no UNKNOWN.  A canonical job is expected to
    # have at least one presence; if a damaged/legacy row violates that
    # invariant, leave durable state unchanged and surface UNKNOWN to caller.
    if derived == "UNKNOWN":
        return "UNKNOWN"

    if prior is None:
        return derived

    if derived == prior_status:
        return derived

    conn.execute(
        """
        UPDATE jobs
           SET listing_status = ?, last_changed_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (derived, now, now, job_id),
    )

    detail = json.dumps(
        {
            "availability_presence_id": resolution.presence_id,
            "availability_source_id": resolution.source_id,
            "availability_evidence_kind": resolution.evidence_kind,
            "conflict": resolution.conflict,
        },
        sort_keys=True,
    )

    if derived == "CLOSED" and prior_status != "CLOSED":
        history_id = new_id("jh")
        conn.execute(
            """
            INSERT INTO job_history (
                id, job_id, at, change_class, detail_json, evidence_ref)
            VALUES (?, ?, ?, 'JOB_CLOSED', ?, ?)
            """,
            (history_id, job_id, now, detail, resolution.evidence_ref),
        )
        from jobscraper.applications.core import record_listing_closed_if_applicable

        record_listing_closed_if_applicable(conn, job_id, now=now, commit=False)

    if derived == "ACTIVE" and prior_status in ("CLOSED", "EXPIRED", "WITHDRAWN"):
        history_id = new_id("jh")
        conn.execute(
            """
            INSERT INTO job_history (
                id, job_id, at, change_class, detail_json, evidence_ref)
            VALUES (?, ?, ?, 'JOB_REOPENED', ?, ?)
            """,
            (history_id, job_id, now, detail, resolution.evidence_ref),
        )
        for profile_row in list_profiles(conn):
            maybe_emit_inbox_event(
                conn,
                job_id=job_id,
                profile_id=profile_row["id"],
                event_kind="REOPENED",
                trigger_history_id=history_id,
                now=now,
                commit=False,
            )
    return derived

def _evaluate_for_profiles(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    request_type: str,
    now: str,
) -> None:
    """Materialize one exact current eligibility/score pair.

    ELIGIBILITY and SCORE remain separate durable obligations for compatibility
    and crash accounting, but either may be replayed in either order.  Each
    therefore attempts the same paired deterministic materialization; the first
    current delivery writes, and the second becomes an exact-input no-op.  This
    also prevents a profile/content revision advancing between the two requests
    from leaving a mixed-generation current pair.
    """

    if request_type not in ("ELIGIBILITY", "SCORE"):
        raise ValueError(f"unsupported evaluation obligation: {request_type!r}")
    for profile_row in list_profiles(conn):
        snapshot = capture_evaluation_snapshot(conn, job_id, profile_row["id"])
        if snapshot is None:
            continue
        eligibility, score = evaluate_snapshot(snapshot)
        persist_eligibility_result(conn, snapshot, eligibility, now=now)
        persist_score_result(conn, snapshot, score, now=now)
        emit_inbox_if_current_pair(conn, snapshot, now=now)


def drain_one_obligation(conn: sqlite3.Connection, *, now: str | None = None, worker_id: str = "service") -> bool:
    """Claim and execute one host-native obligation under the fence."""
    claim = claim_next_request(conn, worker_id, now=now, types=OBLIGATION_TYPES)
    if claim is None:
        return False

    def mutate(cursor: sqlite3.Connection) -> None:
        from jobscraper.runtime.clock import db_utc_now

        ts = now or db_utc_now(cursor)
        payload = claim.payload or {}
        job_id = payload.get("job_id")
        if job_id:
            if claim.request_type == "RECONCILE":
                reconcile_job(cursor, job_id, now=ts)
            elif claim.request_type in ("ELIGIBILITY", "SCORE"):
                _evaluate_for_profiles(
                    cursor, job_id, request_type=claim.request_type, now=ts
                )

    with fenced_commit(conn, claim.request_id, claim.attempt_id, now=now, mutate=mutate):
        pass
    return True


def drain_all_obligations(
    conn: sqlite3.Connection, *, now: str | None = None, worker_id: str = "service", limit: int = 500
) -> int:
    drained = 0
    while drained < limit and drain_one_obligation(conn, now=now, worker_id=worker_id):
        drained += 1
    return drained


__all__ = [
    "OBLIGATION_TYPES",
    "drain_all_obligations",
    "drain_one_obligation",
    "reconcile_job",
]
