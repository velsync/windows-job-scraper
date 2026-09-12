"""Evidence-ordered local eligibility/score materialization (Slice 3 S3.11).

RUN-08 keeps accepted host-native work durable across cancellation/restart.
RUN-21 additionally requires the mutable *current* eligibility and score rows
to identify the exact inputs they evaluated and to reject stale completion.

This module owns that second rule.  Evaluation work captures an immutable
coordinate containing the canonical job evaluation revision, immutable profile
revision/rules identity, and normalization/evaluator/scorer code pins.  A
result may update the current projection only while that complete coordinate
is still current.  Re-delivery of the same coordinate is an idempotent no-op.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from jobscraper.inbox.events import maybe_emit_inbox_event
from jobscraper.pipeline.eligibility import EVALUATOR_VERSION, EligibilityVerdict, evaluate_eligibility
from jobscraper.pipeline.normalize import NORMALIZATION_VERSION
from jobscraper.pipeline.scoring import RULES_VERSION, SCORER_VERSION, ScoreResult, score_job

STALE_INPUT = "STALE_INPUT"
CURRENT_NOOP = "CURRENT_NOOP"
WRITTEN = "WRITTEN"


@dataclass(frozen=True)
class EvaluationSnapshot:
    """Exact immutable coordinate and value inputs for one job/profile pair."""

    job_id: str
    profile_id: str
    job_content_revision: int
    profile_revision_id: str
    profile_content_hash: str
    rules_revision_id: str | None
    normalization_version: str
    evaluator_version: str
    scorer_version: str
    scoring_rule_version: str
    title: str
    normalized_title: str
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    remote_worldwide: bool
    locations: tuple[str, ...]
    profile_snapshot_json: str

    @property
    def profile(self) -> dict[str, Any]:
        value = json.loads(self.profile_snapshot_json)
        if not isinstance(value, dict):  # immutable profile revisions are objects
            raise ValueError("profile revision snapshot is not a JSON object")
        return value

    def scoring_job_data(self, *, eligibility_verdict: str) -> dict[str, Any]:
        return {
            "title": self.title,
            "normalized_title": self.normalized_title,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_currency": self.salary_currency,
            "salary_period": self.salary_period,
            "eligibility_verdict": eligibility_verdict,
        }


def capture_evaluation_snapshot(
    conn: sqlite3.Connection, job_id: str, profile_id: str
) -> EvaluationSnapshot | None:
    """Capture the exact current deterministic evaluation inputs.

    The caller normally holds the request output fence, so this capture and the
    eventual conditional write share one serialized SQLite write transaction.
    Keeping an explicit snapshot also makes stale/reverse-completion rejection
    independently testable and safe for future compute-outside-lock refactors.
    """

    row = conn.execute(
        """
        SELECT j.id AS job_id, j.evaluation_revision, j.title,
               j.normalized_title, j.salary_min, j.salary_max,
               j.salary_currency, j.salary_period, j.remote_worldwide,
               p.id AS profile_id, p.current_revision_id,
               pr.content_hash AS profile_content_hash,
               pr.rules_revision_id, pr.profile_snapshot_json
          FROM jobs j
          JOIN search_profiles p ON p.id = ?
          JOIN profile_revisions pr ON pr.id = p.current_revision_id
         WHERE j.id = ?
        """,
        (profile_id, job_id),
    ).fetchone()
    if row is None or not row["current_revision_id"]:
        return None

    locations = tuple(
        str(location["raw_text"]).strip()
        for location in conn.execute(
            """
            SELECT raw_text
              FROM job_locations
             WHERE job_id = ? AND raw_text IS NOT NULL AND TRIM(raw_text) <> ''
             ORDER BY id
            """,
            (job_id,),
        ).fetchall()
    )
    return EvaluationSnapshot(
        job_id=str(row["job_id"]),
        profile_id=str(row["profile_id"]),
        job_content_revision=int(row["evaluation_revision"]),
        profile_revision_id=str(row["current_revision_id"]),
        profile_content_hash=str(row["profile_content_hash"]),
        rules_revision_id=row["rules_revision_id"],
        normalization_version=NORMALIZATION_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        scorer_version=SCORER_VERSION,
        scoring_rule_version=RULES_VERSION,
        title=str(row["title"]),
        normalized_title=str(row["normalized_title"]),
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        salary_currency=row["salary_currency"],
        salary_period=row["salary_period"],
        remote_worldwide=bool(row["remote_worldwide"]),
        locations=locations,
        profile_snapshot_json=str(row["profile_snapshot_json"]),
    )


def snapshot_is_current(conn: sqlite3.Connection, snapshot: EvaluationSnapshot) -> bool:
    """Return whether every mutable/replaceable coordinate still matches."""

    row = conn.execute(
        """
        SELECT j.evaluation_revision, p.current_revision_id,
               pr.content_hash AS profile_content_hash, pr.rules_revision_id
          FROM jobs j
          JOIN search_profiles p ON p.id = ?
          JOIN profile_revisions pr ON pr.id = p.current_revision_id
         WHERE j.id = ?
        """,
        (snapshot.profile_id, snapshot.job_id),
    ).fetchone()
    if row is None:
        return False
    return (
        int(row["evaluation_revision"]) == snapshot.job_content_revision
        and row["current_revision_id"] == snapshot.profile_revision_id
        and row["profile_content_hash"] == snapshot.profile_content_hash
        and row["rules_revision_id"] == snapshot.rules_revision_id
        and snapshot.normalization_version == NORMALIZATION_VERSION
        and snapshot.evaluator_version == EVALUATOR_VERSION
        and snapshot.scorer_version == SCORER_VERSION
        and snapshot.scoring_rule_version == RULES_VERSION
    )


def evaluate_snapshot(snapshot: EvaluationSnapshot) -> tuple[EligibilityVerdict, ScoreResult]:
    """Deterministically compute eligibility and score from one frozen input."""

    profile = snapshot.profile
    eligibility = evaluate_eligibility(
        locations=snapshot.locations,
        remote_worldwide=snapshot.remote_worldwide,
        profile=profile,
    )
    score = score_job(
        job_data=snapshot.scoring_job_data(eligibility_verdict=eligibility.verdict),
        profile=profile,
    )
    return eligibility, score


def persist_eligibility_result(
    conn: sqlite3.Connection,
    snapshot: EvaluationSnapshot,
    result: EligibilityVerdict,
    *,
    now: str,
) -> str:
    """Write current eligibility iff the complete captured input is current."""

    if not snapshot_is_current(conn, snapshot):
        return STALE_INPUT
    cur = conn.execute(
        """
        INSERT INTO job_eligibility (
            job_id, profile_id, profile_revision_id, rules_revision_id,
            job_content_revision, normalization_version, evaluator_version,
            verdict, confidence, reason_codes_json, evidence_json,
            rule_version, evaluated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (job_id, profile_id) DO UPDATE SET
            profile_revision_id = excluded.profile_revision_id,
            rules_revision_id = excluded.rules_revision_id,
            job_content_revision = excluded.job_content_revision,
            normalization_version = excluded.normalization_version,
            evaluator_version = excluded.evaluator_version,
            verdict = excluded.verdict,
            confidence = excluded.confidence,
            reason_codes_json = excluded.reason_codes_json,
            evidence_json = excluded.evidence_json,
            rule_version = excluded.rule_version,
            evaluated_at = excluded.evaluated_at
        WHERE job_eligibility.profile_revision_id IS NOT excluded.profile_revision_id
           OR job_eligibility.rules_revision_id IS NOT excluded.rules_revision_id
           OR job_eligibility.job_content_revision IS NOT excluded.job_content_revision
           OR job_eligibility.normalization_version IS NOT excluded.normalization_version
           OR job_eligibility.evaluator_version IS NOT excluded.evaluator_version
           OR job_eligibility.rule_version IS NOT excluded.rule_version
        """,
        (
            snapshot.job_id,
            snapshot.profile_id,
            snapshot.profile_revision_id,
            snapshot.rules_revision_id,
            snapshot.job_content_revision,
            snapshot.normalization_version,
            snapshot.evaluator_version,
            result.verdict,
            result.confidence,
            json.dumps(result.reason_codes, sort_keys=True),
            json.dumps(result.evidence, sort_keys=True, default=str),
            snapshot.evaluator_version,
            now,
        ),
    )
    return WRITTEN if cur.rowcount == 1 else CURRENT_NOOP


def persist_score_result(
    conn: sqlite3.Connection,
    snapshot: EvaluationSnapshot,
    result: ScoreResult,
    *,
    now: str,
) -> str:
    """Write current score iff the complete captured input is still current."""

    if not snapshot_is_current(conn, snapshot):
        return STALE_INPUT
    if (
        result.scorer_version != snapshot.scorer_version
        or result.rule_version != snapshot.scoring_rule_version
    ):
        raise ValueError("score result code pins do not match captured evaluation input")
    cur = conn.execute(
        """
        INSERT INTO job_scores (
            job_id, profile_id, profile_revision_id, rules_revision_id,
            job_content_revision, normalization_version, scorer_version,
            eligibility_evaluator_version, score, breakdown_json,
            rule_version, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (job_id, profile_id) DO UPDATE SET
            profile_revision_id = excluded.profile_revision_id,
            rules_revision_id = excluded.rules_revision_id,
            job_content_revision = excluded.job_content_revision,
            normalization_version = excluded.normalization_version,
            scorer_version = excluded.scorer_version,
            eligibility_evaluator_version = excluded.eligibility_evaluator_version,
            score = excluded.score,
            breakdown_json = excluded.breakdown_json,
            rule_version = excluded.rule_version,
            scored_at = excluded.scored_at
        WHERE job_scores.profile_revision_id IS NOT excluded.profile_revision_id
           OR job_scores.rules_revision_id IS NOT excluded.rules_revision_id
           OR job_scores.job_content_revision IS NOT excluded.job_content_revision
           OR job_scores.normalization_version IS NOT excluded.normalization_version
           OR job_scores.scorer_version IS NOT excluded.scorer_version
           OR job_scores.eligibility_evaluator_version IS NOT excluded.eligibility_evaluator_version
           OR job_scores.rule_version IS NOT excluded.rule_version
        """,
        (
            snapshot.job_id,
            snapshot.profile_id,
            snapshot.profile_revision_id,
            snapshot.rules_revision_id,
            snapshot.job_content_revision,
            snapshot.normalization_version,
            snapshot.scorer_version,
            snapshot.evaluator_version,
            result.score,
            json.dumps(result.breakdown, sort_keys=True, default=str),
            snapshot.scoring_rule_version,
            now,
        ),
    )
    return WRITTEN if cur.rowcount == 1 else CURRENT_NOOP


def current_pair_is_materialized(
    conn: sqlite3.Connection, snapshot: EvaluationSnapshot
) -> bool:
    """True only when eligibility and score match one exact *current* input."""

    if not snapshot_is_current(conn, snapshot):
        return False
    row = conn.execute(
        """
        SELECT 1
          FROM job_eligibility e
          JOIN job_scores s
            ON s.job_id = e.job_id AND s.profile_id = e.profile_id
         WHERE e.job_id = ? AND e.profile_id = ?
           AND e.profile_revision_id IS ?
           AND e.rules_revision_id IS ?
           AND e.job_content_revision IS ?
           AND e.normalization_version IS ?
           AND e.evaluator_version IS ?
           AND e.rule_version IS ?
           AND s.profile_revision_id IS ?
           AND s.rules_revision_id IS ?
           AND s.job_content_revision IS ?
           AND s.normalization_version IS ?
           AND s.scorer_version IS ?
           AND s.eligibility_evaluator_version IS ?
           AND s.rule_version IS ?
        """,
        (
            snapshot.job_id,
            snapshot.profile_id,
            snapshot.profile_revision_id,
            snapshot.rules_revision_id,
            snapshot.job_content_revision,
            snapshot.normalization_version,
            snapshot.evaluator_version,
            snapshot.evaluator_version,
            snapshot.profile_revision_id,
            snapshot.rules_revision_id,
            snapshot.job_content_revision,
            snapshot.normalization_version,
            snapshot.scorer_version,
            snapshot.evaluator_version,
            snapshot.scoring_rule_version,
        ),
    ).fetchone()
    return row is not None


def materialize_profile_revision(
    conn: sqlite3.Connection,
    profile_id: str,
    profile_revision_id: str,
    *,
    now: str,
) -> int:
    """Synchronously materialize a newly-current immutable profile revision.

    RUN-21 requires profile edits to create new evaluations while making old
    rows non-current.  Profile mutation is local-only, so doing this inside the
    same caller-owned SQLite transaction gives a crash-safe all-or-nothing
    boundary without inventing source-network authority or a synthetic run.
    """

    current = conn.execute(
        "SELECT current_revision_id FROM search_profiles WHERE id = ?",
        (profile_id,),
    ).fetchone()
    if current is None or current["current_revision_id"] != profile_revision_id:
        return 0

    materialized = 0
    for row in conn.execute("SELECT id FROM jobs ORDER BY id").fetchall():
        snapshot = capture_evaluation_snapshot(conn, str(row["id"]), profile_id)
        if snapshot is None or snapshot.profile_revision_id != profile_revision_id:
            continue
        eligibility, score = evaluate_snapshot(snapshot)
        persist_eligibility_result(conn, snapshot, eligibility, now=now)
        persist_score_result(conn, snapshot, score, now=now)
        if current_pair_is_materialized(conn, snapshot):
            materialized += 1
    return materialized


def emit_inbox_if_current_pair(
    conn: sqlite3.Connection, snapshot: EvaluationSnapshot, *, now: str
) -> bool:
    """Emit existing PROD-02 triggers only after both current rows agree.

    The first obligation that materializes a matching pair may emit; later
    ELIGIBILITY/SCORE replay is harmless because durable Inbox dedupe keys own
    occurrence identity.
    """

    if not current_pair_is_materialized(conn, snapshot):
        return False
    maybe_emit_inbox_event(
        conn,
        job_id=snapshot.job_id,
        profile_id=snapshot.profile_id,
        event_kind="NEW_ELIGIBLE_APPEARANCE",
        now=now,
        commit=False,
    )
    maybe_emit_inbox_event(
        conn,
        job_id=snapshot.job_id,
        profile_id=snapshot.profile_id,
        event_kind="MEANINGFUL_CHANGE",
        trigger_content_revision=snapshot.job_content_revision,
        now=now,
        commit=False,
    )
    return True


__all__ = [
    "CURRENT_NOOP",
    "STALE_INPUT",
    "WRITTEN",
    "EvaluationSnapshot",
    "capture_evaluation_snapshot",
    "current_pair_is_materialized",
    "emit_inbox_if_current_pair",
    "evaluate_snapshot",
    "materialize_profile_revision",
    "persist_eligibility_result",
    "persist_score_result",
    "snapshot_is_current",
]
