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
from jobscraper.acquisition.pagevalidity import PageClass, classify_page
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    ValidatedResult,
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
        request_plan = adapter.plan(task, cursor, ctx=None)
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
            _persist_fetch_attempt(cursor_conn, envelope, result, ts)
            cursor_conn.execute(
                "UPDATE scrape_requests SET page_class = ? WHERE id = ?",
                (classification.state.value, claim.request_id),
            )
            # EMPTY is a recognized non-job outcome (§21): the adapter parse
            # yields SUCCESS_EMPTY, which terminates the enumeration
            # authoritatively (ACQ-02).
            if classification.state in (
                PageClass.VALID_LIST,
                PageClass.VALID_JOB,
                PageClass.EMPTY,
            ):
                validated = ValidatedResult(
                    envelope=result,
                    page_class=classification.state,
                    validation_evidence=classification.evidence,
                )
                outcome_obj = adapter.parse(task, validated, ctx=None)
                _persist_parse_attempt(cursor_conn, envelope, outcome_obj, ts)
                for observation in outcome_obj.observations:
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
                    )
                    if observation.source_job_id:
                        record_seen_identity(
                            cursor_conn, coverage_id, observation.source_job_id,
                            evidence_ref=observation.raw_url, commit=False,
                        )
                if outcome_obj.kind.value == "SUCCESS_EMPTY":
                    signal["value"] = "EMPTY"
                    return
                next_cursor = adapter.next_cursor(task, outcome_obj, cursor, ctx=None)
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


def _persist_fetch_attempt(conn, envelope, result, ts) -> None:
    conn.execute(
        """
        INSERT INTO fetch_attempts (id, attempt_id, request_id, requested_url,
            final_url, status_code, content_type, body_hash, bytes_downloaded,
            duration_ms, redirect_chain_json, was_304, failure_kind, failure_json,
            fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("fa"),
            envelope.attempt_id,
            envelope.request_id,
            result.requested_url,
            result.final_url,
            result.status_code,
            result.content_type,
            result.body_hash,
            result.bytes_downloaded,
            result.duration_ms,
            json.dumps(result.redirect_chain),
            1 if result.was_304 else 0,
            result.failure.kind.value if result.failure else None,
            json.dumps(result.failure.as_dict()) if result.failure else None,
            ts,
        ),
    )


def _persist_parse_attempt(conn, envelope, outcome, ts) -> None:
    conn.execute(
        """
        INSERT INTO parse_attempts (id, attempt_id, request_id, parser_kind,
            parser_version, outcome_kind, observation_count, failure_kind,
            failure_json, parsed_at)
        VALUES (?, ?, ?, 'feed_api', '1.0.0', ?, ?, ?, ?, ?)
        """,
        (
            new_id("pa"),
            envelope.attempt_id,
            envelope.request_id,
            outcome.kind.value,
            len(outcome.observations),
            outcome.failure.kind.value if outcome.failure else None,
            json.dumps(outcome.failure.as_dict()) if outcome.failure else None,
            ts,
        ),
    )


__all__ = ["execute_run", "source_policy"]
