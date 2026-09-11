"""Run creation, fallback-group orchestration, and aggregation (RUN-01/RUN-02).

S3.9 makes the logical ``source_plan_group_id`` the durable unit of run truth.
``run_source_plans`` remain immutable execution snapshots plus per-plan terminal
history; ``source_plan_group_state`` owns the currently active fallback rank and
the logical group outcome.

Fallback activation is serialized under ``BEGIN IMMEDIATE``.  A failed/policy-
denied active rank activates the next pinned rank exactly once.  A successful or
accepted-partial rank closes the logical group and marks later unused ranks
``SKIPPED_NOT_NEEDED``.  Run aggregation reads logical group state and refuses
to finalize while relevant dynamically-created request/local-processing work is
still open.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Mapping

from jobscraper.ids import new_id
from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES

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

# Only these active-plan terminal outcomes are allowed to advance to the next
# pinned fallback rank.  Accepted partial data is deliberately visible as
# SATISFIED_PARTIAL rather than silently triggering a second strategy.
FALLBACK_ELIGIBLE_PLAN_OUTCOMES = frozenset({"FAILED", "POLICY_DENIED"})

# A partial verdict never closes the books: the same rank may later
# supersede it once its accepted children drain (SATISFIED) or fail
# (FAILED/POLICY_DENIED), or when cancellation dominates (CANCELLED).
# Every other recorded verdict is write-once.
_PARTIAL_SUPERSEDING_OUTCOMES = frozenset(
    {"SATISFIED", "FAILED", "POLICY_DENIED", "CANCELLED"}
)

RUN_STATUSES = frozenset(
    {"QUEUED", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
)
_OPEN_REQUEST_STATUSES = ("PENDING", "RUNNING", "RETRY_WAIT")


def _claimable_or_inflight_sql(alias: str | None = None) -> str:
    """SQL fragment matching acquisition work that still blocks terminal truth.

    Mirrors the claim path exactly: ``PENDING`` is always claimable,
    ``RUNNING`` is in flight under a lease, and ``RETRY_WAIT`` is claimable
    only with a valid durable ``next_retry_at`` that is already due.  A
    dormant retry (not-yet-due, NULL, or empty timestamp) is neither
    claimable nor in flight: nothing in the bounded pass can act on it, so
    it must not wedge plan terminalization or run finalization (RUN-01
    liveness; RUN-07 lease-loss recovery keeps the retry row durable for a
    later pass via the terminal-plan re-drive path).  The caller binds the
    authoritative timestamp once.
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}status IN ('PENDING', 'RUNNING')"
        f" OR ({prefix}status = 'RETRY_WAIT'"
        f" AND {prefix}next_retry_at IS NOT NULL"
        f" AND {prefix}next_retry_at <> ''"
        f" AND {prefix}next_retry_at <= ?))"
    )


def plan_actionable_open_work(
    conn: sqlite3.Connection, plan_id: str, *, now: str
) -> int:
    """Claimable-or-inflight acquisition requests owned by one plan."""
    acquisition_types = ",".join("?" for _ in ACQUISITION_REQUEST_TYPES)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM scrape_requests"
            " WHERE run_source_plan_id = ?"
            f" AND request_type IN ({acquisition_types})"
            f" AND {_claimable_or_inflight_sql()}",
            (plan_id, *sorted(ACQUISITION_REQUEST_TYPES), now),
        ).fetchone()[0]
    )


class RunStateConflict(RuntimeError):
    """A caller attempted to contradict durable fallback/group truth."""


def _snapshot(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _ensure_group_states(
    conn: sqlite3.Connection, run_id: str, *, now: str
) -> None:
    """Backfill state for legacy/directly-inserted plan groups idempotently.

    Normal S3.9 run creation writes these rows in the same transaction.  The
    idempotent backfill keeps old test fixtures and migrated pre-S3.9 runs
    readable without making mutable binding state authoritative.
    """
    conn.execute(
        """
        INSERT INTO source_plan_group_state (
            run_id, source_plan_group_id, active_fallback_rank,
            group_outcome, created_at, updated_at)
        SELECT p.run_id, p.source_plan_group_id, MIN(p.fallback_rank),
               NULL, ?, ?
          FROM run_source_plans p
         WHERE p.run_id = ?
           AND NOT EXISTS (
               SELECT 1 FROM source_plan_group_state g
                WHERE g.run_id = p.run_id
                  AND g.source_plan_group_id = p.source_plan_group_id
           )
         GROUP BY p.run_id, p.source_plan_group_id
        """,
        (now, now, run_id),
    )


def create_run(
    conn: sqlite3.Connection,
    *,
    profile_id: str | None,
    plans: Iterable[Mapping[str, object]],
    now: str | None = None,
    commit: bool = True,
) -> tuple[str, list[str]]:
    """Create a QUEUED run plus immutable plans and durable group state.

    Each plan mapping carries the pinned identity per RUN-02.  Group state is
    created atomically from those pins; the minimum pinned ``fallback_rank`` is
    the only active rank initially.

    With ``commit=False`` the transaction remains open so a caller can add
    dependent durable work and commit the complete creation boundary atomically.
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
        _ensure_group_states(conn, run_id, now=ts)
        if commit:
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


def plan_is_active(conn: sqlite3.Connection, plan_id: str) -> bool:
    """Whether ``plan_id`` is the one acquisition rank currently authorized."""
    return conn.execute(
        """
        SELECT 1
          FROM run_source_plans p
          JOIN source_plan_group_state g
            ON g.run_id = p.run_id
           AND g.source_plan_group_id = p.source_plan_group_id
         WHERE p.id = ?
           AND p.fallback_rank = g.active_fallback_rank
           AND p.group_outcome IS NULL
           AND g.group_outcome IS NULL
        """,
        (plan_id,),
    ).fetchone() is not None


def active_plan_sql_predicate(request_alias: str = "req") -> str:
    """SQL predicate restricting acquisition work to the durable active rank.

    Requests without a RunSourcePlan are retained for pre-S3.9 compatibility;
    every S3.9 plan-owned acquisition request is checked against group state.
    A rank that closed partial keeps owning its accepted children until they
    drain or fail, so a partial verdict does not strand them: the predicate
    stays rank-bound and only additionally admits the partial markers.
    The caller supplies a trusted static SQL alias, never user input.
    """
    alias = request_alias
    return f"""
      AND (
            {alias}.run_source_plan_id IS NULL
            OR EXISTS (
                SELECT 1
                  FROM run_source_plans rsp_active
                  JOIN source_plan_group_state g_active
                    ON g_active.run_id = rsp_active.run_id
                   AND g_active.source_plan_group_id = rsp_active.source_plan_group_id
                 WHERE rsp_active.id = {alias}.run_source_plan_id
                   AND rsp_active.fallback_rank = g_active.active_fallback_rank
                   AND (rsp_active.group_outcome IS NULL
                        OR rsp_active.group_outcome = 'SATISFIED_PARTIAL')
                   AND (g_active.group_outcome IS NULL
                        OR g_active.group_outcome = 'SATISFIED_PARTIAL')
            )
          )
    """


def _finish_group(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    group_id: str,
    outcome: str,
    active_rank: int,
    now: str,
) -> None:
    conn.execute(
        "UPDATE source_plan_group_state"
        " SET group_outcome = ?, updated_at = ?"
        " WHERE run_id = ? AND source_plan_group_id = ?"
        " AND active_fallback_rank = ? AND group_outcome IS NULL",
        (outcome, now, run_id, group_id, active_rank),
    )
    # Once a logical group is terminal, later never-activated ranks remain
    # diagnostic plan snapshots but are explicitly not failures.
    conn.execute(
        "UPDATE run_source_plans SET group_outcome = 'SKIPPED_NOT_NEEDED'"
        " WHERE run_id = ? AND source_plan_group_id = ?"
        " AND fallback_rank > ? AND group_outcome IS NULL",
        (run_id, group_id, active_rank),
    )


def set_group_outcome(
    conn: sqlite3.Connection, plan_id: str, outcome: str, *, now: str | None = None
) -> None:
    """Record one active plan result and advance/finalize its logical group.

    Despite the legacy ``run_source_plans.group_outcome`` column name, that
    field is per-plan diagnostic history from S3.9 onward.  Logical group truth
    lives only in ``source_plan_group_state``.

    A ``SATISFIED_PARTIAL`` verdict never closes the books: the same rank may
    later supersede it (completion upgrade, failure downgrade, or dominant
    cancellation) by reopening plan and group inside this same serialized
    transaction and flowing through the normal machine.  Every other recorded
    verdict is write-once.  A plan diagnostic lost after its group verdict
    committed (crash window) is repaired when the reported outcome agrees
    with durable group truth and nothing actionable remains open.
    """
    if outcome not in TERMINAL_GROUP_OUTCOMES:
        raise ValueError(f"non-terminal or unknown group outcome: {outcome!r}")

    conn.execute("BEGIN IMMEDIATE")
    try:
        ts = now or db_utc_now(conn)
        plan = conn.execute(
            "SELECT id, run_id, source_plan_group_id, fallback_rank, group_outcome"
            " FROM run_source_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        if plan is None:
            raise KeyError(plan_id)
        _ensure_group_states(conn, plan["run_id"], now=ts)
        state = conn.execute(
            "SELECT active_fallback_rank, group_outcome"
            " FROM source_plan_group_state"
            " WHERE run_id = ? AND source_plan_group_id = ?",
            (plan["run_id"], plan["source_plan_group_id"]),
        ).fetchone()

        previous = plan["group_outcome"]
        if previous is not None and previous != outcome:
            if not (
                previous == "SATISFIED_PARTIAL"
                and outcome in _PARTIAL_SUPERSEDING_OUTCOMES
            ):
                raise RunStateConflict(
                    f"plan {plan_id} is already terminal as {previous}, not {outcome}"
                )
            reopened_plan = conn.execute(
                "UPDATE run_source_plans SET group_outcome = NULL"
                " WHERE id = ? AND group_outcome = 'SATISFIED_PARTIAL'",
                (plan_id,),
            )
            reopened_group = conn.execute(
                "UPDATE source_plan_group_state"
                " SET group_outcome = NULL, updated_at = ?"
                " WHERE run_id = ? AND source_plan_group_id = ?"
                " AND active_fallback_rank = ? AND group_outcome = 'SATISFIED_PARTIAL'",
                (
                    ts,
                    plan["run_id"],
                    plan["source_plan_group_id"],
                    plan["fallback_rank"],
                ),
            )
            if reopened_plan.rowcount != 1 or reopened_group.rowcount != 1:
                raise RunStateConflict(
                    f"plan {plan_id} partial reopen lost serialized ownership"
                )
            previous = None
            state = conn.execute(
                "SELECT active_fallback_rank, group_outcome"
                " FROM source_plan_group_state"
                " WHERE run_id = ? AND source_plan_group_id = ?",
                (plan["run_id"], plan["source_plan_group_id"]),
            ).fetchone()
        if previous is not None:
            conn.execute("COMMIT")
            return
        if state["group_outcome"] is not None:
            if previous is None and outcome == state["group_outcome"]:
                if plan_actionable_open_work(conn, plan_id, now=ts):
                    raise RunStateConflict(
                        f"plan {plan_id} cannot repair around open request(s)"
                    )
                repaired = conn.execute(
                    "UPDATE run_source_plans SET group_outcome = ?"
                    " WHERE id = ? AND group_outcome IS NULL",
                    (outcome, plan_id),
                )
                if repaired.rowcount != 1:
                    raise RunStateConflict(
                        f"plan {plan_id} repair lost serialized ownership"
                    )
                conn.execute("COMMIT")
                return
            raise RunStateConflict(
                f"group {plan['source_plan_group_id']} is already terminal as "
                f"{state['group_outcome']}"
            )
        if int(state["active_fallback_rank"]) != int(plan["fallback_rank"]):
            raise RunStateConflict(
                f"plan {plan_id} rank {plan['fallback_rank']} is dormant; active rank is "
                f"{state['active_fallback_rank']}"
            )
        if outcome not in ("SATISFIED_PARTIAL", "CANCELLED"):
            # A partial verdict honestly reports unfinished work and
            # cancellation dominates; every other verdict must not
            # terminalize around claimable-or-inflight work.
            open_work = plan_actionable_open_work(conn, plan_id, now=ts)
            if open_work:
                raise RunStateConflict(
                    f"plan {plan_id} cannot terminalize with {open_work} open request(s)"
                )

        conn.execute(
            "UPDATE run_source_plans SET group_outcome = ?"
            " WHERE id = ? AND group_outcome IS NULL",
            (outcome, plan_id),
        )

        if outcome in FALLBACK_ELIGIBLE_PLAN_OUTCOMES:
            next_row = conn.execute(
                "SELECT fallback_rank FROM run_source_plans"
                " WHERE run_id = ? AND source_plan_group_id = ?"
                " AND fallback_rank > ? AND group_outcome IS NULL"
                " ORDER BY fallback_rank LIMIT 1",
                (
                    plan["run_id"],
                    plan["source_plan_group_id"],
                    plan["fallback_rank"],
                ),
            ).fetchone()
            if next_row is not None:
                updated = conn.execute(
                    "UPDATE source_plan_group_state"
                    " SET active_fallback_rank = ?, updated_at = ?"
                    " WHERE run_id = ? AND source_plan_group_id = ?"
                    " AND active_fallback_rank = ? AND group_outcome IS NULL",
                    (
                        next_row["fallback_rank"],
                        ts,
                        plan["run_id"],
                        plan["source_plan_group_id"],
                        plan["fallback_rank"],
                    ),
                )
                if updated.rowcount != 1:
                    raise RunStateConflict("fallback activation lost serialized ownership")
            else:
                policy_only = conn.execute(
                    "SELECT COUNT(*) = SUM(CASE WHEN group_outcome = 'POLICY_DENIED'"
                    " THEN 1 ELSE 0 END)"
                    " FROM run_source_plans"
                    " WHERE run_id = ? AND source_plan_group_id = ?"
                    " AND group_outcome <> 'SKIPPED_NOT_NEEDED'",
                    (plan["run_id"], plan["source_plan_group_id"]),
                ).fetchone()[0]
                final_outcome = "POLICY_DENIED" if policy_only else "FAILED"
                _finish_group(
                    conn,
                    run_id=plan["run_id"],
                    group_id=plan["source_plan_group_id"],
                    outcome=final_outcome,
                    active_rank=int(plan["fallback_rank"]),
                    now=ts,
                )
        else:
            _finish_group(
                conn,
                run_id=plan["run_id"],
                group_id=plan["source_plan_group_id"],
                outcome=outcome,
                active_rank=int(plan["fallback_rank"]),
                now=ts,
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def cancel_open_groups(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    now: str | None = None,
    commit: bool = True,
) -> None:
    """Cancel each still-open logical group without rewriting completed groups.

    A group with RUNNING acquisition requests (active lease) is NOT cancelled
    here — the canceller does not revoke an active lease. The RUNNING request
    remains for its lease to expire and be reclaimed by recovery, after which
    the group will be eligible for cancellation.
    """
    owns_tx = commit
    if owns_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        ts = now or db_utc_now(conn)
        _ensure_group_states(conn, run_id, now=ts)
        open_groups = conn.execute(
            "SELECT source_plan_group_id, active_fallback_rank"
            " FROM source_plan_group_state"
            " WHERE run_id = ? AND group_outcome IS NULL"
            " ORDER BY source_plan_group_id",
            (run_id,),
        ).fetchall()
        for group in open_groups:
            # Only cancel if no RUNNING acquisition requests remain under this
            # active rank. PENDING/RETRY_WAIT are cancelled by the caller
            # (request_run_cancellation) before reaching here; RUNNING leases
            # are respected and reclaimed later.
            running_acq = conn.execute(
                "SELECT COUNT(*) FROM scrape_requests req"
                " WHERE req.run_id = ?"
                " AND req.run_source_plan_id IN ("
                "   SELECT id FROM run_source_plans"
                "   WHERE run_id = ? AND source_plan_group_id = ?"
                "   AND fallback_rank = ? AND group_outcome IS NULL"
                " )"
                " AND req.request_type IN ("
                + ",".join("?" for _ in ACQUISITION_REQUEST_TYPES)
                + ")"
                " AND req.status = 'RUNNING'",
                (
                    run_id,
                    run_id,
                    group["source_plan_group_id"],
                    group["active_fallback_rank"],
                    *sorted(ACQUISITION_REQUEST_TYPES),
                ),
            ).fetchone()[0]
            if running_acq:
                continue
            conn.execute(
                "UPDATE run_source_plans SET group_outcome = 'CANCELLED'"
                " WHERE run_id = ? AND source_plan_group_id = ?"
                " AND fallback_rank = ? AND group_outcome IS NULL",
                (run_id, group["source_plan_group_id"], group["active_fallback_rank"]),
            )
            _finish_group(
                conn,
                run_id=run_id,
                group_id=group["source_plan_group_id"],
                outcome="CANCELLED",
                active_rank=int(group["active_fallback_rank"]),
                now=ts,
            )
        if owns_tx:
            conn.execute("COMMIT")
    except BaseException:
        if owns_tx and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _relevant_native_open_work(conn: sqlite3.Connection, run_id: str) -> int:
    """Host-native obligations that still prevent terminal run reporting.

    Accepted local processing always drains or remains visibly pending,
    regardless of which fallback row owns it.
    """
    acquisition = ",".join("?" for _ in ACQUISITION_REQUEST_TYPES)
    statuses = ",".join("?" for _ in _OPEN_REQUEST_STATUSES)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM scrape_requests req"
            f" WHERE req.run_id = ? AND req.request_type NOT IN ({acquisition})"
            f" AND req.status IN ({statuses})",
            (
                run_id,
                *sorted(ACQUISITION_REQUEST_TYPES),
                *_OPEN_REQUEST_STATUSES,
            ),
        ).fetchone()[0]
    )


def _relevant_open_work(conn: sqlite3.Connection, run_id: str, *, now: str) -> int:
    """Open work that still prevents terminal run reporting.

    Host-native obligations are always relevant once created, even if their
    owning fallback row later becomes SKIPPED_NOT_NEEDED during migration or
    group closure.

    Acquisition work counts only while its owning plan can still drive it:
    the plan is undecided or partial, and the request is claimable or in
    flight.  A dormant retry is actionable by nothing in the bounded pass,
    and leftovers owned by a finally-terminal plan are claimable by nobody,
    so neither wedges finalization while their rows stay durable.  Only
    acquisition work belonging to an explicitly skipped plan was already
    irrelevant to further collection.
    """
    acquisition = ",".join("?" for _ in ACQUISITION_REQUEST_TYPES)
    actionable = _claimable_or_inflight_sql("req")
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM scrape_requests req"
            " LEFT JOIN run_source_plans p ON p.id = req.run_source_plan_id"
            " WHERE req.run_id = ?"
            " AND ("
            f"      (req.request_type NOT IN ({acquisition})"
            "       AND req.status IN ('PENDING', 'RUNNING', 'RETRY_WAIT'))"
            f"      OR (req.request_type IN ({acquisition}) AND {actionable}"
            "       AND (p.id IS NULL"
            "        OR p.group_outcome IS NULL"
            "        OR p.group_outcome = 'SATISFIED_PARTIAL'))"
            " )",
            (
                run_id,
                *sorted(ACQUISITION_REQUEST_TYPES),
                *sorted(ACQUISITION_REQUEST_TYPES),
                now,
            ),
        ).fetchone()[0]
    )


def aggregate_run(
    conn: sqlite3.Connection, run_id: str, *, now: str | None = None
) -> str | None:
    """Compute/persist RUN-01 status from logical groups plus the work barrier."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        ts = now or db_utc_now(conn)
        _ensure_group_states(conn, run_id, now=ts)
        rows = conn.execute(
            "SELECT source_plan_group_id, group_outcome"
            " FROM source_plan_group_state WHERE run_id = ?"
            " ORDER BY source_plan_group_id",
            (run_id,),
        ).fetchall()
        if not rows or any(row["group_outcome"] is None for row in rows):
            conn.execute("COMMIT")
            return None

        relevant = [r for r in rows if r["group_outcome"] != "SKIPPED_NOT_NEEDED"]
        run_row = conn.execute(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise KeyError(run_id)
        # RUN-01: a cancellation requested while collection was already
        # satisfied but accepted local processing was still draining must still
        # finish CANCELLED. Finished runs are protected by the service route and
        # never enter this path merely because a user clicks cancel afterwards.
        if run_row["cancel_requested_at"] is not None:
            status = "CANCELLED"
        elif not relevant:
            status = "FAILED"
        elif any(row["group_outcome"] == "CANCELLED" for row in relevant):
            status = "CANCELLED"
        else:
            usable = [
                r
                for r in relevant
                if r["group_outcome"] in ("SATISFIED", "SATISFIED_PARTIAL")
            ]
            failed = [
                r for r in relevant if r["group_outcome"] in ("FAILED", "POLICY_DENIED")
            ]
            incomplete = [
                r for r in relevant if r["group_outcome"] == "SATISFIED_PARTIAL"
            ]
            if not usable:
                status = "FAILED"
            elif failed or incomplete:
                status = "PARTIAL"
            else:
                status = "SUCCEEDED"

        # An explicitly incomplete verdict (PARTIAL/CANCELLED) honestly reports
        # unfinished collection, so open acquisition work does not block it;
        # accepted host-native obligations must still drain first.  A verdict
        # of SUCCEEDED/FAILED claims nothing is left actionable.
        if status in ("PARTIAL", "CANCELLED"):
            blocking = _relevant_native_open_work(conn, run_id)
        else:
            blocking = _relevant_open_work(conn, run_id, now=ts)
        if blocking:
            conn.execute("COMMIT")
            return None

        conn.execute(
            "UPDATE scrape_runs SET status = ?, finished_at = COALESCE(finished_at, ?)"
            " WHERE id = ?",
            (status, ts, run_id),
        )
        conn.execute("COMMIT")
        return status
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


__all__ = [
    "FALLBACK_ELIGIBLE_PLAN_OUTCOMES",
    "RUN_STATUSES",
    "RunStateConflict",
    "TERMINAL_GROUP_OUTCOMES",
    "active_plan_sql_predicate",
    "aggregate_run",
    "cancel_open_groups",
    "create_run",
    "mark_run_started",
    "plan_actionable_open_work",
    "plan_is_active",
    "set_group_outcome",
]
