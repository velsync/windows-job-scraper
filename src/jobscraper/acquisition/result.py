"""ResultEnvelope (02 §11.3).

All execution paths normalize to one envelope; the parser does not need to
know whether raw transport was http.client, Playwright, or a later backend.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from jobscraper.acquisition.failures import FailureRecord

_REDACTED_RESPONSE_HEADERS = frozenset(
    {"set-cookie", "www-authenticate", "proxy-authenticate"}
)


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in headers.items() if k.lower() not in _REDACTED_RESPONSE_HEADERS
    }


@dataclass
class ResultEnvelope:
    execution_plan_id: str
    request_id: str
    attempt_id: str
    run_source_plan_id: str | None
    source_id: str
    binding_id: str
    binding_revision_id: str
    adapter_id: str
    adapter_version: str
    strategy: str
    execution_class: str
    requested_url: str
    final_url: str
    status_code: int | None
    headers_redacted: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None
    body: bytes = b""
    body_hash: str | None = None
    normalized_content_hash: str | None = None
    fetched_at: str | None = None
    duration_ms: int = 0
    bytes_downloaded: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    transport: str = "http"
    browser_used: bool = False
    robots_decision: str = "NOT_EVALUATED"
    validators_sent: list[str] = field(default_factory=list)
    was_304: bool = False
    resource_blocking_applied: bool = False
    failure: FailureRecord | None = None

    @staticmethod
    def make_body_hash(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()


__all__ = ["ResultEnvelope"]
