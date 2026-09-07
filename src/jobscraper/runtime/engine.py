"""The run engine: immutable plans, durable acquisition, coverage, aggregation.

Authority: ARC-05 (canonical runtime flow), RUN-01/02 (run states, immutable
RunSourcePlan), RUN-05 (request uniqueness), ACQ-02 (adapter protocol),
RUN-08 (fenced terminal commit), RUN-13 (absence), VER-13.

Adapters plan and parse; the engine (host) owns I/O, policy, fencing,
coverage finalization and run aggregation.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from jobscraper.acquisition import classifier as page_classifier
from jobscraper.acquisition.adapters.base import scope_key_for_binding
from jobscraper.acquisition.contracts import (
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    ExecutionClass,
    FailureKind,
    PageClass,
    ParseOutcomeKind,
    REQUEST_TYPE_TO_TASK,
    RequestType,
    Strategy,
    ValidatedResultEnvelope,
)
from jobscraper.acquisition.executors.http import HttpExecutor
from jobscraper.acquisition.registry import AdapterRegistry
from jobscraper.config import AppConfig
from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.diagnostics.events import EventLog, event
from jobscraper.queue import claims as queue_claims
from jobscraper.queue import retry as queue_retry
from jobscraper.runtime import obligations as obl
from jobscraper.runtime.presence import apply_absence_coverage
from jobscraper.security.netpolicy import NetworkPolicy, normalize_host_for_lookup
from jobscraper.timeutil import utc_now_s

GROUP_SATISFIED = "SATISFIED"
GROUP_SATISFIED_PARTIAL = "SATISFIED_PARTIAL"
GROUP_FAILED = "FAILED"
GROUP_CANCELLED = "CANCELLED"
GROUP_POLICY_DENIED = "POLICY_DENIED"
GROUP_SKIPPED = "SKIPPED_NOT_NEEDED"

RUN_TERMINAL_STATES = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}


@dataclass
class EngineReport:
    run_id: str
    status: str
    groups: dict
    observations: int
    jobs_saved: int
    jobs_updated: int
    coverage_applied: int
    processing_pending: int


class PolicyDenied(Exception):
    pass


class RunEngine:
    """Service-owned run orchestrator (single-machine capacity coordinator)."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        registry: AdapterRegistry,
        *,
        event_log: EventLog | None = None,
        policy_factory: Callable[[dict], NetworkPolicy] | None = None,
        max_requests_per_plan: int = 50,
    ) -> None:
        self.config = config
        self.db = db
        self.registry = registry
        self.events = event_log or EventLog(db.conn)
        self.policy_factory = policy_factory or (lambda ctx: NetworkPolicy())
        self.max_requests_per_plan = max_requests_per_plan

    # ------------------------------------------------------------ run setup
    def create_run(
        self,
        *,
        profile_id: str | None = None,
        source_ids: list[str],
        run_kind: str = "COLLECT",
        query_id: str | None = None,
        only_viable: bool = True,
    ) -> str:
        """Create a run with immutable RunSourcePlans (pinned fallback order)."""
        run_id = "run-" + secrets.token_hex(10)
        now = utc_now_s()
        plans = []
        for source_id in source_ids:
            bindings = self._viable_bindings_with_authority(source_id)
            if not bindings:
                continue
            for b in bindings:
                plans.append((source_id, b))
        profile_revision = None
        if profile_id:
            row = self.db.query_one("SELECT current_revision_id FROM search_profiles WHERE id=?", (profile_id,))
            if row:
                profile_revision = row["current_revision_id"]
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "INSERT INTO scrape_runs(id, run_kind, profile_id, query_id, status,"
                " collection_status, created_at) VALUES (?,?,?,?, 'QUEUED','PENDING',?)",
                (run_id, run_kind, profile_id, query_id, now),
            )
            for source_id, b in plans:
                plan_id = "rsp-" + secrets.token_hex(10)
                config_snapshot = json.loads(b["config_json"])
                crawl_policy = {
                    "max_requests_per_plan": self.max_requests_per_plan,
                    "max_pages": 100,
                    "max_runtime_s": 900,
                    "scope_hosts": [_host_of(config_snapshot, source_id)],
                }
                run_config_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "source": source_id,
                            "binding": b["id"],
                            "adapter": [b["adapter_id"], b["adapter_version"]],
                            "strategy": b["strategy"],
                            "config": config_snapshot,
                            "crawl": crawl_policy,
                            "profile_revision": profile_revision,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                tx.execute(
                    "INSERT INTO run_source_plans(id, run_id, source_id, source_revision_id,"
                    " source_config_snapshot_ref, query_id, query_revision_id, source_plan_group_id,"
                    " fallback_rank, binding_id, binding_revision_id, binding_revision,"
                    " binding_config_snapshot_json, adapter_id, adapter_version, adapter_api_version,"
                    " strategy, execution_class, cursor_schema_version, crawl_policy_snapshot_json,"
                    " rate_policy_snapshot_json, auth_scope_id, permission_profile_id,"
                    " permission_profile_revision, profile_revision, rules_revision, run_config_hash,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, run_id, source_id, self._source_revision_id(source_id), "{}",
                     query_id, None, b["fallback_group"], b["fallback_rank"], b["id"],
                     b["binding_revision_id"], b["binding_revision"], b["config_json"],
                     b["adapter_id"], b["adapter_version"], "1", b["strategy"], b["execution_class"],
                     self._cursor_schema(b), json.dumps(crawl_policy, sort_keys=True), "{}",
                     b["auth_scope_id"], b["permission_profile_id"], b["permission_profile_revision"],
                     profile_revision, "rules-v1", run_config_hash, now),
                )
        self.events.info("run.created", f"run {run_id} created with {len(plans)} source plans", run_id=run_id)
        return run_id

    def _viable_bindings_with_authority(self, source_id: str) -> list:
        from jobscraper.domain.sources import viable_bindings

        bindings = list(viable_bindings(self.db, source_id))
        # Current authority override: permission-profile revocation excludes a
        # binding even though its pinned plan remains historically resolvable.
        allowed = []
        for b in bindings:
            pp = self.db.query_one(
                "SELECT administrative_state FROM adapter_permission_profiles WHERE id=?",
                (b["permission_profile_id"],),
            )
            if pp is None or pp["administrative_state"] == "REVOKED":
                continue
            allowed.append(b)
        return allowed

    def _source_revision_id(self, source_id: str) -> str:
        row = self.db.query_one("SELECT current_revision_id FROM sources WHERE id=?", (source_id,))
        return (row and row["current_revision_id"]) or "unknown"

    def _cursor_schema(self, binding) -> int:
        manifest = self.registry.manifest_for(binding["adapter_id"], binding["adapter_version"])
        return manifest.cursor_schema_version if manifest else 1

    # ----------------------------------------------------------- execution
    def execute_run(self, run_id: str) -> EngineReport:
        run = self.db.query_one("SELECT * FROM scrape_runs WHERE id=?", (run_id,))
        if run is None:
            raise KeyError(run_id)
        if run["status"] in RUN_TERMINAL_STATES:
            return self._report(run_id, {})
        now = utc_now_s()
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "UPDATE scrape_runs SET status='RUNNING', started_at=COALESCE(started_at,?),"
                " collection_status='RUNNING' WHERE id=? AND status IN ('QUEUED','PARTIAL')",
                (now, run_id),
            )
        plans = self.db.query(
            "SELECT * FROM run_source_plans WHERE run_id=? ORDER BY source_plan_group_id, fallback_rank",
            (run_id,),
        )
        groups: dict[str, str] = {}
        executed: set[str] = set()
        for plan in plans:
            group = plan["source_plan_group_id"]
            if group in executed or groups.get(group) in (GROUP_SATISFIED, GROUP_SATISFIED_PARTIAL):
                self._set_group_status(plan["id"], GROUP_SKIPPED, {"reason": "earlier rank satisfied group"})
                continue
            outcome = self._execute_plan(run_id, plan)
            groups[group] = outcome
            if outcome in (GROUP_SATISFIED, GROUP_SATISFIED_PARTIAL):
                executed.add(group)
        status = self._aggregate_run_status(run_id, groups)
        self._finalize_coverages(run_id)
        coverage_applied = self._apply_coverages(run_id)
        # Drain local processing obligations (no network I/O).
        from jobscraper.runtime.processing import ProcessingPipeline

        pipeline = ProcessingPipeline(self.config, self.db, self.events)
        pipeline.drain(run_id=run_id)
        pending = obl.pending_count(self.db, run_id)
        counts = self._run_counts(run_id)
        now = utc_now_s()
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "UPDATE scrape_runs SET status=?, finished_at=?, collection_status='COMPLETED',"
                " processing_status=?, jobs_discovered=?, jobs_saved=?, jobs_updated=? WHERE id=?",
                (status, now, "COMPLETED" if pending == 0 else "PENDING_PROCESSING",
                 counts["discovered"], counts["saved"], counts["updated"], run_id),
            )
        self.events.info("run.finished", f"run {run_id} -> {status}", run_id=run_id, data={"groups": groups})
        return self._report(run_id, groups, coverage_applied=coverage_applied, pending=pending, counts=counts)

    def _execute_plan(self, run_id: str, plan) -> str:
        """Drive one run-source-plan to a logical outcome."""
        if plan["execution_class"] == ExecutionClass.BROWSER.value:
            # Browser execution runs through the browser worker; unavailable
            # runtime is a typed denied/unsupported outcome (fail closed).
            return self._browser_unavailable_outcome(run_id, plan)
        request_id = queue_claims.enqueue_request(
            self.db,
            run_id=run_id,
            run_source_plan_id=plan["id"],
            source_id=plan["source_id"],
            binding_id=plan["binding_id"],
            request_type=RequestType.LIST_FETCH.value,
            request_unique_key=f"list:{plan['id']}:init",
            payload={"page": 0},
            strategy=plan["strategy"],
            execution_class="HTTP",
            priority=10,
        )
        if request_id is None:
            # Resume: plan already has its initial request (or terminal state).
            request_id = self.db.query_one(
                "SELECT id FROM scrape_requests WHERE run_id=? AND request_unique_key=?",
                (run_id, f"list:{plan['id']}:init"),
            )
            if request_id is None:
                return GROUP_FAILED
            request_id = request_id["id"]
        coverage_id = self._ensure_coverage(run_id, plan)
        self.db.execute(
            "UPDATE scrape_requests SET coverage_generation_id=? WHERE id=?", (coverage_id, request_id)
        )
        outcomes: list[str] = []
        budget = self.max_requests_per_plan
        while budget > 0:
            budget -= 1
            claim = queue_claims.claim_next_request(
                self.db, worker_id="service", run_id=run_id,
                request_types=("LIST_FETCH", "DETAIL_FETCH", "SOURCE_CRAWL"),
            )
            if claim is None:
                break
            outcomes.append(self._execute_request(claim, plan, coverage_id))
        final = self._plan_outcome(run_id, plan, coverage_id, outcomes)
        self._set_group_status(plan["id"], final, {"outcomes": outcomes[:20]})
        return final

    def _browser_unavailable_outcome(self, run_id: str, plan) -> str:
        """BROWSER class plan in an environment without the browser worker:
        typed unavailable outcome (fail closed), never a silent bypass."""
        self.events.warn(
            "browser.unavailable",
            "browser execution class requested but browser worker runtime unavailable",
            run_id=run_id,
            source_id=plan["source_id"],
            binding_id=plan["binding_id"],
        )
        self._set_group_status(plan["id"], GROUP_POLICY_DENIED, {"reason": "NO_BROWSER_RUNTIME"})
        return GROUP_POLICY_DENIED

    # -------------------------------------------------------- one request
    def _execute_request(self, claim: queue_claims.Claim, plan, coverage_id: str) -> str:
        adapter = self._build_adapter(plan)
        task = AdapterTask(
            kind=REQUEST_TYPE_TO_TASK[RequestType(claim.request_type)],
            payload={**claim.payload, "entry_url": self._entry_url(plan)},
        )
        cursor = self._load_cursor(plan)
        from jobscraper.acquisition.contracts import PlanningContext

        ctx = PlanningContext(
            run_id=claim.run_id if hasattr(claim, "run_id") else self._run_id_of_request(claim.request_id),
            run_source_plan_id=plan["id"],
            source_snapshot_ref=plan["source_revision_id"],
            binding_revision_id=plan["binding_revision_id"],
            permission_profile_revision=plan["permission_profile_revision"],
            policy_snapshot_ref=plan["crawl_policy_snapshot_json"],
        )
        try:
            request_plan = adapter.plan(task, cursor, ctx)
        except Exception as exc:
            queue_claims.record_failure(
                self.db, request_id=claim.request_id, attempt_id=claim.attempt_id,
                failure_kind="UNSUPPORTED", failure_detail=f"plan error: {exc}", retryable=False,
            )
            return "PLAN_ERROR"

        # Host-side plan validation against pinned permission/policy.
        try:
            self._validate_plan_authority(plan, request_plan)
        except PolicyDenied as exc:
            queue_claims.record_failure(
                self.db, request_id=claim.request_id, attempt_id=claim.attempt_id,
                failure_kind="POLICY_REJECTED", failure_detail=str(exc), retryable=False,
            )
            return "POLICY_REJECTED"

        policy = self.policy_factory({"plan": dict(plan), "request_plan": request_plan.__dict__})
        executor = HttpExecutor(policy, timeout_s=self.config.http_timeout_s)
        envelope_inputs = {
            "execution_plan_id": f"ep-{claim.attempt_id}",
            "request_id": claim.request_id,
            "attempt_id": claim.attempt_id,
            "run_source_plan_id": plan["id"],
            "source_id": plan["source_id"],
            "binding_id": plan["binding_id"],
            "binding_revision_id": plan["binding_revision_id"],
            "adapter_id": plan["adapter_id"],
            "adapter_version": plan["adapter_version"],
            "strategy": plan["strategy"],
            "execution_class": "HTTP",
        }
        plan_dict = {
            "method": request_plan.method,
            "url": request_plan.url,
            "headers": dict(request_plan.headers_without_secrets),
            "validators": dict(request_plan.validators),
            "timeout_s": request_plan.timeout_s,
            "max_bytes": request_plan.max_bytes,
            "max_redirects": self.config.http_max_redirects,
        }
        result = executor.execute(envelope_inputs, plan_dict)
        self._record_fetch_attempt(envelope_inputs, result.envelope, claim)
        if not result.ok and result.envelope.status_code is None:
            # Transport/policy-level failure: no response to classify.
            status = queue_claims.record_failure(
                self.db, request_id=claim.request_id, attempt_id=claim.attempt_id,
                failure_kind=result.envelope.failure_kind or "CONNECT_ERROR",
                failure_detail=result.envelope.failure_detail,
                retry_after_s=self._retry_after(result.envelope),
            )
            self._health_event(plan, result.envelope.failure_kind)
            if result.envelope.failure_kind in ("RATE_LIMIT", "HTTP_4XX") and (result.envelope.status_code == 429):
                queue_retry.record_rate_limit(
                    self.db, scope_key=queue_retry.binding_scope_key(plan["binding_id"]),
                    retry_after_s=self._retry_after(result.envelope),
                )
            return status

        # 304 handling (RUN-13 / cache representation semantics).
        if result.envelope.was_304:
            reuse = self._reuse_cache_representation(plan, coverage_id)
            if reuse is not None:
                self._complete_request_with_cache(claim, plan, coverage_id, result.envelope, reuse)
                return "SUCCEEDED_304_REUSED"
            # Incompatible/missing representation: refetch unconditionally.
            plan_dict["validators"] = {}
            result = executor.execute(envelope_inputs, plan_dict)
            self._record_fetch_attempt(envelope_inputs, result.envelope, claim)
            if not result.ok:
                queue_claims.record_failure(
                    self.db, request_id=claim.request_id, attempt_id=claim.attempt_id,
                    failure_kind=result.envelope.failure_kind or "CONNECT_ERROR",
                    failure_detail=result.envelope.failure_detail,
                )
                return "FAILED"

        classification = page_classifier.classify(
            result.envelope,
            expected="LIST",
            source_signatures=self._source_signatures(plan),
        )
        if not classification.is_valid:
            mapped = _invalid_class_failure(classification.page_class, result.envelope)
            if mapped is None:
                # EMPTY is a recognized outcome: complete with empty coverage.
                self._commit_empty_success(claim, plan, coverage_id, result.envelope, classification)
                return "SUCCEEDED_EMPTY"
            status = queue_claims.record_failure(
                self.db, request_id=claim.request_id, attempt_id=claim.attempt_id,
                failure_kind=mapped[0], failure_detail=f"{classification.page_class.value}: {classification.reason}",
                retryable=mapped[1],
                retry_after_s=self._retry_after(result.envelope) if result.envelope.status_code == 429 else None,
            )
            self._health_event(plan, mapped[0])
            if result.envelope.status_code == 429:
                queue_retry.record_rate_limit(
                    self.db, scope_key=queue_retry.binding_scope_key(plan["binding_id"]),
                    retry_after_s=self._retry_after(result.envelope),
                )
            return status
        if not result.ok:
            # Classified valid despite executor ok=False: treat as usable.
            pass

        validated = ValidatedResultEnvelope(
            result_envelope_ref=result.envelope.execution_plan_id,
            result=result.envelope,
            validated_page_class=classification.page_class.value,
            validation_evidence_ref=f"cls:{classification.reason}",
            security_policy_result="ALLOWED",
        )
        from jobscraper.acquisition.contracts import ParseContext

        parse_ctx = ParseContext(
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            run_source_plan_id=plan["id"],
            parser_version=f"{plan['adapter_id']}:{plan['adapter_version']}",
            recipe_version=None,
            idempotency_namespace=claim.request_id,
        )
        outcome = adapter.parse(task, validated, parse_ctx)
        return self._commit_outcome(claim, plan, coverage_id, result.envelope, outcome, validated)

    # -------------------------------------------------------- commit paths
    def _commit_outcome(self, claim, plan, coverage_id, envelope, outcome, validated) -> str:
        """Fenced terminal commit of a parsed outcome (RUN-08)."""
        now = utc_now_s()
        parse_attempt_id = "pa-" + secrets.token_hex(10)
        observations_committed = 0

        def commit(tx):
            nonlocal observations_committed
            tx.execute(
                "INSERT INTO parse_attempts(id, request_id, attempt_id, fetch_attempt_id, started_at,"
                " finished_at, parser_id, parser_version, outcome_kind, detail_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (parse_attempt_id, claim.request_id, claim.attempt_id, None, now, now,
                 plan["adapter_id"], plan["adapter_version"], outcome.kind.value,
                 json.dumps({"failure_kind": outcome.failure_kind})),
            )
            for draft in outcome.observations:
                if draft.is_closure_evidence:
                    continue  # typed closure evidence handled separately
                obs_id = "obs-" + secrets.token_hex(10)
                content = dict(draft.content)
                tx.execute(
                    "INSERT INTO job_observations(id, run_id, request_id, attempt_id, source_id,"
                    " binding_id, adapter_id, adapter_version, strategy, execution_class, query_id,"
                    " source_job_id, raw_url, canonical_url_candidate, application_url_candidate,"
                    " page_cursor_json, enumeration_scope_key, source_rank_or_order, observed_at,"
                    " parse_evidence_ref, observation_unique_key, content_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (obs_id, self._run_id_of_request(claim.request_id), claim.request_id,
                     claim.attempt_id, plan["source_id"], plan["binding_id"], plan["adapter_id"],
                     plan["adapter_version"], plan["strategy"], plan["execution_class"], None,
                     draft.source_job_id, draft.raw_url, draft.canonical_url_candidate,
                     draft.application_url_candidate, draft.page_cursor,
                     draft.enumeration_scope_key or self._adapter_scope_key(plan),
                     draft.source_rank_or_order, now, parse_attempt_id,
                     draft.observation_unique_key or f"{claim.request_id}:{obs_id}",
                     json.dumps(content, sort_keys=True)),
                )
                observations_committed += 1
                # Atomic downstream processing obligation (cancellation-safe).
                obl.create_obligation(
                    tx, kind="PROCESS_OBSERVATION", observation_id=obs_id,
                    run_id=self._run_id_of_request(claim.request_id), request_id=claim.request_id,
                    payload={"plan_id": plan["id"]}, now=now,
                )
                # Coverage membership.
                if draft.source_job_id:
                    identity = f"{plan['source_id']}:{draft.source_job_id}"
                    tx.execute(
                        "INSERT OR IGNORE INTO coverage_seen_identity(coverage_id, stable_source_identity,"
                        " source_identity_generation, observation_or_listing_evidence_ref)"
                        " VALUES (?,?,1,?)",
                        (coverage_id, identity, obs_id),
                    )
                for fe in draft.field_evidence:
                    tx.execute(
                        "INSERT INTO field_evidence(id, observation_id, attempt_id, field, evidence_kind,"
                        " evidence_text, locator_json, confidence) VALUES (?,?,?,?,?,?,?,?)",
                        ("fe-" + secrets.token_hex(8), obs_id, claim.attempt_id,
                         fe.get("field", "unknown"), fe.get("kind", "text"),
                         fe.get("text"), json.dumps(fe.get("locator")), fe.get("confidence", 0.5)),
                    )
            # Child tasks (typed, deterministic).
            for child in outcome.discovered_tasks:
                queue_claims.enqueue_request_tx(
                    tx,
                    run_id=self._run_id_of_request(claim.request_id),
                    run_source_plan_id=plan["id"],
                    source_id=plan["source_id"],
                    binding_id=plan["binding_id"],
                    request_type="LIST_FETCH" if child.kind.value == "ENUMERATE" else "DETAIL_FETCH",
                    request_unique_key=f"list:{plan['id']}:{child.logical_key}",
                    payload={"target": child.target_reference, "entry_url": self._entry_url(plan)},
                    strategy=plan["strategy"],
                    execution_class="HTTP",
                    priority=20,
                    depth=child.depth,
                    parent_request_id=claim.request_id,
                    coverage_generation_id=coverage_id,
                    now=now,
                )
            # Contributing request bookkeeping.
            tx.execute(
                "INSERT OR IGNORE INTO coverage_contributing_request(coverage_id, request_id)"
                " VALUES (?,?)",
                (coverage_id, claim.request_id),
            )
            # Cursor update.
            if outcome.cursor_proposal is not None:
                tx.execute(
                    "INSERT INTO crawl_cursors(source_id, binding_id, adapter_id, adapter_version,"
                    " cursor_schema_version, state_json, checkpoint_at) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(source_id, binding_id, adapter_id, adapter_version, cursor_schema_version)"
                    " DO UPDATE SET state_json=excluded.state_json, checkpoint_at=excluded.checkpoint_at",
                    (plan["source_id"], plan["binding_id"], plan["adapter_id"], plan["adapter_version"],
                     self._cursor_schema(plan), json.dumps(outcome.cursor_proposal, sort_keys=True), now),
                )
            # Cache representation for future revalidation.
            tx.execute(
                "INSERT INTO cache_representations(id, source_id, binding_revision_id,"
                " auth_scope_generation, request_variant_key, validated_page_class, content_hash,"
                " parser_recipe_compatibility_key, membership_ref, stored_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(source_id, binding_revision_id, auth_scope_generation, request_variant_key)"
                " DO UPDATE SET content_hash=excluded.content_hash, membership_ref=excluded.membership_ref,"
                " stored_at=excluded.stored_at, validated_page_class=excluded.validated_page_class",
                ("cr-" + secrets.token_hex(10), plan["source_id"], plan["binding_revision_id"], "NONE",
                 "list", validated.validated_page_class, envelope.body_hash or "",
                 f"{plan['adapter_id']}:{plan['adapter_version']}", coverage_id, now),
            )
            tx.execute(
                "UPDATE scrape_requests SET page_class=?, bytes_downloaded=?, duration_ms=? WHERE id=?",
                (validated.validated_page_class, envelope.bytes_downloaded, envelope.duration_ms, claim.request_id),
            )

        try:
            if outcome.kind == ParseOutcomeKind.FAILURE:
                queue_claims.record_failure(
                    self.db, request_id=claim.request_id, attempt_id=claim.attempt_id,
                    failure_kind=outcome.failure_kind or "PARSE_EMPTY",
                    failure_detail=outcome.failure_detail,
                )
                return "FAILED"
            # The request itself is complete: continuation is carried by the
            # child requests enqueued atomically in this same commit (RUN-05);
            # RETRY_WAIT is reserved for transient failures.
            queue_claims.fenced_commit(
                self.db,
                request_id=claim.request_id,
                attempt_id=claim.attempt_id,
                commit=commit,
                terminal_status="SUCCEEDED",
                now=now,
            )
        except queue_claims.LeaseLost:
            return "LEASE_LOST"
        if outcome.continuation_required:
            return "PARTIAL"
        return "SUCCEEDED"

    def _commit_empty_success(self, claim, plan, coverage_id, envelope, classification) -> None:
        """A valid complete zero-job enumeration is success (RUN-01)."""
        now = utc_now_s()

        def commit(tx):
            tx.execute(
                "INSERT INTO parse_attempts(id, request_id, attempt_id, started_at, finished_at,"
                " parser_id, parser_version, outcome_kind, detail_json) VALUES (?,?,?,?,?,?,?,?,?)",
                ("pa-" + secrets.token_hex(10), claim.request_id, claim.attempt_id, now, now,
                 plan["adapter_id"], plan["adapter_version"], "SUCCESS_EMPTY",
                 json.dumps({"classification": classification.reason})),
            )
            tx.execute(
                "INSERT OR IGNORE INTO coverage_contributing_request(coverage_id, request_id) VALUES (?,?)",
                (coverage_id, claim.request_id),
            )
            tx.execute(
                "UPDATE scrape_requests SET page_class=?, bytes_downloaded=?, duration_ms=? WHERE id=?",
                (classification.page_class.value, envelope.bytes_downloaded, envelope.duration_ms, claim.request_id),
            )
            tx.execute(
                "UPDATE enumeration_coverage SET completion_state='COMPLETE', finished_at=?,"
                " terminal_enumeration_proven=1, cursor_terminal=1, finalized_at=?,"
                " coverage_authority=?, absence_inference_allowed=? WHERE id=?"
                " AND finalized_at IS NULL",
                (now, now, self._coverage_authority(plan), 1 if self._absence_allowed(plan) else 0, coverage_id),
            )

        try:
            queue_claims.fenced_commit(
                self.db, request_id=claim.request_id, attempt_id=claim.attempt_id,
                commit=commit, terminal_status="SUCCEEDED", now=now,
            )
        except queue_claims.LeaseLost:
            pass

    def _complete_request_with_cache(self, claim, plan, coverage_id, envelope, reuse: dict) -> None:
        """304 with retained compatible membership: reuse seen evidence, no parse."""
        now = utc_now_s()

        def commit(tx):
            tx.execute(
                "INSERT OR IGNORE INTO coverage_contributing_request(coverage_id, request_id) VALUES (?,?)",
                (coverage_id, claim.request_id),
            )
            for identity in reuse["seen"]:
                tx.execute(
                    "INSERT OR IGNORE INTO coverage_seen_identity(coverage_id, stable_source_identity,"
                           "INSERT OR IGNORE INTO coverage_seen_identity(coverage_id, stable_source_identity,"
                    " source_identity_generation, observation_or_listing_evidence_ref)"
                    " VALUES (?,?,1,'cache-reuse')",
                    (coverage_id, identity),
                )
            tx.execute(
                "UPDATE scrape_requests SET page_class='VALID_LIST' WHERE id=?", (claim.request_id,)
            )
            tx.execute(
                "UPDATE enumeration_coverage SET completion_state='COMPLETE', finished_at=?,"
                " terminal_enumeration_proven=1, cursor_terminal=1, finalized_at=?,"
                " coverage_authority=?, absence_inference_allowed=? WHERE id=? AND finalized_at IS NULL",
                (now, now, self._coverage_authority(plan), 1 if self._absence_allowed(plan) else 0, coverage_id),
            )

        try:
            queue_claims.fenced_commit(
                self.db, request_id=claim.request_id, attempt_id=claim.attempt_id,
                commit=commit, terminal_status="SUCCEEDED", now=now,
            )
        except queue_claims.LeaseLost:
            pass

    # ---------------------------------------------------------- coverage
    def _ensure_coverage(self, run_id: str, plan) -> str:
        scope = self._adapter_scope_key(plan)
        generation = f"{run_id}:{plan['id']}"
        existing = self.db.query_one(
            "SELECT id FROM enumeration_coverage WHERE run_source_plan_id=? AND scope_key=? AND generation_key=?",
            (plan["id"], scope, generation),
        )
        if existing:
            return existing["id"]
        coverage_id = "cov-" + secrets.token_hex(10)
        now = utc_now_s()
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "INSERT INTO enumeration_coverage(id, run_source_plan_id, source_plan_group_id, source_id,"
                " binding_id, binding_revision_id, scope_key, generation_key, started_at,"
                " completion_state, coverage_authority, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'UNKNOWN', ?, ?)",
                (coverage_id, plan["id"], plan["source_plan_group_id"], plan["source_id"],
                 plan["binding_id"], plan["binding_revision_id"], scope, generation, utc_now_s(),
                 self._coverage_authority(plan), now),
            )
        return coverage_id

    def _adapter_scope_key(self, plan) -> str:
        """Scope key as the adapter itself computes it (observations stamp the
        same key on job_sources; absence inference matches on it)."""
        try:
            adapter = self._build_adapter(plan)
            return adapter.scope_key()
        except Exception:
            return scope_key_for_binding(plan["source_id"], plan["binding_id"])

    def _coverage_authority(self, plan) -> str:
        manifest = self.registry.manifest_for(plan["adapter_id"], plan["adapter_version"])
        if manifest and manifest.supports_absence_authority:
            return "AUTHORITATIVE_FULL_SOURCE"
        return "NO_ABSENCE_INFERENCE"

    def _absence_allowed(self, plan) -> int:
        return 1 if self._coverage_authority(plan) == "AUTHORITATIVE_FULL_SOURCE" else 0

    def _finalize_coverages(self, run_id: str) -> None:
        """Host-owned finalization barrier (RUN-13 / VER-13)."""
        now = utc_now_s()
        rows = self.db.query(
            "SELECT c.* FROM enumeration_coverage c JOIN run_source_plans p ON p.id = c.run_source_plan_id"
            " WHERE p.run_id=? AND c.finalized_at IS NULL",
            (run_id,),
        )
        for cov in rows:
            open_requests = self.db.query_one(
                "SELECT COUNT(*) c FROM scrape_requests WHERE coverage_generation_id=?"
                " AND status IN ('PENDING','RUNNING','RETRY_WAIT')",
                (cov["id"],),
            )["c"]
            if open_requests > 0:
                continue
            terminal = self.db.query_one(
                "SELECT COUNT(*) c FROM scrape_requests WHERE coverage_generation_id=?"
                " AND status='SUCCEEDED' AND last_failure_kind IS NULL",
                (cov["id"],),
            )["c"]
            cancelled = self.db.query_one(
                "SELECT COUNT(*) c FROM scrape_requests WHERE coverage_generation_id=? AND status='CANCELLED'",
                (cov["id"],),
            )["c"]
            failed = self.db.query_one(
                "SELECT COUNT(*) c FROM scrape_requests WHERE coverage_generation_id=? AND status='FAILED'",
                (cov["id"],),
            )["c"]
            if cancelled:
                state = "CANCELLED"
            elif failed or terminal == 0:
                state = "FAILED" if failed else "PARTIAL"
            else:
                state = "COMPLETE"
            seen = self.db.query_one(
                "SELECT COUNT(*) c FROM coverage_seen_identity WHERE coverage_id=?", (cov["id"],)
            )["c"]
            proven = state == "COMPLETE"
            with immediate_transaction(self.db.conn) as tx:
                tx.execute(
                    "UPDATE enumeration_coverage SET completion_state=?, finished_at=?,"
                    " terminal_enumeration_proven=?, cursor_terminal=?, items_observed=?,"
                    " contributing_request_count=(SELECT COUNT(*) FROM coverage_contributing_request"
                    " WHERE coverage_contributing_request.coverage_id=enumeration_coverage.id),"
                    " finalized_at=?,"
                    " coverage_authority=?, absence_inference_allowed=? WHERE id=? AND finalized_at IS NULL",
                    (state, now, 1 if proven else 0, 1 if proven else 0, seen, now,
                     self._coverage_authority_for_coverage(cov), 1 if proven and self._authority_allows(cov) else 0,
                     cov["id"]),
                )

    def _coverage_authority_for_coverage(self, cov) -> str:
        return cov["coverage_authority"]

    def _authority_allows(self, cov) -> bool:
        return cov["coverage_authority"] in ("AUTHORITATIVE_FULL_SOURCE", "AUTHORITATIVE_DECLARED_SCOPE")

    def _apply_coverages(self, run_id: str) -> int:
        total = 0
        rows = self.db.query(
            "SELECT c.* FROM enumeration_coverage c JOIN run_source_plans p ON p.id = c.run_source_plan_id"
            " WHERE p.run_id=? AND c.applied_at IS NULL AND c.finalized_at IS NOT NULL",
            (run_id,),
        )
        for cov in rows:
            total += apply_absence_coverage(self.db, cov)
        return total

    # ------------------------------------------------------ plan outcome
    def _plan_outcome(self, run_id: str, plan, coverage_id: str, outcomes: list[str]) -> str:
        """Group outcome from durable request states (RUN-01 truth table)."""
        request_states = {
            r["status"]
            for r in self.db.query(
                "SELECT status FROM scrape_requests WHERE run_source_plan_id=?", (plan["id"],)
            )
        }
        if "CANCELLED" in request_states and "SUCCEEDED" not in request_states:
            return GROUP_CANCELLED
        pending = request_states & {"PENDING", "RUNNING", "RETRY_WAIT"}
        if "SUCCEEDED" in request_states and not pending:
            # Valid terminal data (possibly zero jobs: SUCCESS_EMPTY is success).
            return GROUP_SATISFIED
        if "SUCCEEDED" in request_states:
            # Valid usable data with continuation still pending.
            return GROUP_SATISFIED_PARTIAL
        if _has_observations(self.db, plan["id"]):
            return GROUP_SATISFIED_PARTIAL
        return GROUP_FAILED

    def _aggregate_run_status(self, run_id: str, groups: dict[str, str]) -> str:
        """RUN-01 truth table aggregated from logical source-plan groups."""
        cancelled = self.db.query_one(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id=?", (run_id,)
        )
        statuses = list(groups.values()) or [GROUP_FAILED]
        if cancelled and cancelled["cancel_requested_at"]:
            return "CANCELLED"
        if all(s == GROUP_SATISFIED for s in statuses):
            return "SUCCEEDED"
        if all(s in (GROUP_FAILED, GROUP_POLICY_DENIED) for s in statuses):
            return "FAILED"
        return "PARTIAL"

    # ------------------------------------------------------------- helpers
    def _build_adapter(self, plan):
        config = json.loads(plan["binding_config_snapshot_json"])
        if plan["adapter_id"] in ("greenhouse", "lever", "ashby"):
            config.setdefault("org", config.get("board") or config.get("company"))
        if "entry_url" not in config:
            config["entry_url"] = self._entry_url(plan)
        return self.registry.build(plan["adapter_id"], config)

    def _entry_url(self, plan) -> str:
        row = self.db.query_one("SELECT entry_url FROM sources WHERE id=?", (plan["source_id"],))
        return (row and row["entry_url"]) or "https://example.invalid/"

    def _load_cursor(self, plan) -> CrawlCursor | None:
        row = self.db.query_one(
            "SELECT * FROM crawl_cursors WHERE source_id=? AND binding_id=? AND adapter_id=?"
            " AND adapter_version=? AND cursor_schema_version=?",
            (plan["source_id"], plan["binding_id"], plan["adapter_id"], plan["adapter_version"],
             self._cursor_schema(plan)),
        )
        if row is None:
            return None
        return CrawlCursor(
            source_id=row["source_id"], binding_id=row["binding_id"], adapter_id=row["adapter_id"],
            adapter_version=row["adapter_version"], cursor_schema_version=row["cursor_schema_version"],
            state_json=row["state_json"], checkpoint_at=row["checkpoint_at"],
        )

    def _validate_plan_authority(self, plan, request_plan) -> None:
        from jobscraper.acquisition.contracts import ExecutionPlanEnvelope

        envelope = ExecutionPlanEnvelope(
            plan_id="validate", request_id="validate", attempt_id="validate", run_id="validate",
            run_source_plan_id=plan["id"], source_id=plan["source_id"], binding_id=plan["binding_id"],
            binding_revision_id=plan["binding_revision_id"], adapter_id=plan["adapter_id"],
            adapter_version=plan["adapter_version"], strategy=plan["strategy"],
            execution_class=plan["execution_class"], policy_snapshot_ref="validate",
            permission_profile_id=plan["permission_profile_id"],
            permission_profile_revision=plan["permission_profile_revision"],
            payload_kind="RequestPlan", payload=request_plan,
        )
        envelope.validate()  # read-only semantics
        # Host scope check against the pinned permission profile revision.
        pp_row = self.db.query_one(
            "SELECT policy_json FROM adapter_permission_profile_revisions WHERE id=?",
            (plan["permission_profile_revision"],),
        )
        if pp_row is None:
            raise PolicyDenied("pinned permission profile revision not resolvable")
        policy = json.loads(pp_row["policy_json"])
        hosts = policy.get("network_hosts") or []
        if hosts and hosts != ["*"]:
            host = normalize_host_for_lookup(urlsplit(request_plan.url).hostname or "")
            if host not in {normalize_host_for_lookup(h) for h in hosts}:
                raise PolicyDenied(f"plan URL host {host!r} outside approved host scope")

    def _source_signatures(self, plan) -> tuple[str, ...]:
        if plan["adapter_id"] == "greenhouse":
            return ('"jobs"', "greenhouse")
        if plan["adapter_id"] == "lever":
            return ('"text"', "lever")
        if plan["adapter_id"] == "ashby":
            return ('"jobs"', "ashby")
        return ()

    def _record_fetch_attempt(self, inputs, envelope, claim) -> None:
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "INSERT INTO fetch_attempts(id, request_id, attempt_id, execution_plan_id, started_at,"
                " finished_at, transport, requested_url, final_url, status_code, content_type,"
                " body_hash, normalized_content_hash, redirect_chain_json, headers_redacted_json,"
                " validators_sent_json, was_304, bytes_downloaded, duration_ms, failure_kind, failure_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("fa-" + secrets.token_hex(10), inputs["request_id"], inputs["attempt_id"],
                 inputs["execution_plan_id"], envelope.fetched_at, envelope.fetched_at,
                 envelope.transport, envelope.requested_url, envelope.final_url, envelope.status_code,
                 envelope.content_type, envelope.body_hash, envelope.normalized_content_hash,
                 json.dumps(envelope.redirect_chain), json.dumps(envelope.headers_redacted, sort_keys=True),
                 json.dumps(envelope.validators_sent, sort_keys=True), 1 if envelope.was_304 else 0,
                 envelope.bytes_downloaded, envelope.duration_ms, envelope.failure_kind,
                 json.dumps({"detail": envelope.failure_detail})),
            )

    def _retry_after(self, envelope) -> float | None:
        return queue_retry.parse_retry_after(envelope.headers_redacted.get("retry-after"))

    def _health_event(self, plan, failure_kind: str | None) -> None:
        if not failure_kind:
            return
        state = _failure_to_operational_state(failure_kind)
        if state is None:
            return
        now = utc_now_s()
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "INSERT INTO source_binding_health(binding_id, binding_revision_id, strategy,"
                " operational_state, dimensions_json, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(binding_id, binding_revision_id, strategy) DO UPDATE SET"
                " operational_state=excluded.operational_state, dimensions_json=excluded.dimensions_json,"
                " updated_at=excluded.updated_at, last_healthy_at=CASE WHEN excluded.operational_state='HEALTHY'"
                " THEN ? ELSE source_binding_health.last_healthy_at END",
                (plan["binding_id"], plan["binding_revision_id"], plan["strategy"], state,
                 json.dumps({"latest_failure": failure_kind}), now, now),
            )

    def _reuse_cache_representation(self, plan, coverage_id: str) -> dict | None:
        """304 may reuse a retained validated representation only when the
        same source/binding/auth/request variant and compatible parser exist
        AND its membership is retained (RUN-13)."""
        row = self.db.query_one(
            "SELECT * FROM cache_representations WHERE source_id=? AND binding_revision_id=?"
            " AND auth_scope_generation='NONE' AND request_variant_key='list'"
            " AND parser_recipe_compatibility_key=?",
            (plan["source_id"], plan["binding_revision_id"], f"{plan['adapter_id']}:{plan['adapter_version']}"),
        )
        if row is None or not row["membership_ref"]:
            return None
        seen = [
            r["stable_source_identity"]
            for r in self.db.query(
                "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id=?",
                (row["membership_ref"],),
            )
        ]
        if not seen:
            return None
        return {"membership_ref": row["membership_ref"], "seen": seen}

    def _set_group_status(self, plan_id: str, status: str, detail: dict) -> None:
        with immediate_transaction(self.db.conn) as tx:
            tx.execute(
                "UPDATE run_source_plans SET group_status=?, group_outcome_detail_json=? WHERE id=?",
                (status, json.dumps(detail, sort_keys=True), plan_id),
            )

    def _run_id_of_request(self, request_id: str) -> str:
        row = self.db.query_one("SELECT run_id FROM scrape_requests WHERE id=?", (request_id,))
        return (row and row["run_id"]) or "unknown"

    def _run_counts(self, run_id: str) -> dict:
        discovered = self.db.query_one(
            "SELECT COUNT(DISTINCT source_id || ':' || ifnull(source_job_id,'')) c FROM job_observations WHERE run_id=?",
            (run_id,),
        )["c"]
        saved = self.db.query_one(
            "SELECT COUNT(DISTINCT job_id) c FROM job_sources js WHERE js.last_observation_id IN"
            " (SELECT id FROM job_observations WHERE run_id=?)",
            (run_id,),
        )["c"]
        return {"discovered": discovered, "saved": saved, "updated": saved}

    def _report(self, run_id: str, groups: dict, coverage_applied: int = 0, pending: int = 0,
                counts: dict | None = None) -> EngineReport:
        run = self.db.query_one("SELECT * FROM scrape_runs WHERE id=?", (run_id,))
        counts = counts or {"discovered": 0, "saved": 0, "updated": 0}
        observations = self.db.query_one(
            "SELECT COUNT(*) c FROM job_observations WHERE run_id=?", (run_id,)
        )["c"]
        return EngineReport(
            run_id=run_id,
            status=run["status"] if run else "UNKNOWN",
            groups=groups,
            observations=observations,
            jobs_saved=counts["saved"],
            jobs_updated=counts["updated"],
            coverage_applied=coverage_applied,
            processing_pending=pending,
        )


class _TxAdapter:
    """Legacy helper retained for callers that need Database-like routing."""

    def __init__(self, db: Database, tx) -> None:
        self._db = db
        self._tx = tx
        self.conn = tx

    def __getattr__(self, name):
        return getattr(self._db, name)


def _has_observations(db: Database, plan_id: str) -> bool:
    return (
        db.query_one(
            "SELECT COUNT(*) c FROM job_observations o JOIN scrape_requests q ON q.id = o.request_id"
            " WHERE q.run_source_plan_id=?",
            (plan_id,),
        )["c"]
        > 0
    )


def _invalid_class_failure(page_class: PageClass, envelope) -> tuple[str, bool] | None:
    """Map invalid page classes to typed failures (login != parser failure)."""
    mapping = {
        PageClass.LOGIN_REQUIRED: ("AUTH_REQUIRED", False),
        PageClass.AUTH_EXPIRED: ("AUTH_REQUIRED", False),
        PageClass.RATE_LIMITED: ("RATE_LIMIT", True),
        PageClass.CHALLENGE_PAGE: ("CHALLENGE", False),
        PageClass.JS_SHELL: ("UNSUPPORTED", False),
        PageClass.NOT_FOUND: ("HTTP_4XX", False),
        PageClass.JOB_CLOSED: ("HTTP_4XX", False),
        PageClass.UNEXPECTED_REDIRECT: ("UNEXPECTED_CONTENT", True),
        PageClass.UNEXPECTED_CONTENT: ("UNEXPECTED_CONTENT", True),
        PageClass.UNKNOWN: ("UNSUPPORTED", True),
    }
    if page_class == PageClass.EMPTY:
        return None  # recognized empty -> success-empty path
    return mapping.get(page_class, ("UNSUPPORTED", True))


def _failure_to_operational_state(failure_kind: str | None) -> str | None:
    if failure_kind in ("AUTH_REQUIRED",):
        return "NEEDS_LOGIN"
    if failure_kind in ("RATE_LIMIT",):
        return "RATE_LIMITED"
    if failure_kind in ("CHALLENGE", "BLOCKED"):
        return "CHALLENGED"
    if failure_kind in ("PARSE_EMPTY", "PARSE_MARKER_MISSING", "SOURCE_CHANGED", "INVALID_JOB_RECORD"):
        return "BROKEN"
    if failure_kind in ("DNS_ERROR", "CONNECT_ERROR", "TLS_ERROR", "TIMEOUT", "HTTP_5XX"):
        return "DEGRADED"
    return None


def _host_of(config_snapshot: dict, source_id: str) -> str:
    if config_snapshot.get("host"):
        return config_snapshot["host"]
    return source_id
