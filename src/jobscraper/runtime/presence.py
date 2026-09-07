"""Mutable source presence (job_sources) with evidence ordering.

Authority: RUN-11 (job_sources semantics), RUN-13/14/14A (revalidation state,
multi-source availability resolution, temporal precedence), section 40.

Immutable observations and mutable presence remain separate. Current
projections are evidence-ordered: an older observation cannot overwrite a
newer projection because it completed later (conditional update on
evidence_order).
"""

from __future__ import annotations

import json
import secrets

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import utc_now_s

EVIDENCE_ORDER_VERSION = 1


def _evidence_order(observed_at: str, source_rank: int) -> str:
    return f"{observed_at}|{EVIDENCE_ORDER_VERSION:02d}|{source_rank:06d}"


def upsert_presence(
    db: Database,
    *,
    job_id: str,
    source_id: str,
    binding_id: str | None,
    source_job_id: str | None,
    observed_at: str,
    discovery_url: str | None = None,
    raw_source_url: str | None = None,
    canonical_job_url: str | None = None,
    application_url: str | None = None,
    origin: dict | None = None,
    content_revision: int = 1,
    source_rank: int = 100,
    observation_id: str | None = None,
    source_identity_generation: int = 1,
    now: str | None = None,
) -> str:
    """Create or update the per-source presence record.

    ``job_sources.job_id`` is never required before canonical identity exists
    (RUN-11): callers resolve identity first. Updates are conditional on
    evidence order so a late-finishing older observation cannot regress a
    newer projection.
    """
    now = now or utc_now_s()
    order = _evidence_order(observed_at, source_rank)
    existing = db.query_one(
        "SELECT id, evidence_order, content_revision FROM job_sources"
        " WHERE job_id=? AND source_id=? AND ifnull(source_job_id,'')=? AND source_identity_generation=?",
        (job_id, source_id, source_job_id or "", source_identity_generation),
    )
    origin = origin or {}
    if existing is None:
        presence_id = "js-" + secrets.token_hex(10)
        with immediate_transaction(db.conn) as tx:
            tx.execute(
                "INSERT INTO job_sources(id, job_id, source_id, binding_id, source_job_id,"
                " discovery_url, raw_source_url, canonical_job_url, application_url, origin_url,"
                " origin_provider, origin_board, origin_job_id, origin_resolution_confidence,"
                " origin_resolved_at, first_seen_at, last_seen_at, last_verified_at,"
                " presence_state, content_revision, evidence_order, last_observation_id,"
                " source_rank, source_identity_generation, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (presence_id, job_id, source_id, binding_id, source_job_id,
                 discovery_url, raw_source_url, canonical_job_url, application_url,
                 origin.get("origin_url"), origin.get("origin_provider"), origin.get("origin_board"),
                 origin.get("origin_job_id"), origin.get("confidence"), now,
                 observed_at, observed_at, observed_at, "ACTIVE", content_revision, order,
                 observation_id, source_rank, source_identity_generation, now, now),
            )
        return presence_id

    # Conditional update: only newer evidence rewrites current projection.
    if (existing["evidence_order"] or "") >= order:
        # Older/equal evidence: verify-only advance when still ACTIVE and newer
        # than last_verified_at.
        with immediate_transaction(db.conn) as tx:
            tx.execute(
                "UPDATE job_sources SET last_verified_at=MAX(COALESCE(last_verified_at,''), ?),"
                " last_observation_id=COALESCE(?, last_observation_id), updated_at=?"
                " WHERE id=? AND (last_verified_at IS NULL OR last_verified_at < ?)",
                (observed_at, observation_id, now, existing["id"], observed_at),
            )
        return existing["id"]

    changed = existing["content_revision"] != content_revision
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "UPDATE job_sources SET binding_id=COALESCE(?, binding_id),"
            " discovery_url=COALESCE(discovery_url, ?), raw_source_url=COALESCE(?, raw_source_url),"
            " canonical_job_url=COALESCE(?, canonical_job_url), application_url=COALESCE(?, application_url),"
            " origin_url=COALESCE(origin_url, ?), origin_provider=COALESCE(?, origin_provider),"
            " origin_board=COALESCE(?, origin_board), origin_job_id=COALESCE(?, origin_job_id),"
            " last_seen_at=?, last_verified_at=?, presence_state='ACTIVE',"
            " content_revision=?, evidence_order=?, last_observation_id=COALESCE(?, last_observation_id),"
            " source_rank=?, updated_at=? WHERE id=?",
            (binding_id, discovery_url, raw_source_url, canonical_job_url, application_url,
             origin.get("origin_url"), origin.get("origin_provider"), origin.get("origin_board"),
             origin.get("origin_job_id"), observed_at, observed_at, content_revision, order,
             observation_id, source_rank, now, existing["id"]),
        )
    if changed:
        record_job_history(
            db,
            job_id=job_id,
            change_class="CONTENT_CHANGED",
            detail={"source_id": source_id, "from_revision": existing["content_revision"], "to_revision": content_revision},
            evidence_observation_id=observation_id,
            source_id=source_id,
        )
    return existing["id"]


def record_job_history(
    db: Database,
    *,
    job_id: str,
    change_class: str,
    detail: dict,
    evidence_observation_id: str | None = None,
    source_id: str | None = None,
    at: str | None = None,
) -> str:
    now = at or utc_now_s()
    history_id = "jh-" + secrets.token_hex(10)
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO job_history(id, job_id, at, change_class, detail_json,"
            " evidence_observation_id, source_id) VALUES (?,?,?,?,?,?,?)",
            (history_id, job_id, now, change_class, json.dumps(detail, sort_keys=True),
             evidence_observation_id, source_id),
        )
    return history_id


# ------------------------------------------------------- absence application
def apply_absence_coverage(db: Database, coverage_row, *, now: str | None = None) -> int:
    """Apply one COMPLETE absence-authoritative coverage generation exactly once.

    Jobs whose presence belongs to the same scope_key and whose stable source
    identity is absent from the seen set transition ACTIVE -> UNCERTAIN
    (missing from one complete enumeration = uncertainty, not closure).
    Repeated application is idempotent (zero additional transitions).
    """
    now = now or utc_now_s()
    coverage_id = coverage_row["id"]
    if coverage_row["applied_at"]:
        return 0
    if coverage_row["completion_state"] != "COMPLETE":
        return 0
    if coverage_row["coverage_authority"] not in ("AUTHORITATIVE_FULL_SOURCE", "AUTHORITATIVE_DECLARED_SCOPE"):
        return 0
    if not coverage_row["absence_inference_allowed"]:
        return 0

    scope_key = coverage_row["scope_key"]
    seen = {
        row["stable_source_identity"]
        for row in db.query(
            "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id=?",
            (coverage_id,),
        )
    }
    source_id = coverage_row["source_id"]
    transitions = 0
    with immediate_transaction(db.conn) as tx:
        # Mark applied first (exactly-once).
        cur = tx.execute(
            "UPDATE enumeration_coverage SET applied_at=? WHERE id=? AND applied_at IS NULL",
            (now, coverage_id),
        )
        if cur.rowcount != 1:
            return 0
        # Members of the same declared scope missing from the seen union.
        rows = tx.execute(
            "SELECT js.id, js.job_id, js.source_job_id, js.presence_state, js.last_absence_coverage_id,"
            " js.content_revision FROM job_sources js WHERE js.source_id=? AND js.presence_state='ACTIVE'"
            " AND (js.last_authoritative_scope_key=? OR js.last_authoritative_scope_key IS NULL)",
            (source_id, scope_key),
        ).fetchall()
        for row in rows:
            identity = f"{source_id}:{row['source_job_id'] or ''}"
            if identity in seen:
                continue
            tx.execute(
                "UPDATE job_sources SET presence_state='UNCERTAIN', last_absence_coverage_id=?,"
                " updated_at=? WHERE id=? AND presence_state='ACTIVE'",
                (coverage_id, now, row["id"]),
            )
            tx.execute(
                "INSERT INTO job_history(id, job_id, at, change_class, detail_json, source_id)"
                " VALUES (?,?,?,?,?,?)",
                ("jh-" + secrets.token_hex(10), row["job_id"], now, "UNKNOWN_CHANGE",
                 json.dumps({"change": "absence_uncertain", "coverage_id": coverage_id}), source_id),
            )
            transitions += 1
    return transitions


def presence_for_job(db: Database, job_id: str) -> list:
    return db.query("SELECT * FROM job_sources WHERE job_id=? ORDER BY source_rank", (job_id,))


def derive_listing_status(db: Database, job_id: str, *, now: str | None = None) -> str:
    """RUN-14: canonical listing status from current source-presence evidence.

    An aggregator disappearance cannot close a job that a trusted employer/ATS
    source still reports active. Explicit trusted close evidence wins; else
    any sufficiently fresh ACTIVE presence keeps the job ACTIVE; all-absent
    presences degrade to UNCERTAIN/EXPIRED per policy.
    """
    now = now or utc_now_s()
    presences = presence_for_job(db, job_id)
    if not presences:
        return "UNCERTAIN"
    if any(p["presence_state"] == "CLOSED" and _is_trusted(p) for p in presences):
        return "CLOSED"
    if any(p["presence_state"] == "ACTIVE" for p in presences):
        return "ACTIVE"
    if all(p["presence_state"] == "CLOSED" for p in presences):
        return "CLOSED"
    if all(p["presence_state"] in ("UNCERTAIN", "EXPIRED") for p in presences):
        # Repeated absence under policy -> EXPIRED candidate.
        stale_days = 30
        cutoff = _days_ago(now, stale_days)
        if all((p["last_seen_at"] or "") < cutoff for p in presences):
            return "EXPIRED"
        return "UNCERTAIN"
    if any(p["presence_state"] == "WITHDRAWN" for p in presences) and not any(
        p["presence_state"] == "ACTIVE" for p in presences
    ):
        return "WITHDRAWN"
    return "UNCERTAIN"


def _is_trusted(presence_row) -> bool:
    # Employer ATS/API presences (the pipeline ranks provider-native feeds at
    # 10) are trusted; aggregator/secondary presences (50+) are not — an
    # aggregator's CLOSED must not close a job its employer still lists.
    return (presence_row["source_rank"] or 100) < 50


def _days_ago(now: str, days: int) -> str:
    from jobscraper.timeutil import add_seconds

    return add_seconds(now, -days * 86400)


def refresh_listing_status(db: Database, job_id: str, *, now: str | None = None) -> str:
    status = derive_listing_status(db, job_id, now=now)
    with immediate_transaction(db.conn) as tx:
        tx.execute("UPDATE jobs SET listing_status=?, updated_at=? WHERE id=?", (status, now or utc_now_s(), job_id))
    return status
