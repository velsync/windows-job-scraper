"""Entity resolution: stage-1 native identity + reuse guard (01 §38/PROD-03, 03 RUN-15).

Stage 1: same (source_id, source_job_id) is strong identity — UNLESS the
reuse guard detects incompatible temporal/entity/content evidence, in
which case the identity splits: a new canonical job is created and the
source-presence row moves to the next source_identity_generation.

Splitting is the safe direction: a wrong split is recoverable through the
merge ledger; a wrong silent merge is not.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobscraper.pipeline.normalize import NormalizedContent, normalize_company

# A closed presence older than this (combined with incompatible entity
# evidence) is treated as probable source-native-ID reuse (PROD-03).
REUSE_GUARD_CLOSED_INTERVAL_DAYS = 90


def find_current_presence(
    conn: sqlite3.Connection, source_id: str, source_job_id: str | None
) -> sqlite3.Row | None:
    """The current-generation presence row for a native identity, if any."""
    if not source_job_id:
        return None
    return conn.execute(
        """
        SELECT * FROM job_sources
        WHERE source_id = ? AND source_job_id = ?
        ORDER BY source_identity_generation DESC, created_at DESC
        LIMIT 1
        """,
        (source_id, source_job_id),
    ).fetchone()


def _title_similarity(a: str, b: str) -> float:
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


@dataclass(frozen=True)
class EntityResolution:
    job_id: str | None
    decision: str  # CREATED | MATCHED | SPLIT_REUSE
    generation: int
    guard_fired: bool
    guard_evidence: dict


def _is_job_specific_url(url: str | None) -> bool:
    """§31: a generic careers/home/apply URL alone MUST NOT cause an
    automatic merge. Job-specific means an identifying path segment
    beyond a bare careers/home/apply/jobs root."""
    if not url:
        return False
    from jobscraper.net.urlnorm import normalize_url

    try:
        normalized = normalize_url(url)
    except Exception:
        return False
    segments = [seg for seg in normalized.path.split("/") if seg]
    if len(segments) < 2:
        return False
    first = segments[0].lower()
    if first in ("careers", "home", "apply", "jobs", "job", "vacancies") and len(segments) < 2:
        return False
    # the second segment must look identifying (id/slug), not another generic word
    if len(segments) >= 2 and segments[1].lower() in ("list", "search", "all", "feed"):
        return False
    return True


def resolve_entity(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    source_job_id: str | None,
    normalized: NormalizedContent,
    observed_at: str,
    existing: sqlite3.Row | None,
    canonical_url_candidate: str | None = None,
) -> EntityResolution:
    """Decide the canonical identity for one normalized observation.

    Stage 1: same (source_id, source_job_id) is strong identity unless the
    reuse guard fires. Stage 3 (§38): a sufficiently job-specific canonical
    URL shared with an existing presence attaches the new source's
    presence to that canonical job — no two canonical jobs are ever merged
    by this stage, so no merge-ledger entry is required."""
    if existing is None:
        if _is_job_specific_url(canonical_url_candidate):
            from jobscraper.net.urlnorm import url_identity

            try:
                wanted = url_identity(canonical_url_candidate)
            except Exception:
                wanted = None
            if wanted:
                match = conn.execute(
                    "SELECT job_id FROM job_sources WHERE canonical_job_url = ?"
                    " ORDER BY last_seen_at DESC LIMIT 1",
                    (wanted,),
                ).fetchone()
                if match:
                    return EntityResolution(
                        match["job_id"], "MATCHED_URL", 1, False,
                        {"stage": "canonical_url", "url": wanted},
                    )
        return EntityResolution(None, "CREATED", 1, False, {})

    prev_company = existing_row_company(conn, existing)
    company_incompatible = bool(
        normalized.normalized_company
        and prev_company
        and normalized.normalized_company != prev_company
    )
    prev_title = existing_row_title(conn, existing)
    title_similar = _title_similarity(normalized.title or "", prev_title or "") >= 0.3

    from datetime import datetime, timedelta

    from jobscraper.timeutil import parse_rfc3339

    last_seen = parse_rfc3339(existing["last_seen_at"])
    observed = parse_rfc3339(observed_at)
    long_gap = observed - last_seen > timedelta(days=REUSE_GUARD_CLOSED_INTERVAL_DAYS)
    closed = existing["presence_state"] in ("CLOSED", "WITHDRAWN", "EXPIRED")

    guard_evidence = {
        "company_incompatible": company_incompatible,
        "title_similar": title_similar,
        "long_closed_interval": bool(long_gap and closed),
        "presence_state": existing["presence_state"],
    }
    # PROD-03: incompatible entity evidence + (closed presence or long gap)
    # -> probable native-ID reuse -> split. Incompatible company evidence
    # alone on a live posting also refuses silent merging.
    guard_fires = company_incompatible and (closed or long_gap or not title_similar)
    if guard_fires:
        return EntityResolution(
            None,
            "SPLIT_REUSE",
            int(existing["source_identity_generation"]) + 1,
            True,
            guard_evidence,
        )
    return EntityResolution(existing["job_id"], "MATCHED", int(existing["source_identity_generation"]), False, guard_evidence)


def existing_row_company(conn: sqlite3.Connection, presence: sqlite3.Row) -> str | None:
    row = conn.execute(
        "SELECT company_id FROM jobs WHERE id = ?", (presence["job_id"],)
    ).fetchone()
    if row is None or row["company_id"] is None:
        return None
    company = conn.execute(
        "SELECT normalized_name FROM companies WHERE id = ?", (row["company_id"],)
    ).fetchone()
    return company["normalized_name"] if company else None


def existing_row_title(conn: sqlite3.Connection, presence: sqlite3.Row) -> str | None:
    row = conn.execute(
        "SELECT title FROM jobs WHERE id = ?", (presence["job_id"],)
    ).fetchone()
    return row["title"] if row else None


__all__ = [
    "EntityResolution",
    "REUSE_GUARD_CLOSED_INTERVAL_DAYS",
    "find_current_presence",
    "resolve_entity",
]
