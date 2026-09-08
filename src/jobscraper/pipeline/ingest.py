"""Fenced observation ingestion (03 §29/§30, RUN-11, RUN-15, RUN-21; ACQ-02).

``ingest_observation`` runs INSIDE the caller's fenced commit (S1.3) and
performs, atomically:

1. immutable JobObservation insert (request-scoped idempotency — a retry
   after PARTIAL/crash cannot duplicate) + field evidence rows;
2. deterministic normalization;
3. entity resolution (stage-1 native identity + reuse guard);
4. job_sources presence create/update (RUN-11 ordering: entity resolution
   establishes the job_id first; the initial presence row is created in
   the same transaction);
5. canonical projection refresh with provenance selection (§39) under
   RUN-21 ordering;
6. atomic creation of host-native downstream obligations (RECONCILE /
   ELIGIBILITY / SCORE) for the accepted observation.

A collector never writes canonical jobs directly — this module is the
only bridge, and it is fence-owned.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from jobscraper.ids import new_id
from jobscraper.pipeline.canonical import (
    record_change,
    refresh_canonical_presentation,
)
from jobscraper.pipeline.entity import (
    find_current_presence,
    resolve_entity,
)
from jobscraper.pipeline.normalize import normalize_observation
from jobscraper.runtime.requests import enqueue_request

_ABSENCE_AUTHORITIES = frozenset(
    {"AUTHORITATIVE_FULL_SOURCE", "AUTHORITATIVE_DECLARED_SCOPE"}
)


def observation_key(source_id: str, observation, page_cursor_json: str | None) -> str:
    """Deterministic observation identity (03 §29): stable source-native
    identity (or a stable normalized record key) plus logical cursor
    identity."""
    source_job_id = observation.source_job_id
    if source_job_id:
        basis = json.dumps(
            ["native", source_id, source_job_id], separators=(",", ":")
        )
    else:
        fields = observation.fields or {}
        basis = json.dumps(
            [
                "content",
                source_id,
                str(fields.get("title") or ""),
                str(fields.get("company") or ""),
                str(fields.get("job_url") or ""),
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
    if page_cursor_json:
        basis += "|" + page_cursor_json
    return hashlib.sha256(basis.encode()).hexdigest()


def ingest_observation(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    attempt_id: str | None,
    observation,
    source_id: str,
    binding_id: str,
    adapter_id: str,
    adapter_version: str,
    strategy: str,
    execution_class: str,
    observed_at: str,
    now: str,
    query_id: str | None = None,
) -> dict:
    """Ingest one observation proposal inside the caller's fence.

    Returns a summary dict (``observation_id``, ``job_id``, ``decision``,
    ``idempotent``). Never commits — the fence owns the transaction.
    """
    run_row = conn.execute(
        "SELECT run_id, run_source_plan_id FROM scrape_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if run_row is None:  # pragma: no cover - fence guarantees existence
        raise ValueError(f"request {request_id!r} does not exist")
    run_id = run_row["run_id"]
    plan_id = run_row["run_source_plan_id"]

    key = observation_key(source_id, observation, observation.page_cursor_json)
    observation_id = new_id("obs")
    inserted = conn.execute(
        """
        INSERT INTO job_observations (
            id, run_id, request_id, attempt_id, source_id, binding_id,
            adapter_id, adapter_version, strategy, execution_class, query_id,
            source_job_id, raw_url, canonical_url_candidate,
            application_url_candidate, page_cursor_json, source_rank_or_order,
            raw_payload_ref, parse_evidence_ref, observed_at,
            observation_unique_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (request_id, observation_unique_key) DO NOTHING
        """,
        (
            observation_id,
            run_id,
            request_id,
            attempt_id,
            source_id,
            binding_id,
            adapter_id,
            adapter_version,
            strategy,
            execution_class,
            query_id,
            observation.source_job_id,
            observation.raw_url,
            observation.canonical_url_candidate,
            observation.application_url_candidate,
            observation.page_cursor_json,
            observation.source_rank_or_order,
            json.dumps(dict(observation.fields or {}), sort_keys=True, default=str),
            None,  # parse_evidence_ref set below with the content hash
            observed_at,
            key,
        ),
    )
    if inserted.rowcount == 0:
        existing = conn.execute(
            "SELECT id FROM job_observations WHERE request_id = ? AND observation_unique_key = ?",
            (request_id, key),
        ).fetchone()
        return {
            "observation_id": existing["id"],
            "job_id": None,
            "decision": "IDEMPOTENT",
            "idempotent": True,
        }

    normalized = normalize_observation(observation)
    conn.execute(
        "UPDATE job_observations SET parse_evidence_ref = ? WHERE id = ?",
        (normalized.content_hash, observation_id),
    )
    for evidence in observation.field_evidence:
        conn.execute(
            """
            INSERT INTO field_evidence (
                id, observation_id, field_name, locator_kind, locator_value,
                value_hash, excerpt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("fe"),
                observation_id,
                evidence.field_name,
                evidence.locator_kind,
                evidence.locator_value,
                evidence.value_hash,
                evidence.excerpt[:500],
                now,
            ),
        )

    existing_presence = find_current_presence(conn, source_id, observation.source_job_id)
    resolution = resolve_entity(
        conn,
        source_id=source_id,
        source_job_id=observation.source_job_id,
        normalized=normalized,
        observed_at=observed_at,
        existing=existing_presence,
        canonical_url_candidate=observation.canonical_url_candidate,
    )

    if resolution.decision == "CREATED":
        job_id = _create_canonical_job(
            conn, normalized=normalized, observed_at=observed_at, now=now
        )
    elif resolution.decision == "SPLIT_REUSE":
        job_id = _create_canonical_job(
            conn, normalized=normalized, observed_at=observed_at, now=now
        )
    else:
        job_id = resolution.job_id

    conn.execute(
        "INSERT INTO entity_resolution_events (id, observation_id, job_id, stage,"
        " decision, match_score, reason_code, evidence_json, decided_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("ere"),
            observation_id,
            job_id,
            "native_identity_reuse_guard" if resolution.guard_fired else "native_identity_stage1",
            resolution.decision,
            None,
            "REUSE_GUARD" if resolution.guard_fired else None,
            json.dumps(resolution.guard_evidence, sort_keys=True, default=str),
            now,
        ),
    )

    presence_id, presence_updated = _upsert_presence(
        conn,
        job_id=job_id,
        source_id=source_id,
        binding_id=binding_id,
        observation=observation,
        observation_id=observation_id,
        normalized=normalized,
        generation=resolution.generation,
        observed_at=observed_at,
        now=now,
        # presence rows are per (job, source, native id): a cross-source
        # URL match attaches a NEW presence row, never rewrites another
        # source's presence
        existing=existing_presence if resolution.decision == "MATCHED" else None,
    )

    if presence_updated:
        # RUN-21: only forward-evidence observations re-project canonical
        # state; a stale observation stays immutable history.
        refresh_canonical_presentation(
            conn,
            job_id,
            now=now,
            normalized=normalized,
            fresh_presence_id=presence_id,
        )

    # Atomic downstream obligations for the accepted observation (RUN-08:
    # created in the same fenced transaction; they drain as host-native
    # requests even after cancellation).
    for request_type in ("RECONCILE", "ELIGIBILITY", "SCORE"):
        enqueue_request(
            conn,
            run_id=run_id,
            run_source_plan_id=plan_id,
            source_id=source_id,
            binding_id=binding_id,
            request_type=request_type,
            target_identity=observation_id,
            logical_key=normalized.content_hash,
            payload={"observation_id": observation_id, "job_id": job_id},
            priority=-10,  # host-native obligations drain after acquisition work
            commit=False,
        )

    return {
        "observation_id": observation_id,
        "job_id": job_id,
        "decision": resolution.decision,
        "idempotent": False,
    }


def _create_canonical_job(
    conn: sqlite3.Connection, *, normalized, observed_at: str, now: str
) -> str:
    from jobscraper.pipeline.normalize import normalize_company

    company_id = None
    if normalized.normalized_company:
        existing = conn.execute(
            "SELECT id FROM companies WHERE normalized_name = ?",
            (normalized.normalized_company,),
        ).fetchone()
        if existing:
            company_id = existing["id"]
        else:
            company_id = new_id("co")
            conn.execute(
                """
                INSERT INTO companies (id, name, normalized_name, first_seen_at,
                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    normalized.company_name,
                    normalized.normalized_company,
                    observed_at,
                    now,
                    now,
                ),
            )
    job_id = new_id("job")
    conn.execute(
        """
        INSERT INTO jobs (
            id, company_id, title, normalized_title, description_md,
            description_text, description_lang, description_hash,
            salary_original_text, salary_min, salary_max, salary_currency,
            salary_period, posted_at, discovered_at, first_seen_at, last_seen_at,
            last_verified_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            company_id,
            normalized.title,
            normalized.normalized_title,
            normalized.description_md,
            normalized.description_text,
            normalized.description_lang,
            normalized.description_hash,
            normalized.salary_original_text,
            normalized.salary_min,
            normalized.salary_max,
            normalized.salary_currency,
            normalized.salary_period,
            normalized.posted_at,
            now,
            observed_at,
            observed_at,
            observed_at,
            now,
            now,
        ),
    )
    if normalized.locations:
        from jobscraper.ids import new_id as _new_id

        for raw in normalized.locations:
            conn.execute(
                """
                INSERT INTO job_locations (id, job_id, raw_text)
                VALUES (?, ?, ?)
                """,
                (_new_id("jl"), job_id, raw),
            )
    return job_id


def _upsert_presence(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    source_id: str,
    binding_id: str,
    observation,
    observation_id: str,
    normalized,
    generation: int,
    observed_at: str,
    now: str,
    existing: sqlite3.Row | None,
) -> str:
    if existing is None:
        presence_id = new_id("js")
        updated = True
        conn.execute(
            """
            INSERT INTO job_sources (
                id, job_id, source_id, binding_id, source_job_id,
                source_identity_generation, discovery_url, raw_source_url,
                canonical_job_url, application_url, origin_url, first_seen_at,
                last_seen_at, last_verified_at, presence_state, content_revision,
                last_observation_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 1, ?, ?, ?)
            """,
            (
                presence_id,
                job_id,
                source_id,
                binding_id,
                observation.source_job_id,
                generation,
                observation.raw_url,  # discovery URL: never overwritten later
                observation.raw_url,
                observation.canonical_url_candidate,
                observation.application_url_candidate,
                None,
                observed_at,
                observed_at,
                observed_at,
                observation_id,
                now,
                now,
            ),
        )
        return presence_id, updated

    # RUN-21: an older observation processed late must not regress the
    # presence projection.
    if observed_at < (existing["last_seen_at"] or ""):
        return existing["id"], False

    previous_hash = None
    if existing["last_observation_id"]:
        row = conn.execute(
            "SELECT parse_evidence_ref FROM job_observations WHERE id = ?",
            (existing["last_observation_id"],),
        ).fetchone()
        previous_hash = row["parse_evidence_ref"] if row else None
    content_changed = previous_hash != normalized.content_hash

    if (observation.application_url_candidate or None) != (existing["application_url"] or None):
        record_change(conn, job_id, "APPLY_URL_CHANGED", now)

    conn.execute(
        """
        UPDATE job_sources SET
            binding_id = ?, last_seen_at = ?, last_verified_at = ?,
            canonical_job_url = COALESCE(?, canonical_job_url),
            application_url = COALESCE(?, application_url),
            content_revision = CASE WHEN ? THEN content_revision + 1
                                    ELSE content_revision END,
            presence_state = CASE WHEN presence_state IN ('UNCERTAIN', 'UNKNOWN')
                                  THEN 'ACTIVE' ELSE presence_state END,
            last_observation_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            binding_id,
            observed_at,
            observed_at,
            observation.canonical_url_candidate,
            observation.application_url_candidate,
            content_changed,
            observation_id,
            now,
            existing["id"],
        ),
    )
    return existing["id"], True


__all__ = ["ingest_observation", "observation_key"]
