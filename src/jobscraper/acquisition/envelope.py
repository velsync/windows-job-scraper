"""ExecutionPlanEnvelope and RequestPlan (02 §11, §11.1, ACQ-06).

The host creates and validates the envelope only after a durable request
is claimed (ARC-05). Adapters never receive raw secrets; plan headers are
secret-free by construction and validated as such. Only host-approved
read-only methods are accepted (an explicitly supported read-only
search/filter POST would be a declared capability — Slice 1 grants none).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from jobscraper.net.destination import DestinationPolicy, check_url

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


def validate_envelope(
    envelope: ExecutionPlanEnvelope, policy: DestinationPolicy
) -> None:
    """Host-side validation before any I/O (fail closed)."""
    if envelope.payload_kind != "REQUEST" or not isinstance(envelope.payload, RequestPlan):
        raise ValueError(f"unsupported payload kind: {envelope.payload_kind!r}")
    if envelope.execution_class != "HTTP":
        raise ValueError(f"HTTP executor cannot run class {envelope.execution_class!r}")
    plan = envelope.payload
    if plan.method.upper() not in _ALLOWED_METHODS:
        raise ValueError(
            f"method {plan.method!r} is not an approved read capability "
            "(unknown state-changing HTTP operations are denied)"
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
    # Destination policy check (fails closed, typed rejection).
    check_url(plan.url, policy)


__all__ = ["ExecutionPlanEnvelope", "RequestPlan", "validate_envelope"]
