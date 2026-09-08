"""Typed failure model (02 §27).

Failures are structured objects, not result strings. ``SUCCESS_EMPTY`` is
a successful recognized parse outcome and is therefore NOT a failure kind.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class FailureKind(enum.Enum):
    DNS_ERROR = "DNS_ERROR"
    CONNECT_ERROR = "CONNECT_ERROR"
    TLS_ERROR = "TLS_ERROR"
    TIMEOUT = "TIMEOUT"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    RATE_LIMIT = "RATE_LIMIT"
    BLOCKED = "BLOCKED"
    CHALLENGE = "CHALLENGE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CONSENT_INTERSTITIAL = "CONSENT_INTERSTITIAL"
    PARSE_MARKER_MISSING = "PARSE_MARKER_MISSING"
    PARSE_EMPTY = "PARSE_EMPTY"
    PAGINATION_LOOP = "PAGINATION_LOOP"
    DUPLICATE_PAGE = "DUPLICATE_PAGE"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    INVALID_JOB_RECORD = "INVALID_JOB_RECORD"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"
    BROWSER_CRASH = "BROWSER_CRASH"
    WORKER_CRASH = "WORKER_CRASH"
    LEASE_LOST = "LEASE_LOST"
    CANCELLED = "CANCELLED"
    POLICY_REJECTED = "POLICY_REJECTED"


@dataclass(frozen=True)
class FailureRecord:
    kind: FailureKind
    retryable: bool
    source_health_impact: str = "NONE"
    http_status: int | None = None
    source_id: str | None = None
    binding_id: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    attempt_id: str | None = None
    details_redacted: dict = field(default_factory=dict)
    observed_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "retryable": self.retryable,
            "source_health_impact": self.source_health_impact,
            "http_status": self.http_status,
            "details_redacted": dict(self.details_redacted),
        }


__all__ = ["FailureKind", "FailureRecord"]
