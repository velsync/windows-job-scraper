"""ExecutionPlanEnvelope and RequestPlan (02 §11, §11.1, ACQ-06).

The host creates and validates the envelope only after a durable request is
claimed.  Slice 3 additionally binds the envelope to the exact immutable
RunSourcePlan/request/attempt before dispatch.  Class-specific payload data may
never rewrite binding, permission, strategy or execution-class identity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Mapping

from jobscraper.net.destination import DestinationPolicy, check_url
from jobscraper.runtime.authorization import require_request_authorized
from jobscraper.runtime.claims import StaleOwnership
from jobscraper.runtime.clock import current_service_epoch, db_utc_now

_ALLOWED_METHODS = frozenset({"GET"})
_FORBIDDEN_HEADER_NAMES = frozenset(
    {"authorization", "cookie", "proxy-authorization", "set-cookie", "x-api-key"}
)


@dataclass(frozen=True)
class RequestPlan:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    expected_content_types: tuple[str, ...] = ()
    timeout_s: float = 30.0
    max_bytes: int = 2_000_000
    purpose: str = "LIST_FETCH"
    allowed_redirects: int | None = None
    cache_policy: str | None = None
    revalidation_headers_allowed: bool = False
    auth_scope_ref: str | None = None
    # Host-owned only.  Adapters/imported data do not gain authority to create
    # or rotate egress merely by setting a source-controlled field (R2-F6).
    egress_requirement: str | None = None
    operation_capability_ref: str | None = None
    expected_operation_class: str = "READ"


@dataclass(frozen=True)
class ExecutionPlanEnvelope:
    plan_id: str
    request_id: str
    attempt_id: str
    run_id: str
    run_source_plan_id: str | None
    source_id: str
    binding_id: str
    binding_revision_id: str
    adapter_id: str
    adapter_version: str
    strategy: str
    execution_class: str
    policy_snapshot_ref: str | None
    permission_profile_id: str
    permission_profile_revision: int
    payload_kind: str
    payload: RequestPlan


def policy_snapshot_reference(plan_row: sqlite3.Row | Mapping[str, object]) -> str:
    """Stable reference to the immutable crawl/rate policy pins on a run plan."""

    crawl = str(plan_row["crawl_policy_snapshot_json"] or "{}")
    rate = str(plan_row["rate_policy_snapshot_json"] or "{}")
    # Frame the two arbitrary JSON-text snapshots structurally rather than with
    # a delimiter that could also occur inside pretty-printed JSON.
    material = json.dumps([crawl, rate], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"run-source-plan://{plan_row['id']}/policy/{digest}"


def validate_envelope(
    envelope: ExecutionPlanEnvelope, policy: DestinationPolicy
) -> None:
    """Host-side class/payload validation before any I/O (fail closed)."""
    if envelope.payload_kind != "REQUEST" or not isinstance(envelope.payload, RequestPlan):
        raise ValueError(f"unsupported payload kind: {envelope.payload_kind!r}")
    if envelope.execution_class != "HTTP":
        raise ValueError(f"HTTP executor cannot run class {envelope.execution_class!r}")
    if not all(
        (
            envelope.plan_id,
            envelope.request_id,
            envelope.attempt_id,
            envelope.run_id,
            envelope.source_id,
            envelope.binding_id,
            envelope.binding_revision_id,
            envelope.adapter_id,
            envelope.adapter_version,
            envelope.strategy,
            envelope.permission_profile_id,
        )
    ):
        raise ValueError("execution envelope contains empty ownership/policy identity")
    plan = envelope.payload
    if plan.timeout_s <= 0:
        raise ValueError("plan timeout_s must be positive")
    if plan.max_bytes <= 0:
        raise ValueError("plan max_bytes must be positive")
    if plan.allowed_redirects is not None and plan.allowed_redirects < 0:
        raise ValueError("plan allowed_redirects must be non-negative")
    if plan.method.upper() not in _ALLOWED_METHODS:
        raise ValueError(
            f"method {plan.method!r} is not an approved read capability "
            "(unknown state-changing HTTP operations are denied)"
        )
    if plan.expected_operation_class != "READ":
        raise ValueError(
            f"operation class {plan.expected_operation_class!r} is not authorized for Slice-3 HTTP"
        )
    for name in plan.headers:
        if name.lower() in _FORBIDDEN_HEADER_NAMES:
            raise ValueError(f"secret-bearing header {name!r} is not allowed in plans")
    if plan.max_bytes > policy.max_bytes:
        raise ValueError(
            f"plan max_bytes {plan.max_bytes} exceeds policy cap {policy.max_bytes}"
        )
    if plan.timeout_s > policy.timeout_s:
        raise ValueError(
            f"plan timeout_s {plan.timeout_s} exceeds policy cap {policy.timeout_s}"
        )
    if plan.allowed_redirects is not None and plan.allowed_redirects > policy.max_redirects:
        raise ValueError(
            f"plan redirect cap {plan.allowed_redirects} exceeds policy cap {policy.max_redirects}"
        )
    check_url(plan.url, policy)


def bind_execution_plan(
    conn: sqlite3.Connection,
    envelope: ExecutionPlanEnvelope,
    *,
    now: str | None = None,
) -> None:
    """Bind one envelope to the exact claimed durable identity before I/O.

    The check and ``request_attempts.execution_plan_id`` write are serialized
    under ``BEGIN IMMEDIATE``.  A malformed envelope therefore cannot replace
    a pinned RunSourcePlan/binding/permission identity, and a second envelope
    cannot be substituted after dispatch identity was recorded.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        ts = now or db_utc_now(conn)
        epoch = current_service_epoch(conn)
        if epoch is None:
            raise StaleOwnership(envelope.request_id, "no active service epoch")
        require_request_authorized(
            conn,
            envelope.request_id,
            attempt_id=envelope.attempt_id,
            require_running=True,
        )
        row = conn.execute(
            """
            SELECT
                req.run_id, req.run_source_plan_id, req.source_id, req.binding_id,
                req.request_type, req.strategy AS request_strategy,
                req.execution_class AS request_execution_class,
                req.current_attempt_id, req.lease_until,
                rsp.binding_revision_id, rsp.adapter_id, rsp.adapter_version,
                rsp.strategy AS plan_strategy,
                rsp.execution_class AS plan_execution_class,
                rsp.permission_profile_id, rsp.permission_profile_revision,
                rsp.crawl_policy_snapshot_json, rsp.rate_policy_snapshot_json,
                a.execution_plan_id, a.service_epoch_id
            FROM scrape_requests req
            JOIN request_attempts a
              ON a.request_id = req.id AND a.attempt_id = req.current_attempt_id
            JOIN run_source_plans rsp ON rsp.id = req.run_source_plan_id
            WHERE req.id = ? AND req.status = 'RUNNING'
              AND req.current_attempt_id = ?
            """,
            (envelope.request_id, envelope.attempt_id),
        ).fetchone()
        if row is None:
            raise StaleOwnership(
                envelope.request_id, "claimed request/attempt/run-plan identity is not resolvable"
            )
        if (row["lease_until"] or "") <= ts:
            raise StaleOwnership(envelope.request_id, "lease already expired")
        if row["service_epoch_id"] != epoch.epoch_id:
            raise StaleOwnership(
                envelope.request_id, "service epoch advanced; attempt ownership invalidated"
            )

        expected = {
            "run_id": row["run_id"],
            "run_source_plan_id": row["run_source_plan_id"],
            "source_id": row["source_id"],
            "binding_id": row["binding_id"],
            "binding_revision_id": row["binding_revision_id"],
            "adapter_id": row["adapter_id"],
            "adapter_version": row["adapter_version"],
            "strategy": row["plan_strategy"],
            "execution_class": row["plan_execution_class"],
            "permission_profile_id": row["permission_profile_id"],
            "permission_profile_revision": int(row["permission_profile_revision"]),
        }
        actual = {
            "run_id": envelope.run_id,
            "run_source_plan_id": envelope.run_source_plan_id,
            "source_id": envelope.source_id,
            "binding_id": envelope.binding_id,
            "binding_revision_id": envelope.binding_revision_id,
            "adapter_id": envelope.adapter_id,
            "adapter_version": envelope.adapter_version,
            "strategy": envelope.strategy,
            "execution_class": envelope.execution_class,
            "permission_profile_id": envelope.permission_profile_id,
            "permission_profile_revision": int(envelope.permission_profile_revision),
        }
        mismatches = [name for name in expected if actual[name] != expected[name]]
        if mismatches:
            raise ValueError(
                "execution envelope does not match immutable request/run-plan pins: "
                + ", ".join(sorted(mismatches))
            )
        if envelope.payload.purpose != row["request_type"]:
            raise ValueError("RequestPlan purpose does not match durable request_type")
        expected_policy_ref = policy_snapshot_reference(
            {
                "id": row["run_source_plan_id"],
                "crawl_policy_snapshot_json": row["crawl_policy_snapshot_json"],
                "rate_policy_snapshot_json": row["rate_policy_snapshot_json"],
            }
        )
        if envelope.policy_snapshot_ref != expected_policy_ref:
            raise ValueError("execution envelope policy_snapshot_ref does not match pinned plan")
        if row["execution_plan_id"] is not None:
            # One attempt owns one execution only.  A second call, even with
            # the same plan id, could otherwise duplicate source network I/O.
            raise StaleOwnership(
                envelope.request_id, "attempt is already bound to an execution plan"
            )
        updated = conn.execute(
            """
            UPDATE request_attempts
               SET execution_plan_id = ?
             WHERE attempt_id = ? AND request_id = ?
               AND execution_plan_id IS NULL
            """,
            (
                envelope.plan_id,
                envelope.attempt_id,
                envelope.request_id,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("failed to bind execution plan identity")
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


__all__ = [
    "ExecutionPlanEnvelope",
    "RequestPlan",
    "bind_execution_plan",
    "policy_snapshot_reference",
    "validate_envelope",
]
