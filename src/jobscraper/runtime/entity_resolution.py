"""Entity resolution: observation -> canonical job identity.

Authority: module 01 section 38 (staged reversible dedup), RUN-11/12/15,
PROD-03 (source-native ID reuse guard).

Ordering (RUN-21): conflicting canonicalization uses compare-and-set on the
canonical/entity revision; an older observation cannot overwrite a newer
projection because it completed later.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.normalize.company import resolve_company
from jobscraper.timeutil import utc_now_s

RESOLVER_VERSION = "entity-resolver-v1"
NORMALIZATION_VERSION = "normalize-v1"

# Time window for stage-4 content similarity merges.
_STAGE4_WINDOW_DAYS = 90


@dataclass
class ResolutionDecision:
    decision: str  # CREATE | SELECT | SPLIT | REVIEW_REQUIRED
    job_id: str | None
    stage: str
    evidence: dict = field(default_factory=dict)


def _new_job_id() -> str:
    return "job-" + secrets.token_hex(10)


def source_identity_generation(db: Database, source_id: str, source_job_id: str) -> int:
    """Current source-identity generation for a native ID (reuse guard)."""
    row = db.query_one(
        "SELECT MAX(source_identity_generation) g FROM job_sources"
        " WHERE source_id=? AND source_job_id=?",
        (source_id, source_job_id),
    )
    return int(row["g"] or 0)


def detect_source_id_reuse(
    db: Database,
    *,
    source_id: str,
    source_job_id: str,
    normalized: dict,
) -> dict | None:
    """PROD-03 / RUN-15: detect incompatible reuse of a source-native ID.

    Returns reuse evidence when the same (source, native id) was seen with a
    materially incompatible title/company across a closed interval.
    """
    rows = db.query(
        "SELECT js.job_id, js.presence_state, js.first_seen_at, js.last_seen_at, j.title,"
        " j.company_id, c.normalized_name"
        " FROM job_sources js JOIN jobs j ON j.id = js.job_id"
        " LEFT JOIN companies c ON c.id = j.company_id"
        " WHERE js.source_id=? AND js.source_job_id=?"
        " ORDER BY js.last_seen_at DESC LIMIT 5",
        (source_id, source_job_id),
    )
    if not rows:
        return None
    existing = rows[0]
    new_title = (normalized.get("title") or "").lower().strip()
    old_title = (existing["title"] or "").lower().strip()
    new_company = (normalized.get("company_normalized") or "").lower()
    old_company = (existing["normalized_name"] or "").lower()
    same_company = bool(new_company and old_company and new_company == old_company)
    similar_title = bool(new_title and old_title and (
        new_title == old_title
        or _token_jaccard(new_title, old_title) >= 0.5
    ))
    closed = existing["presence_state"] in ("CLOSED", "EXPIRED", "WITHDRAWN")
    if closed and not (same_company and similar_title):
        return {
            "kind": "source_id_reuse",
            "existing_job_id": existing["job_id"],
            "old_title": existing["title"],
            "new_title": normalized.get("title"),
            "old_company": old_company,
            "new_company": new_company,
            "closed_interval": [existing["first_seen_at"], existing["last_seen_at"]],
        }
    if not similar_title and not same_company:
        return {
            "kind": "entity_mismatch",
            "existing_job_id": existing["job_id"],
            "old_title": existing["title"],
            "new_title": normalized.get("title"),
        }
    return None


def _token_jaccard(a: str, b: str) -> float:
    ta = {t for t in a.split() if len(t) > 2}
    tb = {t for t in b.split() if len(t) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def resolve_observation(
    db: Database,
    *,
    observation_row,
    normalized: dict,
    company_id: str | None = None,
    auto_merge: bool = True,
) -> ResolutionDecision:
    """Create-or-select the canonical Job identity for one observation.

    Stages (module 01 section 38):
      1. same (source_id, source_job_id) — unless reuse guard fires (SPLIT);
      2. same (origin_provider, origin_board, origin_job_id) with guard;
      3. same job-specific canonical application URL;
      4. normalized company + title + compatible window + strong similarity;
      5. company/title with location disagreement -> cluster only;
      6. ambiguous -> separate job (never a risky automatic merge).
    """
    source_id = observation_row["source_id"]
    source_job_id = observation_row["source_job_id"]
    now = utc_now_s()

    if source_job_id:
        reuse = detect_source_id_reuse(db, source_id=source_id, source_job_id=source_job_id, normalized=normalized)
        if reuse and reuse.get("kind") == "source_id_reuse":
            # SPLIT: new canonical identity under a new generation.
            job_id = _create_job(db, normalized, company_id, now, evidence_at=_observed_at(observation_row, now))
            return ResolutionDecision("SPLIT", job_id, "stage1_reuse_guard", reuse)
        row = db.query_one(
            "SELECT job_id, source_identity_generation FROM job_sources"
            " WHERE source_id=? AND source_job_id=? ORDER BY source_identity_generation DESC LIMIT 1",
            (source_id, source_job_id),
        )
        if row is not None:
            return ResolutionDecision(
                "SELECT", row["job_id"], "stage1_source_native_id",
                {"source_job_id": source_job_id, "generation": row["source_identity_generation"]},
            )

    canonical_url = normalized.get("canonical_url")
    if canonical_url:
        row = db.query_one(
            "SELECT job_id FROM job_sources WHERE canonical_job_url=? AND job_id IS NOT NULL LIMIT 1",
            (canonical_url,),
        )
        if row is not None:
            return ResolutionDecision("SELECT", row["job_id"], "stage3_canonical_url", {"url": canonical_url})

    # Stage 4: company + title + time window + content similarity.
    stage4_company = company_id or normalized.get("company_id")
    if auto_merge and stage4_company:
        candidates = db.query(
            "SELECT j.id, j.title, j.first_seen_at, c.normalized_name FROM jobs j"
            " JOIN companies c ON c.id = j.company_id"
            " WHERE j.company_id=? AND j.listing_status != 'CLOSED' AND j.first_seen_at >= ?"
            " LIMIT 25",
            (stage4_company, _days_ago(now, _STAGE4_WINDOW_DAYS)),
        )
        new_title = (normalized.get("title") or "").lower()
        for cand in candidates:
            if _token_jaccard(new_title, (cand["title"] or "").lower()) >= 0.6:
                return ResolutionDecision(
                    "SELECT", cand["id"], "stage4_content_similarity",
                    {"similar_to": cand["id"], "title": cand["title"]},
                )

    job_id = _create_job(db, normalized, company_id, now, evidence_at=_observed_at(observation_row, now))
    return ResolutionDecision("CREATE", job_id, "new_identity", {})


def _observed_at(observation_row, default: str) -> str:
    try:
        keys = observation_row.keys()
    except AttributeError:  # pragma: no cover - defensive
        return default
    if "observed_at" in keys:
        return observation_row["observed_at"] or default
    return default


def _days_ago(now: str, days: int) -> str:
    from jobscraper.timeutil import add_seconds

    return add_seconds(now, -days * 86400)


def _create_job(db: Database, normalized: dict, company_id: str | None, now: str, *, evidence_at: str | None = None) -> str:
    job_id = _new_job_id()
    # The projection's evidence time: when the creating observation was made
    # (RUN-21 ordering anchor for the canonical projection).
    projection_evidence_at = evidence_at or now
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO jobs(id, company_id, title, normalized_title, description_md,"
            " description_text, description_lang, description_hash, employment_type,"
            " experience_level, remote_mode, remote_worldwide, salary_original_text, salary_min,"
            " salary_max, salary_currency, salary_period, salary_annual_min_ref, salary_annual_max_ref,"
            " salary_ref_currency, salary_confidence, posted_at, discovered_at, first_seen_at,"
            " last_seen_at, listing_status, fingerprint, projection_evidence_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, company_id, normalized.get("title") or "Untitled",
                (normalized.get("title") or "untitled").lower(),
                normalized.get("description_md"), normalized.get("description_text"),
                normalized.get("description_lang"), normalized.get("description_hash"),
                normalized.get("employment_type"), normalized.get("experience_level"),
                normalized.get("remote_mode"), 1 if normalized.get("remote_worldwide") else 0,
                normalized.get("salary_original_text"), normalized.get("salary_min"),
                normalized.get("salary_max"), normalized.get("salary_currency"),
                normalized.get("salary_period"), normalized.get("salary_annual_min"),
                normalized.get("salary_annual_max"), "USD", normalized.get("salary_confidence"),
                normalized.get("posted_at"), now, now, now, "ACTIVE", normalized.get("fingerprint"),
                projection_evidence_at, now, now,
            ),
        )
    return job_id


# ---------------------------------------------------------------- merging
def merge_jobs(
    db: Database,
    *,
    kept_job_id: str,
    absorbed_job_id: str,
    stage: str,
    reason_code: str,
    match_score: float | None = None,
    evidence: dict | None = None,
    merged_by: str = "user",
) -> str:
    """Merge two canonical jobs reversibly (module 01 section 38).

    Dependent user state ownership is recorded so undo can deterministically
    restore it. Conflicting dispositions are never silently discarded; distinct
    application records are never collapsed.
    """
    now = utc_now_s()
    merge_id = "mrg-" + secrets.token_hex(8)
    state_ownership = _migrate_dependent_state(db, kept_job_id, absorbed_job_id, merge_id, now)
    evidence = dict(evidence or {})
    evidence["state_ownership"] = state_ownership
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO job_merges(id, kept_job_id, absorbed_job_id, stage, match_score,"
            " reason_code, evidence_json, merged_at) VALUES (?,?,?,?,?,?,?,?)",
            (merge_id, kept_job_id, absorbed_job_id, stage, match_score, reason_code,
             json.dumps(evidence, sort_keys=True), now),
        )
        tx.execute(
            "INSERT INTO job_aliases(job_id, alias_of_job_id, merge_id, created_at) VALUES (?,?,?,?)",
            (absorbed_job_id, kept_job_id, merge_id, now),
        )
        # Absorbed job becomes an alias: hidden from canonical listing but not destroyed.
        tx.execute(
            "UPDATE jobs SET listing_status='WITHDRAWN', notes_md=COALESCE(notes_md,'')"
            " || '[merged->" + kept_job_id + "]', updated_at=? WHERE id=?",
            (now, absorbed_job_id),
        )
    return merge_id


def _migrate_dependent_state(db: Database, kept: str, absorbed: str, merge_id: str, now: str) -> dict:
    """Move dependent user state to the kept job with recorded ownership."""
    ownership: dict = {"dispositions": [], "applications": [], "inbox_events": [], "notes": [], "reminders": []}
    with immediate_transaction(db.conn) as tx:
        for row in tx.execute(
            "SELECT * FROM job_profile_state WHERE job_id=?", (absorbed,)
        ).fetchall():
            existing = tx.execute(
                "SELECT disposition FROM job_profile_state WHERE job_id=? AND profile_id=?",
                (kept, row["profile_id"]),
            ).fetchone()
            if existing is None:
                tx.execute(
                    "INSERT INTO job_profile_state(job_id, profile_id, disposition, dismissed_reason,"
                    " snoozed_until, first_inbox_at, last_inbox_at, triaged_at, archived_at,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (kept, row["profile_id"], row["disposition"], row["dismissed_reason"],
                     row["snoozed_until"], row["first_inbox_at"], row["last_inbox_at"],
                     row["triaged_at"], row["archived_at"], now, now),
                )
                # The state now belongs to the kept job; remove the absorbed
                # row so undo can deterministically move it back.
                tx.execute(
                    "DELETE FROM job_profile_state WHERE job_id=? AND profile_id=?",
                    (absorbed, row["profile_id"]),
                )
                ownership["dispositions"].append(
                    {"profile_id": row["profile_id"], "from_job": absorbed, "action": "moved"}
                )
            elif existing["disposition"] != row["disposition"]:
                # Conflicting dispositions are never silently discarded.
                ownership["dispositions"].append(
                    {
                        "profile_id": row["profile_id"],
                        "from_job": absorbed,
                        "action": "conflict_preserved_kept",
                        "kept_disposition": existing["disposition"],
                        "absorbed_disposition": row["disposition"],
                    }
                )
        # Applications are re-pointed, never collapsed.
        for row in tx.execute("SELECT id FROM applications WHERE job_id=?", (absorbed,)).fetchall():
            tx.execute("UPDATE applications SET job_id=?, updated_at=? WHERE id=?", (kept, now, row["id"]))
            ownership["applications"].append({"application_id": row["id"], "from_job": absorbed})
        # Durable inbox events keep their identity; job_id re-pointed for visibility.
        for row in tx.execute(
            "SELECT id, dedupe_key FROM job_profile_inbox_events WHERE job_id=?", (absorbed,)
        ).fetchall():
            dup = tx.execute(
                "SELECT id FROM job_profile_inbox_events WHERE job_id=? AND dedupe_key=?",
                (kept, row["dedupe_key"]),
            ).fetchone()
            if dup is None:
                tx.execute(
                    "UPDATE job_profile_inbox_events SET job_id=? WHERE id=?", (kept, row["id"])
                )
                ownership["inbox_events"].append({"event_id": row["id"], "from_job": absorbed})
        for row in tx.execute("SELECT id FROM reminders WHERE job_id=?", (absorbed,)).fetchall():
            tx.execute("UPDATE reminders SET job_id=? WHERE id=?", (kept, row["id"]))
            ownership["reminders"].append({"reminder_id": row["id"], "from_job": absorbed})
    return ownership


def undo_merge(db: Database, merge_id: str, *, undone_by: str = "user") -> bool:
    """Deterministically undo a merge, restoring independent visibility and
    recorded ownership of dependent state."""
    now = utc_now_s()
    merge = db.query_one("SELECT * FROM job_merges WHERE id=?", (merge_id,))
    if merge is None or merge["undone_at"] is not None:
        return False
    evidence = json.loads(merge["evidence_json"] or "{}")
    ownership = evidence.get("state_ownership") or {}
    kept, absorbed = merge["kept_job_id"], merge["absorbed_job_id"]
    with immediate_transaction(db.conn) as tx:
        # Restore dispositions that were moved.
        for item in ownership.get("dispositions", []):
            if item.get("action") == "moved":
                tx.execute(
                    "UPDATE job_profile_state SET job_id=? WHERE job_id=? AND profile_id=?",
                    (absorbed, kept, item["profile_id"]),
                )
        # Applications return to the absorbed job (never collapsed, so exact restore).
        for item in ownership.get("applications", []):
            tx.execute(
                "UPDATE applications SET job_id=? WHERE id=? AND job_id=?",
                (absorbed, item["application_id"], kept),
            )
        for item in ownership.get("inbox_events", []):
            tx.execute(
                "UPDATE job_profile_inbox_events SET job_id=? WHERE id=?",
                (absorbed, item["event_id"]),
            )
        for item in ownership.get("reminders", []):
            tx.execute("UPDATE reminders SET job_id=? WHERE id=?", (absorbed, item["reminder_id"]))
        tx.execute("DELETE FROM job_aliases WHERE job_id=? AND alias_of_job_id=?", (absorbed, kept))
        tx.execute(
            "UPDATE jobs SET listing_status='ACTIVE', notes_md=REPLACE(notes_md,"
            " '[merged->" + kept + "]', ''), updated_at=? WHERE id=?",
            (now, absorbed),
        )
        tx.execute(
            "UPDATE job_merges SET undone_at=?, undone_by=? WHERE id=?", (now, undone_by, merge_id)
        )
    return True


def resolve_company_for_observation(db: Database, normalized: dict) -> str | None:
    if normalized.get("company"):
        return resolve_company(
            db,
            name=normalized.get("company"),
            domain=normalized.get("company_domain"),
            careers_url=normalized.get("company_careers_url"),
            ats_provider=normalized.get("origin_provider"),
            ats_board=normalized.get("origin_board"),
        )
    return None
