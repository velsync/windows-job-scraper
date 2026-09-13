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
from dataclasses import dataclass

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


def _latest_observation_projection(
    conn: sqlite3.Connection, presence: sqlite3.Row, *, normalized=None,
):
    """Project the latest semantic change among this presence's accepted views.

    Repeated LIST/DETAIL (or other request-kind) views are verification, not
    new content authority. Collapse equal normalized states within each kind
    before selecting the latest transition across kinds. Thus an unchanged
    poorer view cannot undo richer evidence, while a changed view still wins.
    The latest-observation pointer continues to record the newest sighting.
    """
    from types import SimpleNamespace

    from jobscraper.pipeline.normalize import normalize_observation

    observation_id = presence["last_observation_id"]
    if not observation_id:
        return None
    rows = conn.execute(
        """
        SELECT o.id, o.raw_payload_ref, o.observed_at, r.request_type
          FROM job_observations o
          JOIN scrape_requests r ON r.id = o.request_id
         WHERE o.source_id = ? AND o.source_job_id IS ?
           AND (o.id = ? OR (? AND o.resolved_job_id = ?
                AND o.run_effect IN ('NEW_JOB', 'UPDATED_JOB', 'UNCHANGED_JOB')))
           AND o.rowid <= (SELECT rowid FROM job_observations WHERE id = ?)
         ORDER BY o.rowid
        """,
        (presence["source_id"], presence["source_job_id"], observation_id,
         bool(presence["source_job_id"]), presence["job_id"], observation_id),
    )
    previous_by_kind = {}
    winner = None
    for row in rows:
        if row["id"] == observation_id and normalized is not None:
            current = normalized
        else:
            try:
                fields = json.loads(row["raw_payload_ref"] or "null")
            except ValueError:
                fields = None
            if not isinstance(fields, dict):
                # Missing retained evidence cannot authorize a re-projection.
                if row["id"] == observation_id:
                    return None
                continue
            current = normalize_observation(
                SimpleNamespace(fields=fields), observed_at=row["observed_at"],
            )
        kind = row["request_type"]
        # Compare the whole normalized projection: content_hash deliberately
        # covers only title/company/description and misses location/salary/etc.
        if current != previous_by_kind.get(kind):
            winner = current
        previous_by_kind[kind] = current
    return winner


@dataclass(frozen=True)
class CanonicalRefreshResult:
    projection_changed: bool
    evaluation_changed: bool
    change_classes: tuple[str, ...]


def refresh_canonical_presentation(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    now: str,
    normalized,
    fresh_presence_id: str,
) -> CanonicalRefreshResult:
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
    winner_norm = _latest_observation_projection(
        conn, winner,
        normalized=normalized if winner["id"] == fresh_presence_id else None,
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

    # A5.1: semantic projection comparison — every field the UPDATE derives
    # from current evidence, excluding housekeeping timestamps. Version
    # columns ride along but never alone constitute a semantic change.
    _new_desc_md = (winner_norm.description_md if winner_norm else None) or job["description_md"]
    _new_desc_text = (winner_norm.description_text if winner_norm else None) or job["description_text"]
    _new_desc_lang = (winner_norm.description_lang if winner_norm else None) or job["description_lang"]
    _new_salary_text = (winner_norm.salary_original_text if winner_norm else None) or job["salary_original_text"]
    _new_posted = job["posted_at"] or (winner_norm.posted_at if winner_norm else None)
    _new_remote_mode = (winner_norm.remote_mode if winner_norm else None) or job["remote_mode"]
    _new_employment = (winner_norm.employment_type if winner_norm else None) or job["employment_type"]
    _new_experience = (winner_norm.experience_level if winner_norm else None) or job["experience_level"]
    projection_changed = any(
        (
            new_title != job["title"],
            next_normalized_title != job["normalized_title"],
            _new_desc_md != job["description_md"],
            _new_desc_text != job["description_text"],
            _new_desc_lang != job["description_lang"],
            new_desc_hash != job["description_hash"],
            _new_salary_text != job["salary_original_text"],
            next_salary_min != job["salary_min"],
            next_salary_max != job["salary_max"],
            next_salary_currency != job["salary_currency"],
            next_salary_period != job["salary_period"],
            (_new_posted or None) != (job["posted_at"] or None),
            (winner["origin_provider"] or None) != (job["origin_provider"] or None),
            (winner["origin_board"] or None) != (job["origin_board"] or None),
            (winner["origin_job_id"] or None) != (job["origin_job_id"] or None),
            (_new_remote_mode or None) != (job["remote_mode"] or None),
            int(bool(next_remote_worldwide)) != int(job["remote_worldwide"]),
            (_new_employment or None) != (job["employment_type"] or None),
            (_new_experience or None) != (job["experience_level"] or None),
            (winner["id"] or None) != (job["canonical_provenance_id"] or None),
            bool(location_changed),
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

    return CanonicalRefreshResult(
        projection_changed=projection_changed,
        evaluation_changed=evaluation_changed,
        change_classes=tuple(changes),
    )


def record_change(conn: sqlite3.Connection, job_id: str, change_class: str, now: str) -> None:
    conn.execute(
        "INSERT INTO job_history (id, job_id, at, change_class, detail_json)"
        " VALUES (?, ?, ?, ?, '{}')",
        (new_id("jh"), job_id, now, change_class),
    )


__all__ = [
    "STRATEGY_QUALITY",
    "CanonicalRefreshResult",
    "presences_for_job",
    "record_change",
    "refresh_canonical_presentation",
    "select_canonical_provenance",
]
