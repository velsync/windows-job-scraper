"""Service-owned run driver (S1.10; 03 RUN-09, ARC-05).

The service process is the authoritative durable claim and capacity
coordinator. For each claimed acquisition request it:

1. builds the ExecutionPlanEnvelope from the immutable RunSourcePlan and
   the adapter's RequestPlan (host validation before any I/O);
2. executes the fetch with NO transaction held (03 §50: no network wait
   inside DB write transactions);
3. classifies page validity — invalid classes never reach the parser;
4. under the fenced commit: persists fetch/parse attempt evidence,
   ingests every observation through the provenance spine, records
   coverage seen identities, and advances or terminates the cursor;
5. enqueues the deterministic continuation (next page) when the cursor
   proposes one;
6. drains host-native obligations and aggregates the run (RUN-01).

Destination policy is host-owned: the allowed host set is derived from
the source's own entry URL. A narrow loopback grant is issued ONLY when
the source's entry host is itself a loopback literal — a host rule in
this driver, constructible by neither imported configuration nor
scraped content (04 §5.1).
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3

from jobscraper.acquisition.envelope import (
    ExecutionPlanEnvelope,
    RequestPlan,
    validate_envelope,
)
from jobscraper.acquisition.failures import FailureKind
from jobscraper.acquisition.httpexec import execute_request
from jobscraper.acquisition.origin import resolve_origin
from jobscraper.acquisition.pagevalidity import classify_page
from jobscraper.adapters.contract import (
    NORMAL_PARSE_CLASSES,
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    ParseContext,
    PlanningContext,
    ValidatedResultEnvelope,
)
from jobscraper.adapters.feed_api import FeedApiAdapter, FeedApiConfig
from jobscraper.adapters.registry import get_adapter
from jobscraper.ids import new_id
from jobscraper.net.destination import (
    DestinationPolicy,
    InternalGrant,
)
from jobscraper.net.urlnorm import normalize_url
from jobscraper.pipeline.coverage import (
    finalize_coverage,
    open_coverage,
    record_seen_identity,
)
from jobscraper.pipeline.ingest import ingest_observation
from jobscraper.pipeline.normalize import NORMALIZATION_VERSION
from jobscraper.pipeline.obligations import drain_all_obligations
from jobscraper.runtime.cancellation import (
    abandon_request_for_cancellation,
    run_is_cancelled,
)
from jobscraper.runtime.claims import StaleOwnership, claim_next_request, reclaim_expired
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.requests import (
    ACQUISITION_REQUEST_TYPES,
    enqueue_request,
)
from jobscraper.runtime.runs import aggregate_run, mark_run_started, set_group_outcome

MAX_PAGES_PER_RUN = 50


def source_policy(source_row: sqlite3.Row) -> DestinationPolicy:
    """Host-owned destination policy for one source (04 §5.1)."""
    try:
        normalized = normalize_url(source_row["entry_url"])
        host = normalized.host or ""
    except Exception:
        host = ""
    grant = None
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        is_loopback = literal.is_loopback
    except ValueError:
        is_loopback = False
    if is_loopback:
        # Narrow host rule: only a source whose own entry host is a loopback
        # literal gets a loopback grant for that exact host. Scraped content
        # and imported configuration can never construct a grant.
        grant = InternalGrant(
            purpose="loopback-source-entry", allowed_hosts=frozenset({host})
        )
    return DestinationPolicy(
        allowed_hosts=frozenset({host}) if host else None,
        internal_grant=grant,
        max_redirects=3,
        timeout_s=30.0,
        max_bytes=2_000_000,
    )


def _plan_config(conn: sqlite3.Connection, plan_row: sqlite3.Row) -> dict:
    row = conn.execute(
        "SELECT config_json FROM source_adapter_binding_revisions WHERE id = ?",
        (plan_row["binding_revision_id"],),
    ).fetchone()
    return json.loads(row["config_json"]) if row and row["config_json"] else {}


def _load_cursor(conn: sqlite3.Connection, plan_row: sqlite3.Row) -> CrawlCursor | None:
    row = conn.execute(
        """
        SELECT * FROM crawl_cursors
        WHERE binding_id = ? AND adapter_id = ? AND adapter_version = ?
          AND cursor_schema_version = ?
        """,
        (
            plan_row["binding_id"],
            plan_row["adapter_id"],
            plan_row["adapter_version"],
            plan_row["cursor_schema_version"],
        ),
    ).fetchone()
    if row is None:
        return None
    return CrawlCursor(
        source_id=row["source_id"],
        binding_id=row["binding_id"],
        adapter_id=row["adapter_id"],
        adapter_version=row["adapter_version"],
        cursor_schema_version=row["cursor_schema_version"],
        state_json=row["state_json"],
        checkpoint_at=row["checkpoint_at"],
    )


def _save_cursor(
    conn: sqlite3.Connection, plan_row: sqlite3.Row, cursor: CrawlCursor, now: str
) -> None:
    conn.execute(
        """
        INSERT INTO crawl_cursors (id, source_id, binding_id, adapter_id,
            adapter_version, cursor_schema_version, state_json, checkpoint_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (binding_id, adapter_id, adapter_version, cursor_schema_version)
        DO UPDATE SET state_json = excluded.state_json,
                      checkpoint_at = excluded.checkpoint_at
        """,
        (
            new_id("cur"),
            cursor.source_id or plan_row["source_id"],
            cursor.binding_id or plan_row["binding_id"],
            cursor.adapter_id,
            cursor.adapter_version,
            cursor.cursor_schema_version,
            cursor.state_json,
            now,
        ),
    )


def execute_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    worker_id: str = "service",
    now: str | None = None,
) -> str:
    """Drive one run to completion (bounded) and return its aggregate status."""
    from jobscraper.runtime.clock import db_utc_now

    ts = now or db_utc_now(conn)
    mark_run_started(conn, run_id, now=ts)
    plan_rows = conn.execute(
        "SELECT * FROM run_source_plans WHERE run_id = ? ORDER BY source_plan_group_id, fallback_rank",
        (run_id,),
    ).fetchall()

    for plan_row in plan_rows:
        _execute_plan(conn, run_id, plan_row, worker_id=worker_id, now=ts)

    # Host-native obligations drain before the run finalizes (a run must
    # not report finished while accepted observations are unprocessed).
    drain_all_obligations(conn, now=ts)
    _update_run_counters(conn, run_id)
    return aggregate_run(conn, run_id, now=ts)


def _update_run_counters(conn: sqlite3.Connection, run_id: str) -> None:
    """Honest run accounting derived from persisted evidence (§16.1)."""
    conn.execute(
        """
        UPDATE scrape_runs SET
            jobs_discovered = (SELECT COUNT(*) FROM job_observations WHERE run_id = ?),
            jobs_saved = (
                SELECT COUNT(DISTINCT js.job_id) FROM job_sources js
                JOIN jobs j ON j.id = js.job_id
                WHERE js.last_observation_id IN
                    (SELECT id FROM job_observations WHERE run_id = ?)
                  AND j.created_at >= COALESCE(
                      (SELECT started_at FROM scrape_runs WHERE id = ?), j.created_at)
            ),
            jobs_updated = (
                SELECT COUNT(DISTINCT js.job_id) FROM job_sources js
                JOIN jobs j ON j.id = js.job_id
                WHERE js.last_observation_id IN
                    (SELECT id FROM job_observations WHERE run_id = ?)
                  AND j.created_at < COALESCE(
                      (SELECT started_at FROM scrape_runs WHERE id = ?), j.created_at)
            ),
            requests_total = (SELECT COUNT(*) FROM scrape_requests WHERE run_id = ?),
            requests_failed = (SELECT COUNT(*) FROM scrape_requests
                               WHERE run_id = ? AND status = 'FAILED')
        WHERE id = ?
        """,
        (run_id, run_id, run_id, run_id, run_id, run_id, run_id, run_id),
    )
    conn.commit()


def _execute_plan(
    conn: sqlite3.Connection,
    run_id: str,
    plan_row: sqlite3.Row,
    *,
    worker_id: str,
    now: str,
) -> None:
    from jobscraper.runtime.clock import db_utc_now

    source = conn.execute(
        "SELECT * FROM sources WHERE id = ?", (plan_row["source_id"],)
    ).fetchone()
    policy = source_policy(source)
    config = _plan_config(conn, plan_row)
    if plan_row["adapter_id"] != "json_api_feed":
        raise ValueError(f"no builtin adapter for {plan_row['adapter_id']!r}")
    adapter = FeedApiAdapter(FeedApiConfig(**config))

    coverage_id = open_coverage(
        conn,
        run_source_plan_id=plan_row["id"],
        source_id=plan_row["source_id"],
        binding_id=plan_row["binding_id"],
        scope_key="full-source",
        generation_key=f"run-{run_id}",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        now=now,
    )

    terminal = False
    pages = 0
    outcome = None
    while pages < MAX_PAGES_PER_RUN:
        if run_is_cancelled(conn, run_id):
            # §18: no new acquisition driving once cancellation is durable
            outcome = "CANCELLED"
            break
        claim = claim_next_request(
            conn, worker_id, now=db_utc_now(conn), types=ACQUISITION_REQUEST_TYPES,
            run_source_plan_id=plan_row["id"],
        )
        if claim is None:
            break
        ts = db_utc_now(conn)
        conn.execute(
            "INSERT INTO coverage_contributing_request (coverage_id, request_id)"
            " VALUES (?, ?) ON CONFLICT DO NOTHING",
            (coverage_id, claim.request_id),
        )
        conn.commit()

        cursor = _load_cursor(conn, plan_row)
        task = AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={})
        # 02 ACQ-09: planning receives the host-resolved pins, never mutable
        # host state.  Snapshots that do not exist durably yet stay None —
        # the driver does not fabricate references (Slice 3 frontier/budgets).
        planning_ctx = PlanningContext(
            run_id=run_id,
            run_source_plan_id=plan_row["id"],
            source_snapshot_ref=f"source://{plan_row['source_id']}",
            binding_revision_id=plan_row["binding_revision_id"],
            permission_profile_revision=plan_row["permission_profile_revision"],
            cursor_schema_version=plan_row["cursor_schema_version"],
        )
        request_plan = adapter.plan(task, cursor, ctx=planning_ctx)
        envelope = ExecutionPlanEnvelope(
            plan_id=new_id("plan"),
            request_id=claim.request_id,
            attempt_id=claim.attempt_id,
            run_id=run_id,
            run_source_plan_id=plan_row["id"],
            source_id=plan_row["source_id"],
            binding_id=plan_row["binding_id"],
            binding_revision_id=plan_row["binding_revision_id"],
            adapter_id=plan_row["adapter_id"],
            adapter_version=plan_row["adapter_version"],
            strategy=plan_row["strategy"],
            execution_class=plan_row["execution_class"],
            policy_snapshot_ref=None,
            permission_profile_id=plan_row["permission_profile_id"],
            permission_profile_revision=plan_row["permission_profile_revision"],
            payload_kind="REQUEST",
            payload=RequestPlan(
                method=request_plan.method,
                url=request_plan.url,
                headers=dict(request_plan.headers),
                expected_content_types=tuple(request_plan.expected_content_types),
                timeout_s=request_plan.timeout_s,
                max_bytes=request_plan.max_bytes,
                purpose="LIST_FETCH",
            ),
        )

        result = execute_request(envelope, policy)  # NO transaction held
        classification = classify_page(result, expect="LIST")

        signal: dict = {}

        def mutate(cursor_conn):
            fetch_attempt_id = _persist_fetch_attempt(cursor_conn, envelope, result, ts)
            _record_evidence(
                cursor_conn,
                request_id=claim.request_id,
                attempt_id=claim.attempt_id,
                fetch_attempt_id=fetch_attempt_id,
                kind="RESULT_ENVELOPE",
                ref=result.body_ref,
                detail=result.as_evidence(),
                content_hash=result.normalized_content_hash,
                now=ts,
            )
            if result.security_policy_result != "ALLOWED":
                # the denial itself is durable evidence (04 §5.1): an empty
                # result must always be explainable as a policy outcome
                _record_evidence(
                    cursor_conn,
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    fetch_attempt_id=fetch_attempt_id,
                    kind="SECURITY_POLICY",
                    ref=result.security_policy_result,
                    detail={"result": result.security_policy_result,
                            "requested_url": result.requested_url},
                    content_hash=None,
                    now=ts,
                )
            cursor_conn.execute(
                "UPDATE scrape_requests SET page_class = ? WHERE id = ?",
                (classification.state.value, claim.request_id),
            )
            _record_evidence(
                cursor_conn,
                request_id=claim.request_id,
                attempt_id=claim.attempt_id,
                fetch_attempt_id=fetch_attempt_id,
                kind="PAGE_VALIDITY",
                ref=f"validity://{classification.state.value}",
                detail=dict(classification.evidence),
                content_hash=result.normalized_content_hash,
                now=ts,
            )
            # EMPTY is a recognized non-job outcome (§21): the adapter parse
            # yields SUCCESS_EMPTY, which terminates the enumeration
            # authoritatively (ACQ-02).
            if classification.state in NORMAL_PARSE_CLASSES:
                validated = ValidatedResultEnvelope(
                    envelope=result,
                    page_class=classification.state,
                    validation_evidence=classification.evidence,
                    security_policy_result=result.security_policy_result,
                    cache_representation_ref=result.cache_representation_ref,
                )
                parse_ctx = ParseContext(
                    request_id=claim.request_id,
                    attempt_id=claim.attempt_id,
                    run_source_plan_id=plan_row["id"],
                    parser_version=plan_row["adapter_version"],
                    normalization_version=NORMALIZATION_VERSION,
                    idempotency_namespace=claim.request_id,
                )
                outcome_obj = adapter.parse(task, validated, ctx=parse_ctx)
                parse_attempt_id = _persist_parse_attempt(
                    cursor_conn, envelope, outcome_obj, validated, ts
                )
                for observation in outcome_obj.observations:
                    # 02 §32 origin resolution — host-owned and network-inert:
                    # it consumes the recorded redirect chain plus the
                    # observation's own link candidates.
                    origin = resolve_origin(
                        discovery_url=source["entry_url"] if source else None,
                        raw_source_url=observation.raw_url,
                        canonical_job_url=observation.canonical_url_candidate,
                        application_url=observation.application_url_candidate,
                        redirect_chain=result.redirect_chain,
                        final_url=result.final_url,
                        observed_at=ts,
                    )
                    ingest_observation(
                        cursor_conn,
                        request_id=claim.request_id,
                        attempt_id=claim.attempt_id,
                        observation=observation,
                        source_id=plan_row["source_id"],
                        binding_id=plan_row["binding_id"],
                        adapter_id=plan_row["adapter_id"],
                        adapter_version=plan_row["adapter_version"],
                        strategy=plan_row["strategy"],
                        execution_class=plan_row["execution_class"],
                        observed_at=ts,
                        now=ts,
                        fetch_attempt_id=fetch_attempt_id,
                        parse_attempt_id=parse_attempt_id,
                        origin=origin,
                    )
                    if observation.source_job_id:
                        record_seen_identity(
                            cursor_conn, coverage_id, observation.source_job_id,
                            evidence_ref=observation.raw_url, commit=False,
                        )
                if outcome_obj.kind.value == "SUCCESS_EMPTY":
                    signal["value"] = "EMPTY"
                    return
                next_cursor = adapter.next_cursor(task, outcome_obj, cursor, ctx=parse_ctx)
                if next_cursor is not None:
                    _save_cursor(cursor_conn, plan_row, next_cursor, ts)
                    enqueue_request(
                        cursor_conn,
                        run_id=run_id,
                        run_source_plan_id=plan_row["id"],
                        source_id=plan_row["source_id"],
                        binding_id=plan_row["binding_id"],
                        request_type="LIST_FETCH",
                        target_identity=request_plan.url,
                        logical_key=next_cursor.state_json,
                        commit=False,
                    )
                else:
                    signal["value"] = "EMPTY"
                    return
                signal["value"] = "JOBS"
                return
            signal["value"] = "INVALID"

        try:
            with fenced_commit(
                conn, claim.request_id, claim.attempt_id, now=ts, mutate=mutate
            ):
                pass
        except StaleOwnership:
            if run_is_cancelled(conn, run_id):
                # §18: the fence refused a late acquisition commit; this
                # page's outputs were rolled back, the worker cooperatively
                # abandons the request, and the plan ends CANCELLED.
                abandon_request_for_cancellation(
                    conn, claim.request_id, now=db_utc_now(conn)
                )
                outcome = "CANCELLED"
                break
            # Ownership lost without cancellation (expired lease): an
            # expired lease is already lost ownership (RUN-07), so the
            # expired request is reclaimed for retry and the driver stops
            # claiming this plan rather than reviving the dead lease.
            reclaim_expired(conn)
            break

        pages += 1
        if signal.get("value") == "EMPTY":
            terminal = True
            outcome = "SATISFIED"
            break
        if signal.get("value") == "INVALID":
            outcome = "SATISFIED_PARTIAL"
            break

    if outcome is None:
        outcome = "SATISFIED_PARTIAL" if pages else "FAILED"

    stop_reason = (
        "terminal cursor"
        if terminal
        else "cancelled"
        if outcome == "CANCELLED"
        else "driver stop"
    )
    try:
        finalize_coverage(
            conn,
            coverage_id,
            completion_state="COMPLETE" if terminal else "PARTIAL",
            stop_reason=stop_reason,
            terminal_enumeration_proven=terminal,
            pages_completed=pages,
            now=db_utc_now(conn),
        )
    except Exception:
        # Coverage finalization must never block the run outcome.
        finalize_coverage(
            conn,
            coverage_id,
            completion_state="PARTIAL",
            stop_reason="driver stop",
            terminal_enumeration_proven=False,
            pages_completed=pages,
            now=db_utc_now(conn),
        )
    set_group_outcome(conn, plan_row["id"], outcome, now=db_utc_now(conn))


def _record_evidence(
    conn,
    *,
    request_id: str,
    attempt_id: str | None,
    fetch_attempt_id: str | None = None,
    parse_attempt_id: str | None = None,
    observation_id: str | None = None,
    kind: str,
    ref: str | None,
    detail: dict,
    content_hash: str | None,
    now: str,
) -> str:
    """One durable evidence row (03 §30): hashes, refs and redacted metadata."""
    evidence_id = new_id("ev")
    conn.execute(
        """
        INSERT INTO acquisition_evidence (
            id, request_id, attempt_id, fetch_attempt_id, parse_attempt_id,
            observation_id, kind, ref, detail_json, content_hash, observed_at,
            created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            request_id,
            attempt_id,
            fetch_attempt_id,
            parse_attempt_id,
            observation_id,
            kind,
            ref,
            json.dumps(detail, sort_keys=True, default=str)[:60000],
            content_hash,
            now,
            now,
        ),
    )
    return evidence_id


def _persist_fetch_attempt(conn, envelope, result, ts) -> str:
    """Durable fetch attempt for one ResultEnvelope (02 §11.3, 03 §30)."""
    fetch_attempt_id = new_id("fa")
    conn.execute(
        """
        INSERT INTO fetch_attempts (
            id, attempt_id, request_id, requested_url, final_url, status_code,
            content_type, body_hash, normalized_content_hash, body_ref,
            bytes_downloaded, duration_ms, redirect_chain_json, was_304,
            failure_kind, failure_json, fetched_at,
            contract_version, execution_plan_id, headers_redacted_json,
            validators_sent_json, robots_decision, transport, browser_used,
            resource_blocking_applied, security_policy_json,
            structured_payload_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 ?, ?, ?, ?, ?, ?)
        """,
        (
            fetch_attempt_id,
            envelope.attempt_id,
            envelope.request_id,
            result.requested_url,
            result.final_url,
            result.status_code,
            result.content_type,
            result.body_hash,
            result.normalized_content_hash,
            result.body_ref,
            result.bytes_downloaded,
            result.duration_ms,
            json.dumps(result.redirect_chain),
            1 if result.was_304 else 0,
            result.failure.kind.value if result.failure else None,
            json.dumps(result.failure.as_dict()) if result.failure else None,
            ts,
            result.contract_version,
            envelope.plan_id,
            json.dumps(dict(result.headers_redacted), sort_keys=True, default=str),
            json.dumps(sorted(result.validators_sent)),
            result.robots_decision,
            result.transport,
            1 if result.browser_used else 0,
            1 if result.resource_blocking_applied else 0,
            json.dumps(
                {
                    "result": result.security_policy_result,
                    "policy_snapshot_ref": envelope.policy_snapshot_ref,
                    "permission_profile_revision": envelope.permission_profile_revision,
                },
                sort_keys=True,
                default=str,
            ),
            result.structured_payload_ref,
        ),
    )
    return fetch_attempt_id


def _persist_parse_attempt(conn, envelope, outcome, validated, ts) -> str:
    """Durable parse attempt under the ACQ-09 contract (02 §11.3, 03 §30)."""
    parse_attempt_id = new_id("pa")
    conn.execute(
        """
        INSERT INTO parse_attempts (
            id, attempt_id, request_id, parser_kind, parser_version,
            outcome_kind, observation_count, child_task_count, failure_kind,
            failure_json, parsed_at, contract_version, validated_page_class,
            validation_evidence_json, result_envelope_ref,
            cursor_proposal_json, coverage_proposal_json,
            continuation_required, closure_evidence_json, review_evidence_json,
            evidence_refs_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            parse_attempt_id,
            envelope.attempt_id,
            envelope.request_id,
            envelope.adapter_id,
            envelope.adapter_version,
            outcome.kind.value,
            len(outcome.observations),
            len(outcome.discovered_tasks),
            outcome.failure.kind.value if outcome.failure else None,
            json.dumps(outcome.failure.as_dict()) if outcome.failure else None,
            ts,
            outcome.contract_version,
            validated.validated_page_class.value,
            json.dumps(dict(validated.validation_evidence), sort_keys=True, default=str),
            validated.result_envelope_ref,
            # already serialized JSON text (CrawlCursor.state_json)
            outcome.cursor_proposal.state_json
            if outcome.cursor_proposal is not None
            else None,
            json.dumps(outcome.coverage_proposal, sort_keys=True, default=str)
            if outcome.coverage_proposal
            else None,
            1 if outcome.continuation_required else 0,
            json.dumps([dict(e) for e in outcome.closure_or_missing_evidence], default=str),
            json.dumps([dict(e) for e in outcome.review_evidence], default=str),
            json.dumps(list(outcome.evidence_refs), default=str),
        ),
    )
    return parse_attempt_id


__all__ = ["execute_run", "source_policy"]
