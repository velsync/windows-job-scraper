"""Downstream processing: observation -> canonical corpus -> Inbox.

Authority: ARC-05 (observation before canonicalization), RUN-11 (canonical
creation ordering), module 01 sections 34-42.

Processing is host-native and never performs network I/O. Obligations drain
locally even after run cancellation (accepted evidence is never orphaned).
"""

from __future__ import annotations

import json
import secrets

from jobscraper.config import AppConfig
from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.diagnostics.events import EventLog
from jobscraper.normalize import content as content_norm
from jobscraper.normalize import facts as facts_mod
from jobscraper.normalize import locations as locations_mod
from jobscraper.normalize import salary as salary_mod
from jobscraper.normalize.eligibility import evaluate_eligibility
from jobscraper.normalize.scoring import score_job
from jobscraper.runtime import obligations as obl
from jobscraper.runtime.entity_resolution import (
    NORMALIZATION_VERSION,
    resolve_company_for_observation,
    resolve_observation,
)
from jobscraper.runtime.presence import refresh_listing_status, upsert_presence
from jobscraper.timeutil import utc_now_s
from jobscraper.workflow import inbox as inbox_mod


class ProcessingPipeline:
    def __init__(self, config: AppConfig, db: Database, events: EventLog | None = None) -> None:
        self.config = config
        self.db = db
        self.events = events or EventLog(db.conn)

    def drain(self, *, run_id: str | None = None, max_iterations: int = 200) -> int:
        """Drain pending processing obligations to completion (bounded)."""
        processed = 0
        for _ in range(max_iterations):
            batch = obl.claim_pending(self.db, limit=25)
            if not batch:
                break
            for item in batch:
                ok = self._process_one(item, run_id=run_id)
                if ok:
                    obl.satisfy(self.db, item["id"], claim_token=item["claim_token"])
                    processed += 1
                else:
                    obl.fail(self.db, item["id"], claim_token=item["claim_token"])
        return processed

    # ------------------------------------------------------------------ one
    def _process_one(self, item: dict, *, run_id: str | None) -> bool:
        observation = self.db.query_one(
            "SELECT * FROM job_observations WHERE id=?", (item["observation_id"],)
        )
        if observation is None:
            return True  # nothing to do; obligation satisfied vacuously
        try:
            normalized = self._normalize(observation)
        except Exception as exc:  # normalization failure must not loop forever
            self.events.error(
                "processing.normalize_failed",
                f"normalization failed for {observation['id']}: {exc}",
                request_id=observation["request_id"],
            )
            return False
        company_id = resolve_company_for_observation(self.db, normalized)
        normalized["company_id"] = company_id

        decision = resolve_observation(
            self.db, observation_row=observation, normalized=normalized, company_id=company_id
        )
        job_id = decision.job_id
        now = utc_now_s()
        # Entity resolution event (provenance).
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "INSERT INTO entity_resolution_events(id, observation_id, job_id, decision, stage,"
                " match_evidence_json, normalization_version, resolver_version, at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                ("ere-" + secrets.token_hex(10), observation["id"], job_id, decision.decision,
                 decision.stage, json.dumps(decision.evidence, sort_keys=True), NORMALIZATION_VERSION,
                 "entity-resolver-v1", now),
            )
        # Presence BEFORE canonical projection update (RUN-11 ordering).
        binding = self.db.query_one(
            "SELECT b.id FROM source_adapter_bindings b WHERE b.id=?", (observation["binding_id"],)
        )
        source_rank = 10 if observation["strategy"] in ("PROVIDER_NATIVE", "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT") else 50
        upsert_presence(
            self.db,
            job_id=job_id,
            source_id=observation["source_id"],
            binding_id=observation["binding_id"] if binding else None,
            source_job_id=observation["source_job_id"],
            observed_at=observation["observed_at"],
            discovery_url=observation["raw_url"],
            raw_source_url=observation["raw_url"],
            canonical_job_url=observation["canonical_url_candidate"],
            application_url=observation["application_url_candidate"],
            origin=self._origin_for(observation),
            content_revision=self._content_revision(normalized),
            source_rank=source_rank,
            observation_id=observation["id"],
            now=now,
        )
        self._update_canonical_projection(job_id, normalized, observation, now)
        # Scope stamping for absence inference.
        if observation["enumeration_scope_key"]:
            self.db.execute(
                "UPDATE job_sources SET last_authoritative_scope_key=? WHERE job_id=? AND source_id=?"
                " AND last_authoritative_scope_key IS NULL",
                (observation["enumeration_scope_key"], job_id, observation["source_id"]),
            )
        refresh_listing_status(self.db, job_id, now=now)
        self._reevaluate_for_profiles(job_id, normalized, observation, now)
        return True

    # ----------------------------------------------------------- normalize
    def _normalize(self, observation) -> dict:
        c = json.loads(observation["content_json"] or "{}")
        description_html = c.get("description_html") or c.get("description_plain") or ""
        description_text = content_norm.html_to_text(description_html) if "<" in description_html else description_html
        description_md = content_norm.html_to_markdown(description_html) if "<" in description_html else description_html
        lang, lang_conf = content_norm.detect_language(description_text)
        salary = salary_mod.parse_salary(self._salary_raw_text(c))
        annual_min = annual_max = None
        if salary:
            annual_min, annual_max, _ref = salary.annualized()
        locations = locations_mod.normalize_locations(
            c.get("location_raw"),
            c.get("secondary_locations") or [],
            remote_hint=bool(c.get("is_remote") or c.get("remote")),
        )
        return {
            "title": c.get("title") or "Untitled",
            "company": c.get("company"),
            "company_domain": c.get("company_domain"),
            "company_careers_url": c.get("company_careers_url"),
            "description_md": description_md,
            "description_text": description_text,
            "description_lang": lang,
            "description_hash": content_norm.sha256_text(description_text),
            "employment_type": c.get("employment_type"),
            "location_raw": c.get("location_raw"),
            "locations": locations,
            "remote_worldwide": bool(
                any(loc.get("remote") and not loc.get("country") for loc in locations)
            ),
            "posted_at": c.get("first_published") or c.get("published_at") or c.get("created_at"),
            "salary_original_text": salary.original_text if salary else None,
            "salary_min": salary.min if salary else None,
            "salary_max": salary.max if salary else None,
            "salary_currency": salary.currency if salary else None,
            "salary_period": salary.period if salary else None,
            "salary_confidence": salary.confidence if salary else None,
            "salary_annual_min": annual_min,
            "salary_annual_max": annual_max,
            "salary": (salary.as_dict() if salary else None),
            "origin_provider": c.get("origin_provider"),
            "origin_board": c.get("origin_board"),
            "origin_job_id": c.get("origin_job_id"),
            "canonical_url": observation["canonical_url_candidate"],
            "fingerprint": c.get("fingerprint") or content_norm.content_hash(
                c.get("title") or "", c.get("company") or "", description_text
            ),
            "raw_content": c,
        }

    @staticmethod
    def _salary_raw_text(c: dict) -> str | None:
        if c.get("salary_text"):
            return str(c["salary_text"])
        salary = c.get("salary")
        if isinstance(salary, dict):
            return salary.get("raw") or salary.get("text")
        if isinstance(salary, str):
            return salary
        return None

    def _origin_for(self, observation) -> dict:
        c = json.loads(observation["content_json"] or "{}")
        origin = {}
        if c.get("origin_provider"):
            origin["origin_provider"] = c["origin_provider"]
        if c.get("origin_board"):
            origin["origin_board"] = c["origin_board"]
        if c.get("origin_job_id"):
            origin["origin_job_id"] = c["origin_job_id"]
        return origin

    def _content_revision(self, normalized: dict) -> int:
        return int(normalized["fingerprint"][:8], 16)

    # -------------------------------------------------- canonical projection
    def _update_canonical_projection(self, job_id: str, normalized: dict, observation, now: str) -> None:
        job = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            return
        new_rev = self._content_revision(normalized)
        if job["content_revision"] == new_rev and job["fingerprint"] == normalized["fingerprint"]:
            # Unchanged content: advance verification only (no revision bump).
            with immediate_transaction(self.db.conn) as tx:
                tx.execute(
                    "UPDATE jobs SET last_verified_at=?, updated_at=? WHERE id=?",
                    (now, now, job_id),
                )
            return
        if job["content_revision"] > new_rev and job["first_seen_at"] < observation["observed_at"]:
            # Older content cannot regress a newer projection (RUN-21).
            with immediate_transaction(self.db.conn) as tx:
                tx.execute("UPDATE jobs SET last_verified_at=? WHERE id=?", (now, job_id))
            return
        old = dict(job)
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "UPDATE jobs SET title=?, description_md=?, description_text=?, description_lang=?,"
                " description_hash=?, employment_type=?, remote_worldwide=?, salary_original_text=?,"
                " salary_min=?, salary_max=?, salary_currency=?, salary_period=?,"
                " salary_annual_min_ref=?, salary_annual_max_ref=?, salary_confidence=?,"
                " posted_at=COALESCE(?, posted_at), last_changed_at=?, content_revision=?,"
                " fingerprint=?, updated_at=? WHERE id=?",
                (normalized["title"], normalized["description_md"], normalized["description_text"],
                 normalized["description_lang"], normalized["description_hash"],
                 normalized["employment_type"], 1 if normalized["remote_worldwide"] else 0,
                 normalized["salary_original_text"], normalized["salary_min"], normalized["salary_max"],
                 normalized["salary_currency"], normalized["salary_period"], normalized["salary_annual_min"],
                 normalized["salary_annual_max"], normalized["salary_confidence"], normalized["posted_at"],
                 now, new_rev, normalized["fingerprint"], now, job_id),
            )
            tx.execute("DELETE FROM job_locations WHERE job_id=?", (job_id,))
            for loc in normalized["locations"]:
                tx.execute(
                    "INSERT INTO job_locations(id, job_id, raw_text, country, region, city, remote,"
                    " confidence) VALUES (?,?,?,?,?,?,?,?)",
                    ("jl-" + secrets.token_hex(8), job_id, loc.get("raw_text"), loc.get("country"),
                     loc.get("region"), loc.get("city"), 1 if loc.get("remote") else 0,
                     loc.get("confidence", 0.4)),
                )
            tx.execute("DELETE FROM job_facts WHERE job_id=?", (job_id,))
            for fact in facts_mod.extract_facts(
                normalized["title"], normalized["description_text"] or "",
                employment_hint=normalized.get("employment_type"),
            ):
                tx.execute(
                    "INSERT INTO job_facts(id, job_id, fact_type, value_json, confidence,"
                    " evidence_text, evidence_start, evidence_end, rule_id, rule_version, observed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("jf-" + secrets.token_hex(8), job_id, fact.fact_type, fact.value_json if hasattr(fact, "value_json") else json.dumps(fact.value),
                     fact.confidence, fact.evidence_text, fact.evidence_start, fact.evidence_end,
                     fact.rule_id, fact.rule_version, now),
                )
        change_class = self._change_class(old, normalized)
        if change_class != "UNCHANGED":
            with immediate_transaction(self.db.conn) as tx:
                tx.execute(
                    "INSERT INTO job_history(id, job_id, at, change_class, detail_json,"
                    " evidence_observation_id, source_id) VALUES (?,?,?,?,?,?,?)",
                    ("jh-" + secrets.token_hex(10), job_id, now, change_class,
                     json.dumps({"observed_via": observation["source_id"]}, sort_keys=True),
                     observation["id"], observation["source_id"]),
                )
        self._update_fts(job_id, normalized)

    def _change_class(self, old: dict, new: dict) -> str:
        if (old["salary_min"] or 0) != (new["salary_min"] or 0) or (old["salary_max"] or 0) != (new["salary_max"] or 0):
            return "SALARY_CHANGED"
        if (old["title"] or "").strip() != (new["title"] or "").strip():
            return "TITLE_CHANGED"
        if (old["description_hash"] or "") != (new["description_hash"] or ""):
            return "CONTENT_CHANGED"
        return "UNCHANGED"

    def _update_fts(self, job_id: str, normalized: dict) -> None:
        company = self.db.query_one(
            "SELECT c.name FROM companies c JOIN jobs j ON j.company_id = c.id WHERE j.id=?", (job_id,)
        )
        locations_text = " ".join(str(loc.get("raw_text") or "") for loc in normalized["locations"])
        facts_text = " ".join(
            f"{f['fact_type']}:{f['value_json']}"
            for f in self.db.query("SELECT fact_type, value_json FROM job_facts WHERE job_id=?", (job_id,))
        )
        try:
            with immediate_transaction(self.db.conn) as tx:
                tx.execute("DELETE FROM jobs_fts WHERE job_id = ?", (job_id,))
                tx.execute(
                    "INSERT INTO jobs_fts(job_id, title, company, description, locations, facts)"
                    " VALUES (?,?,?,?,?,?)",
                    (job_id, normalized["title"] or "", (company and company["name"]) or "",
                     (normalized["description_text"] or "")[:20000], locations_text, facts_text[:2000]),
                )
        except Exception:
            pass  # FTS maintenance must never block canonical processing

    # ------------------------------------------- eligibility/score/inbox
    def _reevaluate_for_profiles(self, job_id: str, normalized: dict, observation, now: str) -> None:
        from jobscraper.domain.profiles import list_profiles, current_profile_snapshot

        job = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            return
        locations = [
            {"country": loc["country"], "region": loc["region"], "city": loc["city"], "remote": bool(loc["remote"])}
            for loc in self.db.query("SELECT * FROM job_locations WHERE job_id=?", (job_id,))
        ]
        facts = [
            {"fact_type": f["fact_type"], "value": json.loads(f["value_json"])}
            for f in self.db.query("SELECT fact_type, value_json FROM job_facts WHERE job_id=?", (job_id,))
        ]
        salary_dict = None
        if job["salary_min"] is not None or job["salary_max"] is not None:
            lo, hi, _ = (
                salary_mod.SalaryParse(
                    original_text=job["salary_original_text"] or "",
                    min=job["salary_min"], max=job["salary_max"],
                    currency=job["salary_currency"], period=job["salary_period"],
                ).annualized()
            )
            salary_dict = {"annual_min_usd": lo, "annual_max_usd": hi}

        for profile in list_profiles(self.db):
            snapshot = current_profile_snapshot(self.db, profile["id"])
            if snapshot is None:
                continue
            eligibility = evaluate_eligibility(
                title=job["title"],
                locations=locations,
                facts=facts,
                profile=snapshot,
                remote_worldwide=bool(job["remote_worldwide"]),
            )
            score, breakdown = score_job(
                title=job["title"],
                description_text=job["description_text"] or "",
                company=None,
                locations=locations,
                salary=salary_dict,
                facts=facts,
                eligibility_verdict=eligibility.verdict,
                profile=snapshot,
            )
            with immediate_transaction(self.db.conn) as tx:
                tx.execute(
                    "INSERT INTO job_eligibility(job_id, profile_id, profile_revision_id, rules_revision_id,"
                    " job_content_revision, normalization_version, evaluator_version, verdict, confidence,"
                    " reason_codes_json, evidence_json, rule_version, evaluated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(job_id, profile_id) DO UPDATE SET"
                    " profile_revision_id=excluded.profile_revision_id,"
                    " job_content_revision=excluded.job_content_revision,"
                    " verdict=excluded.verdict, confidence=excluded.confidence,"
                    " reason_codes_json=excluded.reason_codes_json, evidence_json=excluded.evidence_json,"
                    " evaluated_at=excluded.evaluated_at"
                    " WHERE excluded.job_content_revision >= job_eligibility.job_content_revision",
                    (job_id, profile["id"], snapshot.get("_revision_id"), "rules-v1",
                     job["content_revision"], NORMALIZATION_VERSION, eligibility.verdict and "eligibility-v1",
                     eligibility.verdict, eligibility.confidence,
                     json.dumps(eligibility.reason_codes), json.dumps(eligibility.evidence, sort_keys=True),
                     "eligibility-rules-v1", now),
                )
                tx.execute(
                    "INSERT INTO job_scores(job_id, profile_id, profile_revision_id, rules_revision_id,"
                    " job_content_revision, normalization_version, scorer_version, score, breakdown_json,"
                    " rule_version, scored_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(job_id, profile_id) DO UPDATE SET"
                    " profile_revision_id=excluded.profile_revision_id,"
                    " job_content_revision=excluded.job_content_revision, score=excluded.score,"
                    " breakdown_json=excluded.breakdown_json, scored_at=excluded.scored_at"
                    " WHERE excluded.job_content_revision >= job_scores.job_content_revision",
                    (job_id, profile["id"], snapshot.get("_revision_id"), "rules-v1",
                     job["content_revision"], NORMALIZATION_VERSION, "scorer-v1", score,
                     json.dumps(breakdown, sort_keys=True), "scoring-rules-v1", now),
                )
            inbox_mod.evaluate_inbox_triggers(
                self.db,
                job_id=job_id,
                profile_id=profile["id"],
                score=score,
                eligibility=eligibility.verdict,
                min_score_inbox=float(snapshot.get("min_score_inbox") or 0.0),
                content_revision=job["content_revision"],
                profile_revision_id=snapshot.get("_revision_id"),
                listing_status=job["listing_status"],
                now=now,
            )
