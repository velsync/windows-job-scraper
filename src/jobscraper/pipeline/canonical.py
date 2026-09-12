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


def _latest_observation_projection(conn: sqlite3.Connection, presence: sqlite3.Row):
    """Re-normalize the winning presence's retained payload (Slice-1 behavior).

    A presence that is not the freshest one still owns the canonical
    presentation when its quality class wins, so its content — text, salary,
    categorical fields and the derived location set — is re-projected from the
    payload retained on its own observation.  No new fetch is performed and
    nothing is invented: no retained payload means no re-projection.
    """
    observation_id = presence["last_observation_id"]
    if not observation_id:
        return None
    row = conn.execute(
        "SELECT raw_payload_ref, observed_at FROM job_observations WHERE id = ?",
        (observation_id,),
    ).fetchone()
    if row is None or not row["raw_payload_ref"]:
        return None
    try:
        fields = json.loads(row["raw_payload_ref"])
    except ValueError:
        return None
    if not isinstance(fields, dict):
        return None

    class _RetainedObservation:
        """The minimal shape ``normalize_observation`` reads from a proposal."""

    shim = _RetainedObservation()
    shim.fields = fields
    from jobscraper.pipeline.normalize import normalize_observation

    return normalize_observation(shim, observed_at=row["observed_at"])


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
    # the freshest presence is already normalized by the caller; a non-fresh
    # winner is re-projected from its own retained payload
    winner_norm = (
        normalized
        if winner["id"] == fresh_presence_id
        else _latest_observation_projection(conn, winner)
    )

    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    changes: list[str] = []

    new_title = (winner_norm.title if winner_norm else None) or job["title"]
    if new_title != job["title"]:
        changes.append("TITLE_CHANGED")
    new_desc_hash = (
        winner_norm.description_hash if winner_norm else None
    ) or job["description_hash"]
    if new_desc_hash != job["description_hash"]:
        changes.append("CONTENT_CHANGED")

    location_changed = False
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

    # S3.11 RUN-21: one canonical monotonic coordinate owns eligibility/score
    # freshness. Per-source content revisions are not globally comparable, so
    # advance only when an input actually consumed by those evaluators changes.
    next_normalized_title = (
        (winner_norm.normalized_title if winner_norm else None)
        or job["normalized_title"]
    )
    next_salary_min = (
        winner_norm.salary_min
        if winner_norm is not None and winner_norm.salary_min is not None
        else job["salary_min"]
    )
    next_salary_max = (
        winner_norm.salary_max
        if winner_norm is not None and winner_norm.salary_max is not None
        else job["salary_max"]
    )
    next_salary_currency = (
        (winner_norm.salary_currency if winner_norm else None)
        or job["salary_currency"]
    )
    next_salary_period = (
        (winner_norm.salary_period if winner_norm else None)
        or job["salary_period"]
    )
    next_remote_worldwide = (
        winner_norm.remote_worldwide if winner_norm else job["remote_worldwide"]
    )
    evaluation_changed = location_changed or any(
        (
            new_title != job["title"],
            next_normalized_title != job["normalized_title"],
            next_salary_min != job["salary_min"],
            next_salary_max != job["salary_max"],
            next_salary_currency != job["salary_currency"],
            next_salary_period != job["salary_period"],
            int(bool(next_remote_worldwide)) != int(job["remote_worldwide"]),
        )
    )

    conn.execute(
        """
        UPDATE jobs SET
            title = ?, normalized_title = ?, description_md = ?,
            description_text = ?, description_lang = ?, description_hash = ?,
            salary_original_text = ?, salary_min = ?, salary_max = ?,
            salary_currency = ?, salary_period = ?, posted_at = ?,
            origin_provider = ?, origin_board = ?, origin_job_id = ?,
            remote_mode = ?, remote_worldwide = ?,
            employment_type = ?, experience_level = ?,
            location_rules_version = ?, provenance_selector_version = ?,
            content_cleaning_version = ?,
            evaluation_revision = evaluation_revision + ?,
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
            # A publication time is a stable fact, so it is *filled*, never
            # rewritten: a listing that states no posted time (the common
            # provider-native shape — S2.5) leaves the row NULL until an
            # observation of the winning presence states one, and a value
            # already established is never replaced by a later disagreement
            # (that would flap a fact no change class records).
            job["posted_at"] or (winner_norm.posted_at if winner_norm else None),
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
            (winner_norm.content_cleaning_version if winner_norm else None)
            or job["content_cleaning_version"],
            1 if evaluation_changed else 0,
            winner["id"],
            now,
            job_id,
        ),
    )
    if winner_norm is not None and location_changed:
        project_job_locations(conn, job_id=job_id, records=winner_norm.location_records)
    for change_class in changes:
        record_change(conn, job_id, change_class, now)

    # S2.3: keep the searchable snapshot in step with the canonical projection
    # (host-owned, revision-checked inside the caller's fence).
    from jobscraper.search.index import sync_search_doc

    sync_search_doc(conn, job_id=job_id, now=now)


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
