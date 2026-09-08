"""Run creation and aggregation (03 §16.1, RUN-01, RUN-02).

``create_run`` persists the immutable RunSourcePlan snapshots: editing a
binding/query/profile afterwards can never change the interpretation of an
active or historical run (RUN-02 rules 1-3, 7-8).

``aggregate_run`` implements the RUN-01 truth table over logical
source-plan groups, never over individual fallback rows: a successful
fallback satisfies its group, an unused fallback is SKIPPED_NOT_NEEDED
(never a failure), and a valid complete zero-job result is success.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Mapping

from jobscraper.ids import new_id
from jobscraper.runtime.clock import db_utc_now

TERMINAL_GROUP_OUTCOMES = frozenset(
    {
        "SATISFIED",
        "SATISFIED_PARTIAL",
        "FAILED",
        "CANCELLED",
        "POLICY_DENIED",
        "SKIPPED_NOT_NEEDED",
    }
)

RUN_STATUSES = frozenset(
    {"QUEUED", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
)


def _snapshot(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def create_run(
    conn: sqlite3.Connection,
    *,
    profile_id: str | None,
    plans: Iterable[Mapping[str, object]],
    now: str | None = None,
) -> tuple[str, list[str]]:
    """Create a QUEUED run plus its immutable run_source_plans.

    Each plan mapping carries the pinned identity per RUN-02: source_id,
    source_plan_group_id, fallback_rank, binding_id, binding_revision_id,
    adapter_id/adapter_version/adapter_api_version, strategy,
    execution_class, permission_profile_id/permission_profile_revision and
    the crawl/rate/config snapshots.
    """
    ts = now or db_utc_now(conn)
    run_id = new_id("run")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO scrape_runs (id, profile_id, status, created_at)"
            " VALUES (?, ?, 'QUEUED', ?)",
            (run_id, profile_id, ts),
        )
        plan_ids: list[str] = []
        for plan in plans:
            plan_id = new_id("rsp")
            conn.execute(
                """
                INSERT INTO run_source_plans (
                    id, run_id, source_id, source_config_snapshot_ref, query_id,
                    query_revision_id, source_plan_group_id, fallback_rank, binding_id,
                    binding_revision_id, adapter_id, adapter_version, adapter_api_version,
                    strategy, execution_class, cursor_schema_version,
                    crawl_policy_snapshot_json, rate_policy_snapshot_json,
                    auth_scope_id, permission_profile_id, permission_profile_revision,
                    run_config_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    run_id,
                    plan["source_id"],
                    plan.get("source_config_snapshot_ref"),
                    plan.get("query_id"),
                    plan.get("query_revision_id"),
                    plan["source_plan_group_id"],
                    int(plan.get("fallback_rank", 0)),
                    plan["binding_id"],
                    plan["binding_revision_id"],
                    plan["adapter_id"],
                    plan["adapter_version"],
                    plan["adapter_api_version"],
                    plan["strategy"],
                    plan["execution_class"],
                    int(plan.get("cursor_schema_version", 1)),
                    _snapshot(plan.get("crawl_policy_snapshot_json", "{}")),
                    _snapshot(plan.get("rate_policy_snapshot_json", "{}")),
                    plan.get("auth_scope_id"),
                    plan["permission_profile_id"],
                    int(plan["permission_profile_revision"]),
                    plan.get("run_config_hash"),
                    ts,
                ),
            )
            plan_ids.append(plan_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return run_id, plan_ids


def mark_run_started(conn: sqlite3.Connection, run_id: str, *, now: str | None = None) -> None:
    ts = now or db_utc_now(conn)
    conn.execute(
        "UPDATE scrape_runs SET status = 'RUNNING', started_at = COALESCE(started_at, ?)"
        " WHERE id = ? AND status = 'QUEUED'",
        (ts, run_id),
    )
    conn.commit()


def set_group_outcome(
    conn: sqlite3.Connection, plan_id: str, outcome: str, *, now: str | None = None
) -> None:
    if outcome not in TERMINAL_GROUP_OUTCOMES:
        raise ValueError(f"non-terminal or unknown group outcome: {outcome!r}")
    conn.execute(
        "UPDATE run_source_plans SET group_outcome = ? WHERE id = ?", (outcome, plan_id)
    )
    conn.commit()


def aggregate_run(conn: sqlite3.Connection, run_id: str, *, now: str | None = None) -> str | None:
    """Compute and persist the run's aggregate status (RUN-01 truth table).

    Returns the new status, or ``None`` when some group is still open (a
    run must not finalize while dynamically created group work remains
    pending).
    """
    rows = conn.execute(
        "SELECT source_plan_group_id, group_outcome FROM run_source_plans WHERE run_id = ?"
        " ORDER BY source_plan_group_id, fallback_rank",
        (run_id,),
    ).fetchall()
    if not rows:
        return None
    if any(row["group_outcome"] is None for row in rows):
        return None  # an open group forbids finalization

    if any(row["group_outcome"] == "CANCELLED" for row in rows):
        # User cancellation reached at least one group; already committed
        # results remain preserved in the run's counters/evidence.
        status = "CANCELLED"
    else:
        usable = [r for r in rows if r["group_outcome"] in ("SATISFIED", "SATISFIED_PARTIAL")]
        failed = [r for r in rows if r["group_outcome"] in ("FAILED", "POLICY_DENIED")]
        incomplete = [r for r in rows if r["group_outcome"] == "SATISFIED_PARTIAL"]
        if not usable:
            status = "FAILED"
        elif failed or incomplete:
            status = "PARTIAL"
        else:
            status = "SUCCEEDED"

    ts = now or db_utc_now(conn)
    conn.execute(
        "UPDATE scrape_runs SET status = ?, finished_at = COALESCE(finished_at, ?)"
        " WHERE id = ?",
        (status, ts, run_id),
    )
    conn.commit()
    return status


__all__ = [
    "RUN_STATUSES",
    "TERMINAL_GROUP_OUTCOMES",
    "aggregate_run",
    "create_run",
    "mark_run_started",
    "set_group_outcome",
]
