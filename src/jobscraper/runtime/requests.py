"""Durable request enqueueing (03 §16.2, RUN-04, RUN-05).

``request_unique_key`` identifies a logical unit of work, not merely a URL:
hash of (run_source_plan_id, request_type, normalized target identity,
strategy/purpose, logical pagination/detail key). Tracking-only URL
variation therefore cannot create unbounded duplicates, and the same URL
under a different strategy is a distinct logical request (ACQ-07).

Enqueue is idempotent per run: the same logical request enqueued twice is
one durable work item; retries create new attempts, never new requests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from jobscraper.ids import new_id
from jobscraper.net.urlnorm import normalize_url, url_identity
from jobscraper.runtime.clock import db_utc_now

REQUEST_TYPES = frozenset(
    {
        "SOURCE_HEALTH_CHECK",
        "SOURCE_DISCOVERY",
        "LIST_FETCH",
        "DETAIL_FETCH",
        "SOURCE_CRAWL",
        "NORMALIZE",
        "RECONCILE",
        "ENRICH",
        "ELIGIBILITY",
        "SCORE",
        "ADAPTER_SMOKE",
        "EXPORT",
    }
)

# Acquisition request types perform source network I/O; the rest are
# host-native pipeline tasks (03 §16.2, ACQ-09).
ACQUISITION_REQUEST_TYPES = frozenset(
    {
        "SOURCE_HEALTH_CHECK",
        "SOURCE_DISCOVERY",
        "LIST_FETCH",
        "DETAIL_FETCH",
        "SOURCE_CRAWL",
        "ADAPTER_SMOKE",
    }
)

HOST_NATIVE_REQUEST_TYPES = REQUEST_TYPES - ACQUISITION_REQUEST_TYPES


def request_unique_key(
    *,
    run_source_plan_id: str,
    request_type: str,
    target_identity: str,
    strategy: str | None = None,
    logical_key: str | None = None,
) -> str:
    """Deterministic uniqueness for a logical unit of work (RUN-05)."""
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"unknown request type: {request_type!r}")
    try:
        normalized_target = url_identity(target_identity)
    except Exception:
        # Non-URL targets (observation ids, opaque references) hash as-is.
        normalized_target = str(target_identity)
    parts = json.dumps(
        [
            run_source_plan_id,
            request_type,
            normalized_target,
            strategy or "",
            logical_key or "",
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(parts.encode()).hexdigest()


def enqueue_request(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    run_source_plan_id: str | None,
    source_id: str,
    binding_id: str,
    request_type: str,
    target_identity: str,
    payload: Mapping | None = None,
    strategy: str | None = None,
    execution_class: str | None = None,
    priority: int = 0,
    depth: int = 0,
    parent_request_id: str | None = None,
    logical_key: str | None = None,
    max_attempts: int = 3,
    now: str | None = None,
    commit: bool = True,
) -> tuple[str, bool]:
    """Enqueue one durable request; idempotent per (run, unique key).

    Returns ``(request_id, created)``.
    """
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"unknown request type: {request_type!r}")
    key = request_unique_key(
        run_source_plan_id=run_source_plan_id or "",
        request_type=request_type,
        target_identity=target_identity,
        strategy=strategy,
        logical_key=logical_key,
    )
    ts = now or db_utc_now(conn)
    request_id = new_id("req")
    payload_json = json.dumps(dict(payload or {}), sort_keys=True, separators=(",", ":"))
    cur = conn.execute(
        """
        INSERT INTO scrape_requests (
            id, run_id, run_source_plan_id, source_id, binding_id, request_type,
            request_unique_key, payload_json, strategy, execution_class, priority,
            depth, parent_request_id, status, max_attempts, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
        ON CONFLICT (run_id, request_unique_key) DO NOTHING
        """,
        (
            request_id,
            run_id,
            run_source_plan_id,
            source_id,
            binding_id,
            request_type,
            key,
            payload_json,
            strategy,
            execution_class,
            int(priority),
            int(depth),
            parent_request_id,
            int(max_attempts),
            ts,
            ts,
        ),
    )
    if cur.rowcount == 0:
        existing = conn.execute(
            "SELECT id FROM scrape_requests WHERE run_id = ? AND request_unique_key = ?",
            (run_id, key),
        ).fetchone()
        if commit:
            conn.commit()
        return existing["id"], False
    if commit:
        conn.commit()
    return request_id, True


def get_request(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM scrape_requests WHERE id = ?", (request_id,)
    ).fetchone()


def normalized_target(url: str) -> str:
    return normalize_url(url).normalized


__all__ = [
    "ACQUISITION_REQUEST_TYPES",
    "HOST_NATIVE_REQUEST_TYPES",
    "REQUEST_TYPES",
    "enqueue_request",
    "get_request",
    "normalized_target",
    "request_unique_key",
]
