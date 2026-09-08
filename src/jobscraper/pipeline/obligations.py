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

from jobscraper.pipeline.eligibility import (
    EVALUATOR_VERSION,
    evaluate_eligibility,
)
from jobscraper.pipeline.scoring import SCORER_VERSION, score_job
from jobscraper.profiles.core import current_snapshot, list_profiles
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.fence import fenced_commit

OBLIGATION_TYPES = frozenset({"RECONCILE", "ELIGIBILITY", "SCORE"})

_LISTING_PRECEDENCE = ("CLOSED", "EXPIRED", "WITHDRAWN", "UNCERTAIN", "ACTIVE", "UNKNOWN")


def reconcile_job(conn: sqlite3.Connection, job_id: str, *, now: str) -> str:
    """Derive jobs.listing_status from current presence evidence (RUN-14)."""
    states = [
        row["presence_state"]
        for row in conn.execute(
            "SELECT presence_state FROM job_sources WHERE job_id = ?", (job_id,)
        )
    ]
    if not states:
        return "UNKNOWN"
    if "ACTIVE" in states:
        derived = "ACTIVE"
    else:
        derived = next(
            (state for state in _LISTING_PRECEDENCE if state in states), "UNKNOWN"
        )
    conn.execute(
        "UPDATE jobs SET listing_status = ?, updated_at = ? WHERE id = ?",
        (derived, now, job_id),
    )
    return derived


def _evaluate_for_profiles(conn: sqlite3.Connection, job_id: str, *, now: str) -> None:
    job = conn.execute(
        """
        SELECT j.*, COALESCE(
            (SELECT GROUP_CONCAT(raw_text, ' | ') FROM job_locations WHERE job_id = j.id),
            '') AS locations_text
        FROM jobs j WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if job is None:
        return
    locations = [part.strip() for part in (job["locations_text"] or "").split(" | ") if part]
    presence_rev = conn.execute(
        "SELECT MAX(content_revision) FROM job_sources WHERE job_id = ?", (job_id,)
    ).fetchone()[0] or 1
    for profile_row in list_profiles(conn):
        profile = current_snapshot(conn, profile_row["id"])
        if profile is None:
            continue
        verdict = evaluate_eligibility(
            locations=locations,
            remote_worldwide=bool(job["remote_worldwide"]),
            profile=profile,
        )
        conn.execute(
            """
            INSERT INTO job_eligibility (
                job_id, profile_id, profile_revision_id, job_content_revision,
                evaluator_version, verdict, confidence, reason_codes_json,
                evidence_json, rule_version, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id, profile_id) DO UPDATE SET
                profile_revision_id = excluded.profile_revision_id,
                job_content_revision = excluded.job_content_revision,
                evaluator_version = excluded.evaluator_version,
                verdict = excluded.verdict, confidence = excluded.confidence,
                reason_codes_json = excluded.reason_codes_json,
                evidence_json = excluded.evidence_json,
                rule_version = excluded.rule_version, evaluated_at = excluded.evaluated_at
            """,
            (
                job_id,
                profile_row["id"],
                profile_row["current_revision_id"],
                presence_rev,
                EVALUATOR_VERSION,
                verdict.verdict,
                verdict.confidence,
                json.dumps(verdict.reason_codes),
                json.dumps(verdict.evidence, default=str),
                EVALUATOR_VERSION,
                now,
            ),
        )
        job_data = {
            "title": job["title"],
            "normalized_title": job["normalized_title"],
            "salary_min": job["salary_min"],
            "salary_max": job["salary_max"],
            "salary_currency": job["salary_currency"],
            "salary_period": job["salary_period"],
            "eligibility_verdict": verdict.verdict,
        }
        result = score_job(job_data=job_data, profile=profile)
        conn.execute(
            """
            INSERT INTO job_scores (
                job_id, profile_id, profile_revision_id, job_content_revision,
                scorer_version, score, breakdown_json, rule_version, scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id, profile_id) DO UPDATE SET
                profile_revision_id = excluded.profile_revision_id,
                job_content_revision = excluded.job_content_revision,
                scorer_version = excluded.scorer_version,
                score = excluded.score, breakdown_json = excluded.breakdown_json,
                rule_version = excluded.rule_version, scored_at = excluded.scored_at
            """,
            (
                job_id,
                profile_row["id"],
                profile_row["current_revision_id"],
                presence_rev,
                SCORER_VERSION,
                result.score,
                json.dumps(result.breakdown),
                result.rule_version,
                now,
            ),
        )


def drain_one_obligation(conn: sqlite3.Connection, *, now: str, worker_id: str = "service") -> bool:
    """Claim and execute one host-native obligation under the fence."""
    claim = claim_next_request(conn, worker_id, now=now, types=OBLIGATION_TYPES)
    if claim is None:
        return False

    def mutate(cursor: sqlite3.Connection) -> None:
        payload = claim.payload or {}
        job_id = payload.get("job_id")
        if job_id:
            if claim.request_type == "RECONCILE":
                reconcile_job(cursor, job_id, now=now)
            elif claim.request_type in ("ELIGIBILITY", "SCORE"):
                _evaluate_for_profiles(cursor, job_id, now=now)

    with fenced_commit(conn, claim.request_id, claim.attempt_id, now=now, mutate=mutate):
        pass
    return True


def drain_all_obligations(
    conn: sqlite3.Connection, *, now: str, worker_id: str = "service", limit: int = 500
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
