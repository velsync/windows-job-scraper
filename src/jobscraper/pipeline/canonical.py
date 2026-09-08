"""Canonical projection and provenance selection (01 §39, 03 RUN-12/RUN-21).

Displayed canonical fields prefer the highest-quality current provenance
(structured employer/API > careers page > aggregator); this controls
presentation only — every source and application link stays inspectable
in job_sources (RUN-12: canonical links are derived from provenance
records, not duplicated into jobs). Projection updates are
evidence-ordered (RUN-21): an older observation processed late cannot
regress newer canonical state.
"""

from __future__ import annotations

import json
import sqlite3

from jobscraper.ids import new_id

# §39 source-quality ordering, proxied by strategy for Slice 1.
STRATEGY_QUALITY = {
    "PROVIDER_NATIVE": 6,
    "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT": 5,
    "STRUCTURED_PAGE": 4,
    "HTTP_HTML": 3,
    "PLAYWRIGHT_PUBLIC": 2,
    "PLAYWRIGHT_AUTHENTICATED": 1,
    "MANUAL_UNSUPPORTED": 0,
    "GENERIC_DISCOVERY": 0,
}


def select_canonical_provenance(presences: list[sqlite3.Row]) -> sqlite3.Row:
    """Pick the provenance winner: strategy quality first, then recency.

    ``presences`` rows must expose ``strategy_source`` (the strategy of the
    presence's latest observation) and ``last_seen_at``.
    """
    if not presences:
        raise ValueError("cannot select canonical provenance from no presences")
    return max(
        presences,
        key=lambda p: (
            STRATEGY_QUALITY.get(p["strategy_source"], 0),
            p["last_seen_at"] or "",
        ),
    )


def presences_for_job(conn: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    """All presences of a job with each presence's latest-observation strategy."""
    return conn.execute(
        """
        SELECT js.*, COALESCE(
            (SELECT o.strategy FROM job_observations o WHERE o.id = js.last_observation_id),
            'HTTP_HTML') AS strategy_source
        FROM job_sources js WHERE js.job_id = ?
        """,
        (job_id,),
    ).fetchall()


def _latest_observation_fields(conn: sqlite3.Connection, presence: sqlite3.Row) -> dict:
    obs_id = presence["last_observation_id"]
    if not obs_id:
        return {}
    row = conn.execute(
        "SELECT raw_payload_ref FROM job_observations WHERE id = ?", (obs_id,)
    ).fetchone()
    if row is None or not row["raw_payload_ref"]:
        return {}
    try:
        return json.loads(row["raw_payload_ref"])
    except ValueError:
        return {}


def refresh_canonical_presentation(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    now: str,
    normalized,
    fresh_presence_id: str,
) -> None:
    """Re-derive the canonical projection for one job (RUN-21 ordered).

    The caller has already updated the presence rows for this observation.
    This selects the winning provenance and applies that winner's content
    to the canonical row, recording meaningful change classes (never
    regressing to older or lower-quality evidence).
    """
    presences = presences_for_job(conn, job_id)
    winner = select_canonical_provenance(presences)

    if winner["id"] == fresh_presence_id:
        winner_norm = normalized
    else:
        # A different, higher-quality presence owns the presentation;
        # re-apply its latest observation content.
        from jobscraper.pipeline.normalize import normalize_observation

        class _Shim:
            pass

        shim = _Shim()
        shim.fields = _latest_observation_fields(conn, winner)
        winner_norm = normalize_observation(shim)

    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    changes: list[str] = []

    new_title = winner_norm.title or job["title"]
    if new_title != job["title"]:
        changes.append("TITLE_CHANGED")
    new_desc_hash = winner_norm.description_hash or job["description_hash"]
    if new_desc_hash != job["description_hash"]:
        changes.append("CONTENT_CHANGED")

    conn.execute(
        """
        UPDATE jobs SET
            title = ?, normalized_title = ?, description_md = ?,
            description_text = ?, description_lang = ?, description_hash = ?,
            salary_original_text = ?, salary_min = ?, salary_max = ?,
            salary_currency = ?, salary_period = ?,
            canonical_provenance_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            new_title,
            winner_norm.normalized_title or job["normalized_title"],
            winner_norm.description_md or job["description_md"],
            winner_norm.description_text or job["description_text"],
            winner_norm.description_lang or job["description_lang"],
            new_desc_hash,
            winner_norm.salary_original_text or job["salary_original_text"],
            winner_norm.salary_min if winner_norm.salary_min is not None else job["salary_min"],
            winner_norm.salary_max if winner_norm.salary_max is not None else job["salary_max"],
            winner_norm.salary_currency or job["salary_currency"],
            winner_norm.salary_period or job["salary_period"],
            winner["id"],
            now,
            job_id,
        ),
    )
    for change_class in changes:
        record_change(conn, job_id, change_class, now)


def record_change(conn: sqlite3.Connection, job_id: str, change_class: str, now: str) -> None:
    conn.execute(
        "INSERT INTO job_history (id, job_id, at, change_class, detail_json)"
        " VALUES (?, ?, ?, ?, '{}')",
        (new_id("jh"), job_id, now, change_class),
    )


__all__ = [
    "STRATEGY_QUALITY",
    "presences_for_job",
    "record_change",
    "refresh_canonical_presentation",
    "select_canonical_provenance",
]
