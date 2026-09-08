"""Canonical projection and provenance selection (01 §39, 03 RUN-12/RUN-21).

Displayed canonical fields prefer the highest-quality current provenance:
employer structured ATS/API > employer structured API > employer careers page
> aggregator with resolved origin > aggregator without resolved origin
(01 §39).  Slice 2 moved that ordering out of a strategy-name proxy and into
``pipeline/provenance.py``, which ranks presences by the quality class recorded
from their own evidence.

Selection controls presentation only — every source and application link stays
inspectable in job_sources (RUN-12: canonical links are derived from
provenance records, not duplicated into jobs).  Projection updates are
evidence-ordered (RUN-21): an older or lower-quality observation processed late
cannot regress newer canonical state, and the derived location set plus the
categorical projection (remote mode, employment type, experience level) and the
rolled-up origin identity follow the winner.
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
    """Deprecated alias for the single owner in ``pipeline/provenance.py``.

    Kept so the accepted Slice-1 call sites and tests import unchanged; it
    delegates, so there is exactly one ordering implementation (01 §39).
    """
    from jobscraper.pipeline.provenance import (
        select_canonical_provenance as _select,
    )

    return _select(presences)


def presences_for_job(conn: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    """All presences of a job with each presence's latest-observation strategy."""
    from jobscraper.pipeline.provenance import presences_for_job as _presences

    return _presences(conn, job_id)


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
    """Re-derive the canonical projection for one job (01 §39, RUN-12/21).

    The winning provenance is selected by the §39 quality class (recorded per
    presence in v12), tie-broken by strategy quality, evidence recency and a
    stable id.  Presentation only: every presence keeps its rows, links and
    evidence, and an older or lower-quality observation never regresses newer
    canonical state.
    """
    from jobscraper.pipeline.locations import (
        LOCATION_RULES_VERSION,
        location_rows_equal,
        project_job_locations,
    )
    from jobscraper.pipeline.provenance import (
        PROVENANCE_SELECTOR_VERSION,
        select_canonical_provenance,
    )

    presences = presences_for_job(conn, job_id)
    winner = select_canonical_provenance(presences)
    winner_norm = normalized if winner["id"] == fresh_presence_id else None

    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    changes: list[str] = []

    if winner["id"] == fresh_presence_id:
        new_title = winner_norm.title or job["title"]
        if new_title != job["title"]:
            changes.append("TITLE_CHANGED")
        new_desc_hash = winner_norm.description_hash or job["description_hash"]
        if new_desc_hash != job["description_hash"]:
            changes.append("CONTENT_CHANGED")
    else:
        # Another presence still owns the presentation (a lower-quality or
        # older arrival cannot displace it, and this slice re-normalizes only
        # the fresh evidence, so the current projection stands unchanged).
        new_title = job["title"]
        new_desc_hash = job["description_hash"]
        winner_norm = None

    if winner_norm is not None:
        location_changed = not location_rows_equal(
            conn.execute(
                "SELECT raw_text, country, region, city, remote FROM job_locations"
                " WHERE job_id = ?",
                (job_id,),
            ).fetchall(),
            winner_norm.location_records,
        )
        if location_changed:
            changes.append("LOCATION_CHANGED")
    else:
        location_changed = False

    conn.execute(
        """
        UPDATE jobs SET
            title = ?, normalized_title = ?, description_md = ?,
            description_text = ?, description_lang = ?, description_hash = ?,
            salary_original_text = ?, salary_min = ?, salary_max = ?,
            salary_currency = ?, salary_period = ?,
            origin_provider = ?, origin_board = ?, origin_job_id = ?,
            remote_mode = ?, remote_worldwide = ?,
            employment_type = ?, experience_level = ?,
            location_rules_version = ?, provenance_selector_version = ?,
            canonical_provenance_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            new_title,
            (winner_norm.normalized_title if winner_norm else None) or job["normalized_title"],
            (winner_norm.description_md if winner_norm else None) or job["description_md"],
            (winner_norm.description_text if winner_norm else None) or job["description_text"],
            (winner_norm.description_lang if winner_norm else None) or job["description_lang"],
            new_desc_hash,
            (winner_norm.salary_original_text if winner_norm else None) or job["salary_original_text"],
            winner_norm.salary_min if winner_norm and winner_norm.salary_min is not None else job["salary_min"],
            winner_norm.salary_max if winner_norm and winner_norm.salary_max is not None else job["salary_max"],
            (winner_norm.salary_currency if winner_norm else None) or job["salary_currency"],
            (winner_norm.salary_period if winner_norm else None) or job["salary_period"],
            # origin identity rolls up from the winning presence's resolved
            # evidence (02 §32); it is never written by a collector directly
            winner["origin_provider"],
            winner["origin_board"],
            winner["origin_job_id"],
            (winner_norm.remote_mode if winner_norm else None) or job["remote_mode"],
            winner_norm.remote_worldwide if winner_norm else job["remote_worldwide"],
            (winner_norm.employment_type if winner_norm else None) or job["employment_type"],
            (winner_norm.experience_level if winner_norm else None) or job["experience_level"],
            LOCATION_RULES_VERSION,
            PROVENANCE_SELECTOR_VERSION,
            winner["id"],
            now,
            job_id,
        ),
    )
    if winner_norm is not None:
        project_job_locations(conn, job_id=job_id, records=winner_norm.location_records)
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
