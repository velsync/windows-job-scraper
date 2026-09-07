"""Durable request queue: enqueue, atomic claim, heartbeat, fenced commit.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md RUN-04
through RUN-08 (request model, uniqueness, attempts, claim/lease, fenced
terminal commit); VER-01.

Key invariants:
  * work exists durably before an executor performs I/O;
  * exactly one worker receives a claim;
  * an expired lease is already lost ownership, even if the reclaimer has not
    run — a worker MUST NOT revive it;
  * terminal commit is fenced: outputs and the request transition are atomic
    under the same ownership fence;
  * retry creates a new attempt, not a duplicate request.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Callable

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import add_seconds, utc_now_s


class QueueError(Exception):
    pass


class LeaseLost(QueueError):
    """The worker no longer owns the request when it tries to commit."""


@dataclass(frozen=True)
class Claim:
    request_id: str
    attempt_id: str
    worker_id: str
    request_type: str
    payload: dict
    run_source_plan_id: str
    strategy: str
    execution_class: str


def enqueue_request_tx(
    tx: sqlite3.Connection,
    *,
    run_id: str,
    run_source_plan_id: str,
    source_id: str,
    binding_id: str,
    request_type: str,
    request_unique_key: str,
    payload: dict,
    strategy: str,
    execution_class: str = "HTTP",
    priority: int = 100,
    depth: int = 0,
    parent_request_id: str | None = None,
    query_id: str | None = None,
    max_attempts: int = 3,
    coverage_generation_id: str | None = None,
    now: str | None = None,
) -> str | None:
    """Enqueue inside an existing (fenced) transaction; returns None when the
    logical unit of work already exists (request uniqueness)."""
    now = now or utc_now_s()
    request_id = "req-" + secrets.token_hex(12)
    try:
        tx.execute(
            "INSERT INTO scrape_requests(id, run_id, run_source_plan_id, query_id, source_id,"
            " binding_id, request_type, request_unique_key, payload_json, strategy,"
            " execution_class, priority, depth, parent_request_id, status, max_attempts,"
            " coverage_generation_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                run_id,
                run_source_plan_id,
                query_id,
                source_id,
                binding_id,
                request_type,
                request_unique_key,
                json.dumps(payload, sort_keys=True),
                strategy,
                execution_class,
                priority,
                depth,
                parent_request_id,
                "PENDING",
                max_attempts,
                coverage_generation_id,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        return None  # same logical request already enqueued
    tx.execute("UPDATE scrape_runs SET requests_total = requests_total + 1 WHERE id = ?", (run_id,))
    return request_id


def enqueue_request(
    db: Database,
    *,
    run_id: str,
    run_source_plan_id: str,
    source_id: str,
    binding_id: str,
    request_type: str,
    request_unique_key: str,
    payload: dict,
    strategy: str,
    execution_class: str = "HTTP",
    priority: int = 100,
    depth: int = 0,
    parent_request_id: str | None = None,
    query_id: str | None = None,
    max_attempts: int = 3,
    coverage_generation_id: str | None = None,
    now: str | None = None,
) -> str | None:
    """Enqueue one logical request in its own transaction."""
    with immediate_transaction(db.conn) as tx:
        return enqueue_request_tx(
            tx,
            run_id=run_id,
            run_source_plan_id=run_source_plan_id,
            source_id=source_id,
            binding_id=binding_id,
            request_type=request_type,
            request_unique_key=request_unique_key,
            payload=payload,
            strategy=strategy,
            execution_class=execution_class,
            priority=priority,
            depth=depth,
            parent_request_id=parent_request_id,
            query_id=query_id,
            max_attempts=max_attempts,
            coverage_generation_id=coverage_generation_id,
            now=now,
        )


def request_by_id(db: Database, request_id: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM scrape_requests WHERE id = ?", (request_id,))


def claim_next_request(
    db: Database,
    *,
    worker_id: str,
    run_id: str | None = None,
    run_source_plan_id: str | None = None,
    execution_classes: tuple[str, ...] = ("HTTP",),
    request_types: tuple[str, ...] | None = None,
    lease_window_s: int = 120,
    now: str | None = None,
) -> Claim | None:
    """Atomically claim the next due request (PENDING or due RETRY_WAIT).

    Only one worker can receive the claim: the claim runs inside
    ``BEGIN IMMEDIATE`` and stamps a fresh unique attempt token. Expired
    RUNNING leases are reclaimed first — an expired lease is already lost
    ownership even if no reclaimer observed it yet. ``run_source_plan_id``
    restricts the claim to one plan's requests (plans execute sequentially;
    a plan must never consume another plan's request).
    """
    now = now or utc_now_s()
    reclaim_expired_requests(db, now=now)
    classes = ",".join("?" for _ in execution_classes)
    sql = (
        "SELECT * FROM scrape_requests WHERE status IN ('PENDING','RETRY_WAIT')"
        " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
        f" AND execution_class IN ({classes})"
    )
    params: list = [now, *execution_classes]
    if request_types:
        sql += " AND request_type IN (" + ",".join("?" for _ in request_types) + ")"
        params.extend(request_types)
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    if run_source_plan_id:
        sql += " AND run_source_plan_id = ?"
        params.append(run_source_plan_id)
    sql += " AND run_id NOT IN (SELECT id FROM scrape_runs WHERE cancel_requested_at IS NOT NULL)"
    sql += " ORDER BY priority ASC, created_at ASC LIMIT 1"

    with immediate_transaction(db.conn) as tx:
        row = tx.execute(sql, params).fetchone()
        if row is None:
            return None
        attempt_id = "att-" + secrets.token_hex(12)
        lease_until = add_seconds(now, lease_window_s)
        updated = tx.execute(
            "UPDATE scrape_requests SET status='RUNNING', current_worker_id=?,"
            " current_attempt_id=?, lease_until=?, heartbeat_at=?, attempt_count=attempt_count+1,"
            " started_at=COALESCE(started_at, ?), updated_at=? WHERE id=? AND status IN"
            " ('PENDING','RETRY_WAIT')",
            (worker_id, attempt_id, lease_until, now, now, now, row["id"]),
        )
        if updated.rowcount != 1:  # pragma: no cover - single writer guard
            raise QueueError("claim race detected")
        tx.execute(
            "INSERT INTO request_attempts(attempt_id, request_id, worker_id, started_at,"
            " lease_expires_at, created_at) VALUES (?,?,?,?,?,?)",
            (attempt_id, row["id"], worker_id, now, lease_until, now),
        )
        return Claim(
            request_id=row["id"],
            attempt_id=attempt_id,
            worker_id=worker_id,
            request_type=row["request_type"],
            payload=json.loads(row["payload_json"]),
            run_source_plan_id=row["run_source_plan_id"],
            strategy=row["strategy"],
            execution_class=row["execution_class"],
        )


def heartbeat(
    db: Database, *, request_id: str, attempt_id: str, worker_id: str,
    lease_window_s: int = 120, now: str | None = None
) -> bool:
    """Renew the lease; False when ownership is already lost.

    A heartbeat is accepted only when the request is RUNNING, the attempt
    token matches, the lease has NOT expired, and the run was not cancelled.
    An expired lease is already lost ownership even if the reclaimer has not
    yet executed — observing expiry durably invalidates the attempt so it
    cannot be revived by a later heartbeat or commit.
    """
    now = now or utc_now_s()
    with immediate_transaction(db.conn) as tx:
        row = tx.execute(
            "SELECT current_attempt_id, lease_until, status, run_id, attempt_count, max_attempts"
            " FROM scrape_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return False
        if row["status"] != "RUNNING" or row["current_attempt_id"] != attempt_id:
            return False
        if row["lease_until"] is None or row["lease_until"] <= now:
            # Durably invalidate: mark attempt ABANDONED and move the request
            # out of RUNNING so no later write can revive this attempt.
            tx.execute(
                "UPDATE request_attempts SET finished_at=?, outcome='ABANDONED',"
                " abandoned_reason='lease_expired' WHERE attempt_id=?",
                (now, attempt_id),
            )
            budget_remaining = row["attempt_count"] < row["max_attempts"]
            if budget_remaining:
                tx.execute(
                    "UPDATE scrape_requests SET status='RETRY_WAIT', next_retry_at=?,"
                    " current_attempt_id=NULL, current_worker_id=NULL, lease_until=NULL,"
                    " updated_at=? WHERE id=?",
                    (now, now, request_id),
                )
            else:
                tx.execute(
                    "UPDATE scrape_requests SET status='FAILED', finished_at=?,"
                    " current_attempt_id=NULL, current_worker_id=NULL, lease_until=NULL,"
                    " updated_at=? WHERE id=?",
                    (now, now, request_id),
                )
            return False
        cancelled = tx.execute(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id=?", (row["run_id"],)
        ).fetchone()
        if cancelled and cancelled["cancel_requested_at"] is not None:
            return False
        lease_until = add_seconds(now, lease_window_s)
        tx.execute(
            "UPDATE scrape_requests SET lease_until=?, heartbeat_at=?, updated_at=? WHERE id=?",
            (lease_until, now, now, request_id),
        )
        tx.execute(
            "UPDATE request_attempts SET last_heartbeat_at=?, lease_expires_at=? WHERE attempt_id=?",
            (now, lease_until, attempt_id),
        )
        return True


def reclaim_expired_requests(
    db: Database, *, now: str | None = None, abandoned_reason: str = "lease_expired"
) -> list[str]:
    """Reclaim RUNNING requests with expired leases.

    Records the prior attempt ABANDONED, then moves the request back to
    PENDING/RETRY_WAIT when budget remains, FAILED otherwise.
    """
    now = now or utc_now_s()
    reclaimed: list[str] = []
    with immediate_transaction(db.conn) as tx:
        rows = tx.execute(
            "SELECT id, current_attempt_id, attempt_count, max_attempts, run_id"
            " FROM scrape_requests WHERE status='RUNNING' AND lease_until IS NOT NULL AND lease_until <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            if row["current_attempt_id"]:
                tx.execute(
                    "UPDATE request_attempts SET finished_at=?, outcome='ABANDONED',"
                    " abandoned_reason=? WHERE attempt_id=?",
                    (now, abandoned_reason, row["current_attempt_id"]),
                )
            budget_remaining = row["attempt_count"] < row["max_attempts"]
            cancelled = tx.execute(
                "SELECT cancel_requested_at FROM scrape_runs WHERE id=?", (row["run_id"],)
            ).fetchone()
            if cancelled and cancelled["cancel_requested_at"] is not None:
                tx.execute(
                    "UPDATE scrape_requests SET status='CANCELLED', finished_at=?, updated_at=?,"
                    " current_attempt_id=NULL, current_worker_id=NULL WHERE id=?",
                    (now, now, row["id"]),
                )
            elif budget_remaining:
                tx.execute(
                    "UPDATE scrape_requests SET status='RETRY_WAIT', next_retry_at=?,"
                    " current_attempt_id=NULL, current_worker_id=NULL, lease_until=NULL,"
                    " updated_at=?, last_failure_kind='LEASE_LOST' WHERE id=?",
                    (now, now, row["id"]),
                )
            else:
                tx.execute(
                    "UPDATE scrape_requests SET status='FAILED', finished_at=?, updated_at=?,"
                    " current_attempt_id=NULL, current_worker_id=NULL, lease_until=NULL,"
                    " last_failure_kind='LEASE_LOST' WHERE id=?",
                    (now, now, row["id"]),
                )
            reclaimed.append(row["id"])
    return reclaimed


def fenced_commit(
    db: Database,
    *,
    request_id: str,
    attempt_id: str,
    commit: Callable[[sqlite3.Connection], object],
    terminal_status: str = "SUCCEEDED",
    now: str | None = None,
) -> object:
    """Fenced terminal commit (RUN-08).

    Inside one ``BEGIN IMMEDIATE`` transaction: verify ownership (RUNNING +
    matching attempt + unexpired lease + run not cancelled), run the caller's
    commit function (outputs + child work + obligations), then transition the
    request. If ownership verification affects zero rows the worker MUST NOT
    commit request-owned outputs: :class:`LeaseLost` is raised and nothing is
    written.
    """
    now = now or utc_now_s()
    with immediate_transaction(db.conn) as tx:
        row = tx.execute(
            "SELECT status, current_attempt_id, lease_until, run_id, attempt_count,"
            " max_attempts FROM scrape_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "RUNNING"
            or row["current_attempt_id"] != attempt_id
            or row["lease_until"] is None
            or row["lease_until"] <= now
        ):
            raise LeaseLost(f"worker lost ownership of {request_id}")
        cancelled = tx.execute(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id=?", (row["run_id"],)
        ).fetchone()
        if cancelled and cancelled["cancel_requested_at"] is not None:
            raise LeaseLost(f"run {row['run_id']} cancelled before terminal commit")

        result = commit(tx)

        if terminal_status == "SUCCEEDED":
            tx.execute(
                "UPDATE scrape_requests SET status='SUCCEEDED', finished_at=?, updated_at=?,"
                " current_attempt_id=NULL, current_worker_id=NULL, lease_until=NULL WHERE id=?",
                (now, now, request_id),
            )
        elif terminal_status == "RETRY_WAIT":
            tx.execute(
                "UPDATE scrape_requests SET status='RETRY_WAIT', next_retry_at=?, updated_at=?,"
                " current_attempt_id=NULL, current_worker_id=NULL, lease_until=NULL WHERE id=?",
                (add_seconds(now, 5), now, request_id),
            )
        elif terminal_status == "FAILED":
            tx.execute(
                "UPDATE scrape_requests SET status='FAILED', finished_at=?, updated_at=?,"
                " current_attempt_id=NULL, current_worker_id=NULL, lease_until=NULL WHERE id=?",
                (now, now, request_id),
            )
        elif terminal_status == "CANCELLED":
            tx.execute(
                "UPDATE scrape_requests SET status='CANCELLED', finished_at=?, updated_at=? WHERE id=?",
                (now, now, request_id),
            )
        else:
            raise QueueError(f"invalid terminal status {terminal_status}")
        tx.execute(
            "UPDATE request_attempts SET finished_at=?, outcome=? WHERE attempt_id=?",
            (now, terminal_status, attempt_id),
        )
        return result


def record_failure(
    db: Database,
    *,
    request_id: str,
    attempt_id: str,
    failure_kind: str,
    failure_detail: str | None = None,
    retryable: bool = True,
    retry_after_s: float | None = None,
    backoff_base_s: float = 15.0,
    now: str | None = None,
) -> str:
    """Record a failure on a claimed request and move it to retry/failure.

    Returns the next status ('RETRY_WAIT' or 'FAILED').
    """
    now = now or utc_now_s()
    row = request_by_id(db, request_id)
    if row is None:
        raise QueueError(f"unknown request {request_id}")
    detail_json = json.dumps({"kind": failure_kind, "detail": failure_detail})
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "UPDATE request_attempts SET finished_at=?, outcome='FAILED', failure_kind=?"
            " WHERE attempt_id=?",
            (now, failure_kind, attempt_id),
        )
        budget_remaining = row["attempt_count"] < row["max_attempts"]
        if retryable and budget_remaining:
            import random

            jitter = random.uniform(0.0, 0.25)
            delay = retry_after_s if retry_after_s is not None else backoff_base_s * (2 ** (row["attempt_count"] - 1))
            delay = delay * (1 + jitter)
            next_retry = add_seconds(now, delay)
            tx.execute(
                "UPDATE scrape_requests SET status='RETRY_WAIT', next_retry_at=?,"
                " last_failure_kind=?, last_failure_json=?, current_attempt_id=NULL,"
                " current_worker_id=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                (next_retry, failure_kind, detail_json, now, request_id),
            )
            return "RETRY_WAIT"
        tx.execute(
            "UPDATE scrape_requests SET status='FAILED', finished_at=?, last_failure_kind=?,"
            " last_failure_json=?, current_attempt_id=NULL, current_worker_id=NULL,"
            " lease_until=NULL, updated_at=? WHERE id=?",
            (now, failure_kind, detail_json, now, request_id),
        )
        with2 = tx.execute("SELECT run_id FROM scrape_requests WHERE id=?", (request_id,)).fetchone()
        tx.execute(
            "UPDATE scrape_runs SET requests_failed = requests_failed + 1 WHERE id=?",
            (with2["run_id"],),
        )
        return "FAILED"
