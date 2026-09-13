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
from dataclasses import dataclass

from jobscraper.acquisition.origin import OriginResolution, OriginStatus
from jobscraper.adapters.contract import CONTRACT_VERSION
from jobscraper.ids import new_id
from jobscraper.pipeline.canonical import (
    record_change,
    refresh_canonical_presentation,
)
from jobscraper.pipeline.evidence import bounded_json, excerpt_and_hash
from jobscraper.pipeline.entity import (
    find_current_presence,
    resolve_entity,
)
from jobscraper.pipeline.normalize import normalize_observation
from jobscraper.runtime.requests import enqueue_request

_ABSENCE_AUTHORITIES = frozenset(
    {"AUTHORITATIVE_FULL_SOURCE", "AUTHORITATIVE_DECLARED_SCOPE"}
)


@dataclass(frozen=True)
class PresenceUpsertResult:
    presence_id: str
    accepted: bool
    created: bool
    semantic_changed: bool


_RUN_EFFECTS = frozenset({"NEW_JOB", "UPDATED_JOB", "UNCHANGED_JOB", "STALE_IGNORED"})


class ObservationEffectIntegrityError(RuntimeError):
    pass


def _origin_evidence_substance(payload: object):
    """Origin evidence with per-run and per-view noise removed.

    ``origin_resolution_evidence_json`` embeds two kinds of non-semantic
    churn:

    * per-run verification timestamps (top-level ``resolved_at`` and
      per-item ``observed_at``), which advance on every reverification like
      ``last_seen_at`` and must never by themselves count as a change;
    * the per-view corroboration path: a listing view may corroborate the
      same origin identity through ``application_url`` while the detail view
      of the same job corroborates through ``canonical_job_url`` (or vice
      versa). The resolved identity, confidence and strength are identical;
      only the diagnostic field label alternates with the view.

    The substance comparison keeps the resolved projection (status,
    identity, confidence, conflict, rejected candidates, resolver versions
    and URL outcomes) plus, per evidence item, only its probative content
    ``(kind, strength, value, pattern_id)``. A real change in corroboration,
    strength, value, matching rule, conflict or resolver version still fires.
    """
    if payload is None:
        return None
    if not isinstance(payload, str):
        return payload
    try:
        data = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(data, dict):
        return data
    data = dict(data)
    data.pop("resolved_at", None)
    evidence = data.get("evidence")
    if isinstance(evidence, list):
        data["evidence"] = sorted(
            (
                str(item.get("kind")),
                str(item.get("strength", "")),
                str(item.get("value", "")),
                str(item.get("pattern_id", "")),
            )
            for item in evidence
            if isinstance(item, dict)
        )
    return data


def _bind_observation_effect(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    job_id: str,
    run_effect: str,
) -> None:
    if run_effect not in _RUN_EFFECTS:
        raise ValueError("invalid run effect")

    cur = conn.execute(
        """
        UPDATE job_observations
           SET resolved_job_id = ?, run_effect = ?
         WHERE id = ?
           AND resolved_job_id IS NULL
           AND run_effect IS NULL
        """,
        (job_id, run_effect, observation_id),
    )
    if cur.rowcount == 1:
        return

    row = conn.execute(
        "SELECT resolved_job_id, run_effect FROM job_observations WHERE id = ?",
        (observation_id,),
    ).fetchone()
    if row is None:
        raise ObservationEffectIntegrityError("observation disappeared")
    if row["resolved_job_id"] == job_id and row["run_effect"] == run_effect:
        return

    raise ObservationEffectIntegrityError(
        "observation effect already bound inconsistently"
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


def _resolution_event_stage(resolution) -> str:
    """Map entity-resolution evidence to the truthful durable stage label."""
    stage = (resolution.guard_evidence or {}).get("stage")
    if stage == "origin_identity":
        return "origin_identity_stage2"
    if stage == "canonical_url":
        return "canonical_url_stage3"
    if resolution.guard_fired:
        return "native_identity_reuse_guard"
    return "native_identity_stage1"


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
    fetch_attempt_id: str | None = None,
    parse_attempt_id: str | None = None,
    origin: OriginResolution | None = None,
    content_kind: str | None = None,
    same_host_as_source: bool | None = None,
    source_family: str | None = None,
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
            observation_unique_key, fetch_attempt_id, parse_attempt_id,
            contract_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 ?, ?, ?)
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
            fetch_attempt_id,
            parse_attempt_id,
            CONTRACT_VERSION,
        ),
    )
    if inserted.rowcount == 0:
        existing = conn.execute(
            "SELECT id, resolved_job_id, run_effect FROM job_observations"
            " WHERE request_id = ? AND observation_unique_key = ?",
            (request_id, key),
        ).fetchone()
        _existing_job = existing["resolved_job_id"]
        _existing_effect = existing["run_effect"]
        if _existing_job is not None and _existing_effect is not None:
            if _existing_effect not in _RUN_EFFECTS:
                raise ObservationEffectIntegrityError(
                    "observation effect already bound inconsistently"
                )
            return {
                "observation_id": existing["id"],
                "job_id": _existing_job,
                "resolved_job_id": _existing_job,
                "run_effect": _existing_effect,
                "decision": "IDEMPOTENT",
                "idempotent": True,
            }
        if (_existing_job is None) != (_existing_effect is None):
            raise ObservationEffectIntegrityError(
                "observation effect already bound inconsistently"
            )
        return {
            "observation_id": existing["id"],
            "job_id": None,
            "decision": "IDEMPOTENT",
            "idempotent": True,
        }

    normalized = normalize_observation(observation, observed_at=observed_at)

    # 01 §33.1 company resolution runs on normalized evidence plus the §32
    # origin result; a bare name is never a merge key.
    from jobscraper.pipeline.companies import resolve_company, signals_from

    company = resolve_company(
        conn,
        signals=signals_from(
            normalized=normalized,
            origin=origin,
            application_url=observation.application_url_candidate or observation.raw_url,
        ),
        observation_id=observation_id,
        observed_at=observed_at,
        now=now,
    )
    conn.execute(
        "UPDATE job_observations SET parse_evidence_ref = ? WHERE id = ?",
        (normalized.content_hash, observation_id),
    )
    if normalized.content_cleaning_version is not None:
        # S2.3 (01 §34): the deterministic cleaning outcome is durable,
        # append-only evidence on the observation that produced it — version,
        # shape summary and the hash of the stored text.  The raw text is
        # never retained here (review can re-derive from the observation's
        # own retained payload when present).
        conn.execute(
            "INSERT INTO acquisition_evidence (id, request_id, attempt_id,"
            " observation_id, kind, ref, detail_json, content_hash,"
            " observed_at, created_at) VALUES (?, ?, ?, ?, 'CONTENT_CLEANING',"
            " ?, ?, ?, ?, ?)",
            (
                new_id("ev"),
                request_id,
                attempt_id,
                observation_id,
                normalized.content_cleaning_version,
                bounded_json(
                    {
                        "description_text_chars": len(normalized.description_text or ""),
                        "description_md_chars": len(normalized.description_md or ""),
                        "language": normalized.description_lang,
                        "description_hash": normalized.description_hash,
                    }
                ),
                normalized.description_hash,
                observed_at,
                now,
            ),
        )
    for evidence in observation.field_evidence:
        stored_excerpt, excerpt_hash = excerpt_and_hash(evidence.excerpt)
        conn.execute(
            """
            INSERT INTO field_evidence (
                id, observation_id, field_name, locator_kind, locator_value,
                value_hash, excerpt, created_at, evidence_start, evidence_end,
                source_url, excerpt_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("fe"),
                observation_id,
                evidence.field_name,
                evidence.locator_kind,
                evidence.locator_value,
                evidence.value_hash,
                stored_excerpt,
                now,
                evidence.evidence_start,
                evidence.evidence_end,
                evidence.source_url,
                # hashed over what is *stored*, so a reader can verify the
                # excerpt in this row instead of chasing a dropped remainder
                excerpt_hash,
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
        origin=origin,
    )

    company_filled = False
    if resolution.decision in ("CREATED", "SPLIT_REUSE"):
        job_id = _create_canonical_job(
            conn,
            normalized=normalized,
            observed_at=observed_at,
            now=now,
            company_id=company.company_id,
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
            _resolution_event_stage(resolution),
            resolution.decision,
            None,
            resolution.reason_code or ("REUSE_GUARD" if resolution.guard_fired else None),
            json.dumps(resolution.guard_evidence, sort_keys=True, default=str),
            now,
        ),
    )

    if origin is not None:
        # 02 §32: the resolution is durable evidence attached to the
        # observation it was derived from; it never rewrites that
        # observation's own recorded URLs.
        conn.execute(
            "INSERT INTO acquisition_evidence (id, request_id, attempt_id,"
            " observation_id, kind, ref, detail_json, content_hash,"
            " observed_at, created_at) VALUES (?, ?, ?, ?, 'ORIGIN_RESOLUTION',"
            " ?, ?, ?, ?, ?)",
            (
                new_id("ev"),
                request_id,
                attempt_id,
                observation_id,
                origin.origin_url,
                bounded_json(origin.as_dict()),
                None,
                observed_at,
                now,
            ),
        )

    presence_result = _upsert_presence(
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
        origin=origin,
        content_kind=content_kind,
        strategy=strategy,
        same_host_as_source=same_host_as_source,
        source_family=source_family,
    )
    presence_id = presence_result.presence_id

    if presence_result.accepted:
        if company.company_id:
            # a company identified later (or a job created before company
            # resolution existed) is filled in once, never re-pointed
            cur = conn.execute(
                "UPDATE jobs SET company_id = ?, updated_at = ?"
                " WHERE id = ? AND company_id IS NULL",
                (company.company_id, now, job_id),
            )
            company_filled = cur.rowcount == 1

    # A5.6: classify the durable semantic run effect from the authoritative
    # seams. The effect is bound exactly once inside this fence.
    if resolution.decision in ("CREATED", "SPLIT_REUSE"):
        run_effect = "NEW_JOB"
        if presence_result.accepted:
            # RUN-21: only forward-evidence observations re-project canonical
            # state; a stale observation stays immutable history.
            refresh_canonical_presentation(
                conn,
                job_id,
                now=now,
                normalized=normalized,
                fresh_presence_id=presence_id,
            )
    elif not presence_result.accepted:
        run_effect = "STALE_IGNORED"
    else:
        canonical_result = refresh_canonical_presentation(
            conn,
            job_id,
            now=now,
            normalized=normalized,
            fresh_presence_id=presence_id,
        )
        if (
            company_filled
            or presence_result.semantic_changed
            or canonical_result.projection_changed
            or canonical_result.evaluation_changed
        ):
            run_effect = "UPDATED_JOB"
        else:
            run_effect = "UNCHANGED_JOB"

    _bind_observation_effect(
        conn,
        observation_id=observation_id,
        job_id=job_id,
        run_effect=run_effect,
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
        "resolved_job_id": job_id,
        "run_effect": run_effect,
        "decision": resolution.decision,
        "idempotent": False,
    }


def _create_canonical_job(
    conn: sqlite3.Connection,
    *,
    normalized,
    observed_at: str,
    now: str,
    company_id: str | None = None,
) -> str:
    from jobscraper.pipeline.companies import COMPANY_RESOLUTION_VERSION
    from jobscraper.pipeline.locations import (
        LOCATION_RULES_VERSION,
        project_job_locations,
    )
    from jobscraper.pipeline.provenance import PROVENANCE_SELECTOR_VERSION

    job_id = new_id("job")
    conn.execute(
        """
        INSERT INTO jobs (
            id, company_id, title, normalized_title, description_md,
            description_text, description_lang, description_hash,
            employment_type, experience_level, remote_mode, remote_worldwide,
            location_rules_version, company_resolution_version,
            provenance_selector_version,
            salary_original_text, salary_min, salary_max, salary_currency,
            salary_period, posted_at, discovered_at, first_seen_at, last_seen_at,
            last_verified_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 ?, ?, ?, ?, ?)
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
            normalized.employment_type,
            normalized.experience_level,
            normalized.remote_mode,
            normalized.remote_worldwide,
            LOCATION_RULES_VERSION,
            COMPANY_RESOLUTION_VERSION,
            PROVENANCE_SELECTOR_VERSION,
            normalized.salary_original_text,
            normalized.salary_min,
            normalized.salary_max,
            normalized.salary_currency,
            normalized.salary_period,
            normalized.posted_at,
            observed_at,
            observed_at,
            observed_at,
            observed_at,
            now,
            now,
        ),
    )
    project_job_locations(
        conn, job_id=job_id, records=normalized.location_records
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
    origin: OriginResolution | None = None,
    content_kind: str | None = None,
    strategy: str | None = None,
    same_host_as_source: bool | None = None,
    source_family: str | None = None,
) -> PresenceUpsertResult:
    from jobscraper.pipeline.availability import (
        ACTIVE_OBSERVATION,
        apply_presence_evidence,
    )

    if existing is None:
        presence_id = new_id("js")
        conn.execute(
            """
            INSERT INTO job_sources (
                id, job_id, source_id, binding_id, source_job_id,
                source_identity_generation, discovery_url, raw_source_url,
                canonical_job_url, application_url, origin_url, first_seen_at,
                last_seen_at, last_verified_at, presence_state, content_revision,
                last_observation_id, origin_provider, origin_board, origin_job_id,
                origin_resolution_confidence, origin_resolution_evidence_json,
                origin_resolved_at, source_quality_class, content_kind,
                same_host_as_source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'ACTIVE', 1,
                    ?, ?, ?, ?, ?, COALESCE(?, '{}'), ?, ?, ?, ?, ?, ?)
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
                *_origin_fields(origin, resolved_at=observed_at),
                *_quality_fields(
                    origin,
                    content_kind=content_kind,
                    strategy=strategy,
                    same_host_as_source=same_host_as_source,
                    source_family=source_family,
                ),
                now,
                now,
            ),
        )
        # v20 current-evidence coordinates are written only after entity
        # resolution has established the canonical job/presence identity.
        conn.execute(
            """
            UPDATE job_sources
               SET availability_effective_at = ?,
                   availability_received_at = ?,
                   availability_evidence_kind = ?,
                   availability_evidence_ref = ?,
                   availability_revision = 1
             WHERE id = ?
            """,
            (
                observed_at,
                now,
                ACTIVE_OBSERVATION,
                f"observation:{observation_id}",
                presence_id,
            ),
        )
        return PresenceUpsertResult(
            presence_id=presence_id, accepted=True, created=True, semantic_changed=True
        )

    # RUN-14A/RUN-21: make the availability comparator the single freshness
    # gate for mutable per-source projection.  An old worker may still append
    # its immutable JobObservation, but it cannot rewrite current presence or
    # canonical presentation merely because it finished later.
    evidence = apply_presence_evidence(
        conn,
        presence_id=str(existing["id"]),
        evidence_kind=ACTIVE_OBSERVATION,
        effective_at=observed_at,
        received_at=now,
        evidence_ref=f"observation:{observation_id}",
    )
    if not evidence.accepted:
        return PresenceUpsertResult(
            presence_id=str(existing["id"]),
            accepted=False,
            created=False,
            semantic_changed=False,
        )

    # A5.2 (kind-aware content): one presence row may be fed by several
    # observation kinds (e.g. provider LIST_FETCH listings plus DETAIL_FETCH
    # detail views of the same native job). Comparing a listing view against
    # an intervening detail view would report a semantic change on every
    # run forever, even when neither view ever changes. The honest
    # comparison for "did this source's content change" is against the
    # latest prior observation of the SAME acquisition kind; only when no
    # same-kind prior exists (first detail view ever) is the overall latest
    # observation the reference. Single-kind sources are unaffected: the
    # same-kind prior is exactly the latest observation.
    previous_hash = None
    _kind_prior_found = False
    if observation.source_job_id:
        _req_row = conn.execute(
            "SELECT request_type FROM scrape_requests"
            " WHERE id = (SELECT request_id FROM job_observations WHERE id = ?)",
            (observation_id,),
        ).fetchone()
        if _req_row is not None:
            _kind_row = conn.execute(
                "SELECT o.parse_evidence_ref FROM job_observations o"
                " JOIN scrape_requests r ON r.id = o.request_id"
                " WHERE o.source_id = ? AND o.source_job_id = ?"
                " AND o.id != ? AND r.request_type = ?"
                " AND o.resolved_job_id = ?"
                " AND o.run_effect IN ('NEW_JOB', 'UPDATED_JOB', 'UNCHANGED_JOB')"
                " ORDER BY o.rowid DESC LIMIT 1",
                (source_id, observation.source_job_id, observation_id, _req_row["request_type"], job_id),
            ).fetchone()
            if _kind_row is not None:
                _kind_prior_found = True
                previous_hash = _kind_row["parse_evidence_ref"]
    if not _kind_prior_found and existing["last_observation_id"]:
        row = conn.execute(
            "SELECT parse_evidence_ref FROM job_observations WHERE id = ?",
            (existing["last_observation_id"],),
        ).fetchone()
        previous_hash = row["parse_evidence_ref"] if row else None
    content_changed = previous_hash != normalized.content_hash

    # A5.2: semantic change from the actual persisted post-COALESCE values,
    # never from raw candidate inequality. A missing (None) candidate retains
    # the stored value and is not a change.
    _o_fields = _origin_fields(origin, resolved_at=observed_at)
    _q_fields = _quality_fields(
        origin,
        content_kind=content_kind,
        strategy=strategy,
        same_host_as_source=same_host_as_source,
        source_family=source_family,
        existing=existing,
    )
    _canon_candidate = observation.canonical_url_candidate
    _apply_candidate = observation.application_url_candidate
    _new_canonical = (
        _canon_candidate if _canon_candidate is not None else existing["canonical_job_url"]
    )
    _new_application = (
        _apply_candidate if _apply_candidate is not None else existing["application_url"]
    )
    _o_names = (
        "origin_provider",
        "origin_board",
        "origin_job_id",
        "origin_resolution_confidence",
        "origin_resolution_evidence_json",
        "origin_resolved_at",
    )
    _new_origin = tuple(
        cand if cand is not None else existing[name]
        for cand, name in zip(_o_fields, _o_names)
    )
    _new_origin_by_name = dict(zip(_o_names, _new_origin))
    # origin_resolved_at is a verification timestamp (it advances on every
    # reverification like last_seen_at) and never alone signals a semantic
    # change; identity/confidence changes are caught by their own fields.
    # The evidence JSON embeds per-run resolved_at/observed_at stamps, so it
    # is compared by substance with those volatile timestamps removed.
    _origin_identity_changed = any(
        (new_v or None) != (existing[name] or None)
        if isinstance(existing[name], str) or isinstance(new_v, str)
        else new_v != existing[name]
        for name in (
            "origin_provider",
            "origin_board",
            "origin_job_id",
            "origin_resolution_confidence",
        )
        for new_v in (_new_origin_by_name[name],)
    )
    _origin_evidence_changed = _origin_evidence_substance(
        _new_origin_by_name["origin_resolution_evidence_json"]
    ) != _origin_evidence_substance(existing["origin_resolution_evidence_json"])
    _q_names = ("source_quality_class", "content_kind", "same_host_as_source")
    _new_quality = tuple(
        cand if cand is not None else existing[name]
        for cand, name in zip(_q_fields, _q_names)
    )
    semantic_changed = bool(
        content_changed
        or evidence.state_changed
        or (_new_canonical or None) != (existing["canonical_job_url"] or None)
        or (_new_application or None) != (existing["application_url"] or None)
        or _origin_identity_changed
        or _origin_evidence_changed
        or any(new_v != existing[name] for new_v, name in zip(_new_quality, _q_names))
        or (binding_id or None) != (existing["binding_id"] or None)
    )

    # Tightened: record APPLY_URL_CHANGED only when the persisted application
    # URL actually changes; a missing candidate that retains the old URL is
    # not a change and must not invent history.
    if (_new_application or None) != (existing["application_url"] or None):
        record_change(conn, job_id, "APPLY_URL_CHANGED", now)

    conn.execute(
        """
        UPDATE job_sources SET
            binding_id = ?, last_seen_at = ?, last_verified_at = ?,
            origin_provider = COALESCE(?, origin_provider),
            origin_board = COALESCE(?, origin_board),
            origin_job_id = COALESCE(?, origin_job_id),
            origin_resolution_confidence = COALESCE(?, origin_resolution_confidence),
            origin_resolution_evidence_json = COALESCE(?, origin_resolution_evidence_json),
            origin_resolved_at = COALESCE(?, origin_resolved_at),
            source_quality_class = COALESCE(?, source_quality_class),
            content_kind = COALESCE(?, content_kind),
            same_host_as_source = COALESCE(?, same_host_as_source),
            canonical_job_url = COALESCE(?, canonical_job_url),
            application_url = COALESCE(?, application_url),
            content_revision = CASE WHEN ? THEN content_revision + 1
                                    ELSE content_revision END,
            last_observation_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            binding_id,
            observed_at,
            observed_at,
            *_o_fields,
            *_q_fields,
            observation.canonical_url_candidate,
            observation.application_url_candidate,
            content_changed,
            observation_id,
            now,
            existing["id"],
        ),
    )
    return PresenceUpsertResult(
        presence_id=str(existing["id"]),
        accepted=True,
        created=False,
        semantic_changed=semantic_changed,
    )

def _quality_fields(
    origin: OriginResolution | None,
    *,
    content_kind: str | None,
    strategy: str | None,
    same_host_as_source: bool | None,
    source_family: str | None,
    existing: sqlite3.Row | None = None,
) -> tuple:
    """Classify from the effective retained origin and current fetch evidence."""
    from jobscraper.pipeline.provenance import classify_source_quality

    status = getattr(origin, "status", None)
    provider = getattr(origin, "origin_provider", None)
    origin_on_source_host = getattr(origin, "same_host_as_source", None)
    if existing is not None and status is not OriginStatus.RESOLVED:
        # An unresolved sighting supplies no replacement origin authority.
        # Use the same resolved state that the presence UPDATE retains.
        if existing["origin_provider"]:
            status = OriginStatus.RESOLVED
            provider = existing["origin_provider"]
            retained = json.loads(existing["origin_resolution_evidence_json"])
            origin_on_source_host = retained.get("same_host_as_source")
        if same_host_as_source is None:
            same_host_as_source = existing["same_host_as_source"]
    if content_kind is None and existing is not None:
        content_kind = existing["content_kind"]
    same_host = (
        same_host_as_source if same_host_as_source is not None
        else origin_on_source_host
    )
    quality = classify_source_quality(
        strategy=strategy,
        execution_class=None,
        content_kind=content_kind,
        same_host_as_source=bool(same_host),
        source_family=source_family,
        origin_on_source_host=origin_on_source_host,
        origin_status=status.value if status is not None else None,
        origin_provider=provider,
    )
    return (quality, content_kind, 1 if same_host else 0 if same_host is not None else None)


def _origin_fields(origin: OriginResolution | None, *, resolved_at: str) -> tuple:
    """Presence-level origin columns (02 §32).

    An unresolved resolution writes NULLs: nothing is guessed, and an existing
    resolved origin is never erased by a later unresolved sighting (COALESCE in
    the UPDATE).  The resolved URL itself stays inside the evidence rows —
    ``job_sources.origin_url`` remains reserved for canonical URL selection
    (03 §39), so the resolver never overloads it.
    """
    if origin is None or origin.status is not OriginStatus.RESOLVED:
        # UPDATE retains resolved evidence; only INSERT supplies the schema
        # default for a presence with no previously resolved origin.
        return (None, None, None, None, None, None)
    return (
        origin.origin_provider,
        origin.origin_board,
        origin.origin_job_id,
        origin.confidence,
        json.dumps(origin.as_dict(), sort_keys=True, default=str),
        origin.resolved_at or resolved_at,
    )


__all__ = [
    "ObservationEffectIntegrityError",
    "PresenceUpsertResult",
    "ingest_observation",
    "observation_key",
]
