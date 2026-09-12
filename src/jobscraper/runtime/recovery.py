"""Restart recovery for durable requests (03 §16/§18, RUN-07/RUN-09/RUN-19).

The service process is the single-machine claim/capacity coordinator
(RUN-09): request ownership lives inside that one process. A service restart
therefore orphans every prior-epoch ``RUNNING`` request, and startup recovery
runs under the fresh service epoch opened by ``clock.begin_service_epoch``.

S3.12 turns the original request-reclaim scaffold into one deterministic,
network-inert startup reconciliation. Before the coordinator/listener can
start it:

* reclaims impossible prior-epoch ownership with the normal RUN-07 attempt
  history/budget transition;
* makes service-restart-reclaimed local obligations immediately due (they are
  idempotent host-native work and carry no source-pressure cooldown), then
  drains them through the ordinary ownership fence;
* resumes durable cancellation sweeps even when the crash happened before any
  request was claimed;
* reconciles unfinished coverage only from durable proof: COMPLETE is retried
  only when a successful terminal parse is durably provable and the S3.8
  barrier accepts it; otherwise closed work is finalized conservatively as
  PARTIAL/FAILED, never as absence-authoritative COMPLETE;
* repairs the active plan/group result from finalized coverage through the
  existing S3.9 ``set_group_outcome`` owner, which performs exactly-once
  fallback activation;
* validates every active resumable acquisition plan against its exact pinned
  historical binding config + installed adapter/version/API/cursor schema, and
  validates any stored cursor through the existing compatibility gate;
* re-aggregates runs after local work and group truth are repaired;
* never replays ``SUCCEEDED`` source-network requests, never mutates finalized
  coverage, never resets a cursor, and never substitutes mutable adapter pins.

All repairs are replay-safe. If the service crashes again during recovery, the
next fresh epoch can run the same reconciliation again without duplicating
accepted outputs or skipping a fallback rank.
"""

from __future__ import annotations

import json
import sqlite3

from jobscraper.pipeline.coverage import (
    CoverageFinalizationError,
    finalize_coverage,
    open_or_resume_coverage,
)
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.claims import (
    ABANDONED_SERVICE_RESTART,
    _abandon_and_requeue,
)
from jobscraper.runtime.clock import (
    NoActiveServiceEpoch,
    current_service_epoch,
    db_utc_now,
)
from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES
from jobscraper.runtime.runs import (
    aggregate_run,
    plan_actionable_open_work,
    set_group_outcome,
)

_ENUMERATION_REQUEST_TYPES = frozenset({"LIST_FETCH", "SOURCE_CRAWL"})
_META_CRAWL_ROLES = frozenset({"ROBOTS", "SITEMAP"})


def _timestamp(conn: sqlite3.Connection, now: str | None) -> str:
    return now or db_utc_now(conn)


def _payload_role(payload_json: str | None) -> str:
    try:
        value = json.loads(payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(value, dict):
        return ""
    return str(value.get("role") or "").upper()


def recover_interrupted_requests(
    conn: sqlite3.Connection, *, now: str | None = None
) -> dict:
    """Reclaim requests orphaned by a service restart and finalize
    interrupted cancellations. Returns ``{"reclaimed": [...],
    "finalized_cancelled_runs": [...]}``.

    This S1-compatible primitive deliberately keeps its historical report
    shape. S3.12 startup orchestration is provided by
    :func:`recover_startup_state`.
    """
    from jobscraper.pipeline.obligations import OBLIGATION_TYPES

    orphaned = conn.execute(
        """
        SELECT id, run_id, request_type, current_attempt_id, attempt_count, max_attempts
        FROM scrape_requests
        WHERE status = 'RUNNING'
        ORDER BY created_at, id
        """
    ).fetchall()

    reclaimed: list[str] = []
    affected_runs: set[str] = set()
    for row in orphaned:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = _timestamp(conn, now)
            # Re-check under the write lock: the legacy helper is also used by
            # tests/direct callers where another connection might have closed
            # the request after the initial scan.
            current = conn.execute(
                "SELECT id, status, request_type, current_attempt_id,"
                " attempt_count, max_attempts"
                " FROM scrape_requests WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if current is None or current["status"] != "RUNNING":
                conn.execute("COMMIT")
                continue
            new_status = _abandon_and_requeue(
                conn,
                current,
                ts=ts,
                abandoned_reason=ABANDONED_SERVICE_RESTART,
                failure_detail="attempt budget exhausted (service restart)",
            )
            if (
                new_status == "RETRY_WAIT"
                and current["request_type"] in OBLIGATION_TYPES
            ):
                # S3.12 idempotency: local obligations are deterministic,
                # network-inert accepted work. Make them due in the SAME
                # transaction that abandons the dead owner. A second crash
                # immediately after this commit therefore cannot strand the
                # obligation behind generic source-style backoff when the
                # in-memory `reclaimed` list is lost.
                conn.execute(
                    "UPDATE scrape_requests SET next_retry_at = ?, updated_at = ?"
                    " WHERE id = ? AND status = 'RETRY_WAIT'",
                    (ts, ts, current["id"]),
                )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:  # pragma: no cover - already rolled back
                pass
            raise
        reclaimed.append(row["id"])
        affected_runs.add(row["run_id"])

    finalized: list[str] = []
    for run_id in sorted(affected_runs):
        row = conn.execute(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None or row["cancel_requested_at"] is None:
            continue
        request_run_cancellation(conn, run_id, now=now)
        if aggregate_run(conn, run_id, now=now) is not None:
            finalized.append(run_id)

    return {"reclaimed": reclaimed, "finalized_cancelled_runs": finalized}


def _release_reclaimed_local_retries(
    conn: sqlite3.Connection,
    reclaimed: list[str],
    *,
    now: str | None,
) -> list[str]:
    """Report/reassert restart-reclaimed S3.11 local obligations as due.

    The reclaim transaction itself already moves deterministic local
    obligations to ``next_retry_at = reclaim_time`` so a second crash cannot
    strand them. This follow-up is intentionally idempotent: it preserves the
    historical report surface and reasserts the same due timestamp for the
    requests reclaimed in the current uninterrupted startup pass. Source
    acquisition retries/cooldowns are never shortened.
    """
    if not reclaimed:
        return []

    from jobscraper.pipeline.obligations import OBLIGATION_TYPES

    conn.execute("BEGIN IMMEDIATE")
    try:
        ts = _timestamp(conn, now)
        ids = ",".join("?" for _ in reclaimed)
        types = ",".join("?" for _ in sorted(OBLIGATION_TYPES))
        rows = conn.execute(
            "SELECT id FROM scrape_requests"
            f" WHERE id IN ({ids})"
            f" AND request_type IN ({types})"
            " AND status = 'RETRY_WAIT'"
            " ORDER BY created_at, id",
            (*reclaimed, *sorted(OBLIGATION_TYPES)),
        ).fetchall()
        released = [str(row["id"]) for row in rows]
        if released:
            release_ids = ",".join("?" for _ in released)
            conn.execute(
                "UPDATE scrape_requests SET next_retry_at = ?, updated_at = ?"
                f" WHERE id IN ({release_ids}) AND status = 'RETRY_WAIT'",
                (ts, ts, *released),
            )
        conn.execute("COMMIT")
        return released
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _resume_cancelled_runs(
    conn: sqlite3.Connection, *, now: str | None
) -> list[str]:
    """Finish cancellation sweeps that were durable but incomplete at crash."""
    rows = conn.execute(
        """
        SELECT id
          FROM scrape_runs
         WHERE cancel_requested_at IS NOT NULL
           AND status IN ('QUEUED', 'RUNNING')
         ORDER BY created_at, id
        """
    ).fetchall()

    finalized: list[str] = []
    for row in rows:
        run_id = str(row["id"])
        request_run_cancellation(conn, run_id, now=now)
        if aggregate_run(conn, run_id, now=now) == "CANCELLED":
            finalized.append(run_id)
    return finalized


def _validate_resumable_plan_contract(
    conn: sqlite3.Connection, plan_id: str
) -> bool:
    """Resolve one resumable plan through its exact immutable code/config pins.

    Mutable binding promotion is deliberately ignored: configuration comes from
    the plan's historical ``binding_revision_id``.  Recovery first proves that
    this historical binding revision and adapter definition still resolve the
    exact immutable execution pins captured by the RunSourcePlan, then proves
    that the installed built-in identifies itself as the same adapter/version/
    API/cursor schema. Existing cursor state is finally loaded through the S3.5
    compatibility gate.

    Any mismatch is an explicit startup recovery failure rather than a silent
    substitution of newer parser/cursor/security semantics. Returns the pinned
    implementation's ``listing_identity_sufficient`` bit for S3.8 unfinished-
    generation validation.
    """
    from jobscraper.acquisition.crawler.cursor import (
        CursorCompatibilityError,
        load_cursor,
    )
    from jobscraper.adapters.registry import build_adapter

    plan = conn.execute(
        "SELECT * FROM run_source_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if plan is None:
        raise RuntimeError(f"resumable RunSourcePlan {plan_id!r} no longer resolves")

    required_pins = (
        "binding_id",
        "binding_revision_id",
        "adapter_id",
        "adapter_version",
        "adapter_api_version",
        "strategy",
        "execution_class",
        "permission_profile_id",
        "permission_profile_revision",
        "cursor_schema_version",
    )
    missing = [
        name
        for name in required_pins
        if plan[name] is None or (isinstance(plan[name], str) and not plan[name].strip())
    ]
    if missing:
        raise RuntimeError(
            f"resumable RunSourcePlan {plan_id!r} is missing immutable pin(s): "
            + ", ".join(missing)
        )
    try:
        pinned_cursor_schema = int(plan["cursor_schema_version"])
        pinned_permission_revision = int(plan["permission_profile_revision"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"resumable RunSourcePlan {plan_id!r} has invalid numeric immutable pins"
        ) from exc

    revision = conn.execute(
        "SELECT binding_id, adapter_id, adapter_version, strategy, execution_class,"
        " permission_profile_id, permission_profile_revision, config_json"
        " FROM source_adapter_binding_revisions WHERE id = ?",
        (plan["binding_revision_id"],),
    ).fetchone()
    if revision is None:
        raise RuntimeError(
            f"resumable RunSourcePlan {plan_id!r} lost pinned binding revision "
            f"{plan['binding_revision_id']!r}"
        )

    revision_expected = {
        "binding_id": str(plan["binding_id"]),
        "adapter_id": str(plan["adapter_id"]),
        "adapter_version": str(plan["adapter_version"]),
        "strategy": str(plan["strategy"]),
        "execution_class": str(plan["execution_class"]),
        "permission_profile_id": str(plan["permission_profile_id"]),
        "permission_profile_revision": str(pinned_permission_revision),
    }
    revision_actual = {
        "binding_id": str(revision["binding_id"]),
        "adapter_id": str(revision["adapter_id"]),
        "adapter_version": str(revision["adapter_version"]),
        "strategy": str(revision["strategy"]),
        "execution_class": str(revision["execution_class"]),
        "permission_profile_id": str(revision["permission_profile_id"]),
        "permission_profile_revision": str(revision["permission_profile_revision"]),
    }
    revision_mismatches = [
        name
        for name in revision_expected
        if revision_actual[name] != revision_expected[name]
    ]
    if revision_mismatches:
        detail = ", ".join(
            f"{name}: pinned={revision_expected[name]!r} historical={revision_actual[name]!r}"
            for name in revision_mismatches
        )
        raise RuntimeError(
            f"resumable RunSourcePlan {plan_id!r} historical binding revision "
            f"conflicts with immutable pins: {detail}"
        )

    definition = conn.execute(
        "SELECT adapter_api_version FROM adapter_definitions"
        " WHERE adapter_id = ? AND adapter_version = ?",
        (plan["adapter_id"], plan["adapter_version"]),
    ).fetchone()
    if definition is None:
        raise RuntimeError(
            f"resumable RunSourcePlan {plan_id!r} lost historical adapter definition "
            f"{plan['adapter_id']}@{plan['adapter_version']}"
        )
    if str(definition["adapter_api_version"]) != str(plan["adapter_api_version"]):
        raise RuntimeError(
            f"resumable RunSourcePlan {plan_id!r} historical adapter API pin "
            f"conflicts: pinned={plan['adapter_api_version']!r} "
            f"historical={definition['adapter_api_version']!r}"
        )

    try:
        config = json.loads(revision["config_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"resumable RunSourcePlan {plan_id!r} has invalid pinned config_json"
        ) from exc
    if not isinstance(config, dict):
        raise RuntimeError(
            f"resumable RunSourcePlan {plan_id!r} pinned config is not an object"
        )
    try:
        adapter = build_adapter(str(plan["adapter_id"]), config)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"pinned adapter implementation unavailable for resumable "
            f"RunSourcePlan {plan_id!r}: {plan['adapter_id']}@{plan['adapter_version']}"
        ) from exc

    manifest = getattr(adapter, "manifest", None)
    actual = {
        "adapter_id": getattr(manifest, "id", None),
        "adapter_version": getattr(manifest, "version", None),
        "adapter_api_version": getattr(manifest, "adapter_api_version", None),
        "cursor_schema_version": getattr(manifest, "cursor_schema_version", None),
    }
    expected = {
        "adapter_id": str(plan["adapter_id"]),
        "adapter_version": str(plan["adapter_version"]),
        "adapter_api_version": str(plan["adapter_api_version"]),
        "cursor_schema_version": str(pinned_cursor_schema),
    }
    mismatches = [
        name
        for name in expected
        if str(actual[name]) != expected[name]
    ]
    supported_classes = tuple(
        str(value) for value in getattr(manifest, "supported_execution_classes", ())
    )
    if str(plan["execution_class"]) not in supported_classes:
        mismatches.append("execution_class")
    if mismatches:
        detail_parts = []
        for name in mismatches:
            if name == "execution_class":
                detail_parts.append(
                    f"execution_class: pinned={plan['execution_class']!r} "
                    f"installed_supported={supported_classes!r}"
                )
            else:
                detail_parts.append(
                    f"{name}: pinned={expected[name]!r} installed={actual[name]!r}"
                )
        raise RuntimeError(
            f"pinned adapter implementation incompatible for resumable "
            f"RunSourcePlan {plan_id!r}: " + ", ".join(detail_parts)
        )

    try:
        load_cursor(conn, run_source_plan_id=plan_id, plan_row=plan)
    except CursorCompatibilityError as exc:
        raise RuntimeError(
            f"pinned cursor incompatible for resumable RunSourcePlan {plan_id!r}: {exc}"
        ) from exc
    return bool(getattr(adapter, "listing_identity_sufficient", False))


def _validate_active_open_plans(conn: sqlite3.Connection) -> list[str]:
    """Fail closed on incompatible code/cursor before active source work resumes.

    Only the currently active fallback rank is validated here. Dormant ranks do
    not have execution authority yet; if S3.9 recovery activates one earlier in
    this startup pass, it becomes visible to this query and is validated before
    the coordinator/listener starts.
    """
    request_types = sorted(ACQUISITION_REQUEST_TYPES)
    placeholders = ",".join("?" for _ in request_types)
    rows = conn.execute(
        f"""
        SELECT p.id
          FROM run_source_plans p
          JOIN scrape_runs r ON r.id = p.run_id
          JOIN source_plan_group_state g
            ON g.run_id = p.run_id
           AND g.source_plan_group_id = p.source_plan_group_id
          JOIN sources s ON s.id = p.source_id
          JOIN source_adapter_bindings b ON b.id = p.binding_id
         WHERE r.status IN ('QUEUED', 'RUNNING')
           AND r.cancel_requested_at IS NULL
           AND p.fallback_rank = g.active_fallback_rank
           AND (g.group_outcome IS NULL OR g.group_outcome = 'SATISFIED_PARTIAL')
           AND s.desired_state = 'ENABLED' AND s.administrative_state = 'NORMAL'
           AND b.desired_state = 'ENABLED' AND b.administrative_state = 'NORMAL'
           AND EXISTS (
               SELECT 1 FROM scrape_requests req
                WHERE req.run_source_plan_id = p.id
                  AND req.request_type IN ({placeholders})
                  AND req.status IN ('PENDING', 'RUNNING', 'RETRY_WAIT')
           )
         ORDER BY p.run_id, p.source_plan_group_id, p.fallback_rank
        """,
        tuple(request_types),
    ).fetchall()
    validated: list[str] = []
    for row in rows:
        plan_id = str(row["id"])
        _validate_resumable_plan_contract(conn, plan_id)
        validated.append(plan_id)
    return validated


def _coverage_has_accepted_evidence(
    conn: sqlite3.Connection, coverage_id: str
) -> bool:
    """Whether one generation durably accepted provider/result evidence.

    This mirrors the driver's recovery-relevant distinction.  A pre-dispatch
    refusal can close a request without ever accepting provider output and may
    therefore fall back.  Once a fetch/parse/observation was fenced durably,
    however, the driver treats that acquisition pass as useful-but-incomplete
    even when the request itself ended FAILED; recovery must not silently
    convert that visible PARTIAL result into fallback just because the
    in-memory ``pages`` counter died with the process.

    Child rows alone are deliberately *not* evidence here. Host-owned
    ROBOTS/SITEMAP dependencies can be durable children of a page request
    before that page ever dispatches; counting those metadata rows would turn
    a true pre-dispatch failure into false ``SATISFIED_PARTIAL`` and suppress a
    legitimate fallback.
    """
    rows = conn.execute(
        """
        SELECT r.id AS request_id, r.status,
               EXISTS(SELECT 1 FROM fetch_attempts f
                       WHERE f.request_id = r.id) AS has_fetch,
               EXISTS(SELECT 1 FROM parse_attempts p
                       WHERE p.request_id = r.id) AS has_parse,
               EXISTS(SELECT 1 FROM job_observations o
                       WHERE o.request_id = r.id) AS has_observation
          FROM coverage_contributing_request c
          JOIN scrape_requests r ON r.id = c.request_id
         WHERE c.coverage_id = ?
        """,
        (coverage_id,),
    ).fetchall()
    return any(
        row["status"] == "SUCCEEDED"
        or bool(row["has_fetch"])
        or bool(row["has_parse"])
        or bool(row["has_observation"])
        for row in rows
    )


def _has_nonmeta_enumeration_child(conn: sqlite3.Connection, request_id: str) -> bool:
    children = conn.execute(
        "SELECT request_type, payload_json FROM scrape_requests"
        " WHERE parent_request_id = ?",
        (request_id,),
    ).fetchall()
    return any(
        row["request_type"] in _ENUMERATION_REQUEST_TYPES
        and _payload_role(row["payload_json"]) not in _META_CRAWL_ROLES
        for row in children
    )


def _durable_terminal_enumeration_proven(
    conn: sqlite3.Connection, coverage_id: str
) -> bool:
    """Conservative reconstruction of the in-memory driver's terminal bit.

    A terminal proof is accepted only from a SUCCEEDED contributing
    enumeration request whose successful attempt durably parsed a normal
    success, carries no durable host-degradation/refetch evidence, proposed no
    cursor, declared no continuation, and created no non-metadata enumeration
    child. False negatives merely produce PARTIAL;
    false positives would authorize absence, so the predicate is deliberately
    strict and ``finalize_coverage`` still re-checks the full S3.8 barrier.
    """
    rows = conn.execute(
        """
        SELECT r.id AS request_id, r.request_type, r.payload_json, r.status,
               p.outcome_kind, p.cursor_proposal_json, p.continuation_required,
               p.parsed_at, a.outcome AS attempt_outcome,
               EXISTS(
                   SELECT 1 FROM acquisition_evidence e
                    WHERE e.request_id = r.id
                      AND e.attempt_id = p.attempt_id
                      AND (
                           e.kind = 'FAILURE'
                           OR e.ref = 'crawler://BUDGET_EXHAUSTED'
                           OR e.ref = 'crawler://DUPLICATE_FRONTIER'
                           OR e.ref = 'cache://304_REFETCH_REQUIRED'
                           OR e.ref LIKE 'child-task://%'
                      )
               ) AS has_degradation_evidence
          FROM coverage_contributing_request c
          JOIN scrape_requests r ON r.id = c.request_id
          JOIN parse_attempts p ON p.request_id = r.id
          JOIN request_attempts a ON a.attempt_id = p.attempt_id
         WHERE c.coverage_id = ?
         ORDER BY p.parsed_at DESC, p.id DESC
        """,
        (coverage_id,),
    ).fetchall()
    for row in rows:
        if row["request_type"] not in _ENUMERATION_REQUEST_TYPES:
            continue
        if _payload_role(row["payload_json"]) in _META_CRAWL_ROLES:
            continue
        if row["status"] != "SUCCEEDED" or row["attempt_outcome"] != "SUCCEEDED":
            continue
        if row["outcome_kind"] not in ("SUCCESS_WITH_JOBS", "SUCCESS_EMPTY"):
            continue
        if bool(row["has_degradation_evidence"]):
            # A request can be SUCCEEDED while the host deliberately refused a
            # continuation/child because of budget/scope/frontier policy. That
            # is useful partial evidence, not terminal coverage proof.
            continue
        if bool(row["continuation_required"]):
            continue
        if row["cursor_proposal_json"] not in (None, ""):
            continue
        if _has_nonmeta_enumeration_child(conn, str(row["request_id"])):
            continue
        return True
    return False


def _repair_unfinished_coverages(
    conn: sqlite3.Connection, *, now: str | None
) -> list[dict[str, str]]:
    """Resume or conservatively finalize unfinished S3.8 generations.

    Generations with still-open acquisition work are validated through the
    accepted ``open_or_resume_coverage`` identity gate and left open for the
    normal driver. If all acquisition work is closed, startup must repair the
    crash window before ``finalize_coverage``: COMPLETE is attempted only from
    strict durable terminal proof; otherwise the generation is closed
    non-authoritatively as PARTIAL/FAILED (or CANCELLED).
    """
    rows = conn.execute(
        """
        SELECT c.*, p.run_id, p.group_outcome AS plan_outcome,
               r.status AS run_status, r.cancel_requested_at,
               g.group_outcome AS logical_group_outcome
          FROM enumeration_coverage c
          JOIN run_source_plans p ON p.id = c.run_source_plan_id
          JOIN scrape_runs r ON r.id = p.run_id
          LEFT JOIN source_plan_group_state g
            ON g.run_id = p.run_id
           AND g.source_plan_group_id = p.source_plan_group_id
         WHERE c.finalized_at IS NULL
         ORDER BY c.created_at, c.id
        """
    ).fetchall()

    repaired: list[dict[str, str]] = []
    for row in rows:
        plan_id = str(row["run_source_plan_id"])
        open_work = plan_actionable_open_work(
            conn, plan_id, now=_timestamp(conn, now)
        )
        cancelled = (
            row["cancel_requested_at"] is not None
            or row["run_status"] == "CANCELLED"
            or row["plan_outcome"] == "CANCELLED"
            or row["logical_group_outcome"] == "CANCELLED"
        )

        if open_work and not cancelled:
            # No mutation: prove that the unfinished generation is the one the
            # exact historically pinned implementation may legally resume.
            # Mutable adapter/binding promotion is not an input here.
            listing_identity_sufficient = _validate_resumable_plan_contract(
                conn, plan_id
            )
            resumed_id, resumed = open_or_resume_coverage(
                conn,
                run_source_plan_id=plan_id,
                source_id=row["source_id"],
                binding_id=row["binding_id"],
                binding_revision_id=row["binding_revision_id"],
                scope_key=row["scope_key"],
                generation_key=row["generation_key"],
                coverage_authority=row["coverage_authority"],
                listing_identity_sufficient=listing_identity_sufficient,
                now=_timestamp(conn, now),
            )
            if not resumed or resumed_id != row["id"]:
                raise RuntimeError(
                    f"unfinished coverage {row['id']} did not resume itself"
                )
            continue

        ts = _timestamp(conn, now)
        if cancelled:
            finalize_coverage(
                conn,
                str(row["id"]),
                completion_state="CANCELLED",
                stop_reason="startup recovery after cancellation",
                terminal_enumeration_proven=False,
                now=ts,
            )
            repaired.append({"coverage_id": str(row["id"]), "state": "CANCELLED"})
            continue

        terminal_proven = _durable_terminal_enumeration_proven(
            conn, str(row["id"])
        )
        if terminal_proven:
            try:
                finalize_coverage(
                    conn,
                    str(row["id"]),
                    completion_state="COMPLETE",
                    stop_reason="startup recovery: durable terminal enumeration",
                    terminal_enumeration_proven=True,
                    now=ts,
                )
                repaired.append({"coverage_id": str(row["id"]), "state": "COMPLETE"})
                continue
            except CoverageFinalizationError:
                # The S3.8 barrier is authoritative. A strict terminal parse
                # can still be insufficient (failed contributor, degraded
                # generation, required detail, etc.). Never manufacture
                # absence authority; close conservatively below.
                pass

        state = (
            "PARTIAL"
            if _coverage_has_accepted_evidence(conn, str(row["id"]))
            else "FAILED"
        )
        finalize_coverage(
            conn,
            str(row["id"]),
            completion_state=state,
            stop_reason="startup recovery: closed work without complete durable proof",
            terminal_enumeration_proven=False,
            now=ts,
        )
        repaired.append({"coverage_id": str(row["id"]), "state": state})

    return repaired


def _latest_finalized_coverage(
    conn: sqlite3.Connection, plan_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM enumeration_coverage
         WHERE run_source_plan_id = ? AND finalized_at IS NOT NULL
         ORDER BY created_at DESC, id DESC
         LIMIT 1
        """,
        (plan_id,),
    ).fetchone()


def _outcome_from_finalized_coverage(
    conn: sqlite3.Connection, coverage: sqlite3.Row
) -> str:
    state = str(coverage["completion_state"] or "UNKNOWN")
    if state == "COMPLETE":
        return "SATISFIED"
    if state == "CANCELLED":
        return "CANCELLED"
    if state == "FAILED":
        return "FAILED"
    if state in {"PARTIAL", "BUDGET_EXHAUSTED", "UNKNOWN"}:
        return (
            "SATISFIED_PARTIAL"
            if _coverage_has_accepted_evidence(conn, str(coverage["id"]))
            else "FAILED"
        )
    return "FAILED"


def _repair_plan_group_truth(
    conn: sqlite3.Connection, *, now: str | None
) -> list[dict[str, str]]:
    """Repair the crash window after coverage finalization, before group commit.

    This function does not implement fallback itself. It derives only the
    active plan's terminal result from finalized S3.8 truth, then calls the
    existing serialized S3.9 ``set_group_outcome`` owner. That owner alone may
    advance rank N→N+1, close the logical group, or upgrade a prior partial.
    """
    rows = conn.execute(
        """
        SELECT p.*, r.cancel_requested_at,
               g.active_fallback_rank,
               g.group_outcome AS logical_group_outcome
          FROM run_source_plans p
          JOIN scrape_runs r ON r.id = p.run_id
          JOIN source_plan_group_state g
            ON g.run_id = p.run_id
           AND g.source_plan_group_id = p.source_plan_group_id
         WHERE r.status IN ('QUEUED', 'RUNNING')
           AND p.fallback_rank = g.active_fallback_rank
           AND (
                (p.group_outcome IS NULL AND g.group_outcome IS NULL)
                OR
                (p.group_outcome = 'SATISFIED_PARTIAL'
                 AND g.group_outcome = 'SATISFIED_PARTIAL')
               )
         ORDER BY p.run_id, p.source_plan_group_id, p.fallback_rank
        """
    ).fetchall()

    repaired: list[dict[str, str]] = []
    for plan in rows:
        if plan["cancel_requested_at"] is not None:
            continue
        plan_id = str(plan["id"])
        if plan_actionable_open_work(
            conn, plan_id, now=_timestamp(conn, now)
        ):
            continue
        coverage = _latest_finalized_coverage(conn, plan_id)
        if coverage is None:
            # No durable S3.8 terminal fact means there is not enough evidence
            # to invent a plan result. Leave the plan visibly unresolved.
            continue
        outcome = _outcome_from_finalized_coverage(conn, coverage)
        before = (
            plan["group_outcome"],
            int(plan["active_fallback_rank"]),
            plan["logical_group_outcome"],
        )
        set_group_outcome(conn, plan_id, outcome, now=now)
        after_plan = conn.execute(
            "SELECT group_outcome FROM run_source_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        after_group = conn.execute(
            "SELECT active_fallback_rank, group_outcome"
            " FROM source_plan_group_state"
            " WHERE run_id = ? AND source_plan_group_id = ?",
            (plan["run_id"], plan["source_plan_group_id"]),
        ).fetchone()
        after = (
            after_plan["group_outcome"],
            int(after_group["active_fallback_rank"]),
            after_group["group_outcome"],
        )
        if after != before:
            repaired.append({"plan_id": plan_id, "outcome": outcome})
    return repaired


def _drain_due_local_obligations(
    conn: sqlite3.Connection, *, now: str | None, worker_id: str
) -> int:
    """Drain all S3.11 host-native obligations claimable at startup."""
    from jobscraper.pipeline.obligations import drain_one_obligation

    drained = 0
    while drain_one_obligation(conn, now=now, worker_id=worker_id):
        drained += 1
    return drained


def _reaggregate_open_runs(
    conn: sqlite3.Connection, *, now: str | None
) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT id
          FROM scrape_runs
         WHERE status IN ('QUEUED', 'RUNNING')
         ORDER BY created_at, id
        """
    ).fetchall()

    finalized: list[dict[str, str]] = []
    for row in rows:
        run_id = str(row["id"])
        status = aggregate_run(conn, run_id, now=now)
        if status is not None:
            finalized.append({"run_id": run_id, "status": str(status)})
    return finalized


def recover_startup_state(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    worker_id: str = "startup-recovery",
) -> dict:
    """Deterministically reconcile durable state before coordinator startup.

    Caller contract: migrations are complete, a fresh service epoch is active,
    and no coordinator/listener/background source work can race this function.
    ``now`` is only for deterministic tests. Production passes ``None`` so
    each accepted primitive samples authoritative DB UTC at its own serialized
    boundary; a long recovery pass never extends ownership with a stale
    startup timestamp.
    """
    if current_service_epoch(conn) is None:
        raise NoActiveServiceEpoch(
            "startup recovery requires a fresh active service epoch"
        )

    interrupted = recover_interrupted_requests(conn, now=now)
    released_local = _release_reclaimed_local_retries(
        conn, list(interrupted["reclaimed"]), now=now
    )

    # Cancellation is durable run-level truth and dominates group recovery.
    cancelled_now = _resume_cancelled_runs(conn, now=now)

    # Reconcile S3.8 first, then feed only finalized durable coverage truth to
    # the S3.9 group owner. No source-network work occurs here.
    repaired_coverages = _repair_unfinished_coverages(conn, now=now)
    repaired_plans = _repair_plan_group_truth(conn, now=now)
    validated_plans = _validate_active_open_plans(conn)

    # Accepted local work survives both cancellation and restart. Service-
    # restart-reclaimed local work was made due above; source-network retry and
    # Retry-After/circuit state remain untouched.
    drained = _drain_due_local_obligations(
        conn, now=now, worker_id=worker_id
    )

    reaggregated = _reaggregate_open_runs(conn, now=now)

    cancelled_final = set(interrupted["finalized_cancelled_runs"])
    cancelled_final.update(cancelled_now)
    cancelled_final.update(
        item["run_id"]
        for item in reaggregated
        if item["status"] == "CANCELLED"
    )

    return {
        "reclaimed": list(interrupted["reclaimed"]),
        "released_local_retries": released_local,
        "finalized_cancelled_runs": sorted(cancelled_final),
        "repaired_coverages": repaired_coverages,
        "repaired_plan_outcomes": repaired_plans,
        "validated_resumable_plans": validated_plans,
        "drained_local_obligations": drained,
        "reaggregated_terminal_runs": reaggregated,
    }


__all__ = ["recover_interrupted_requests", "recover_startup_state"]
