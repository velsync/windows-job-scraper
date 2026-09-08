"""ResultEnvelope (02 §11.3) plus the durable evidence references (03 §30).

All execution paths normalize to one envelope; the parser does not need to
know whether raw transport was http.client, Playwright, or a later backend.

Slice 2 completes §11.3 (``body_ref``/``structured_payload_ref``) and adds the
evidence-plane handles the richer acquisition contract needs:

* ``contract_version`` — the envelope participates in the versioned
  cross-component contract (02 ACQ-09);
* ``normalized_content_hash`` — a hash over *content-class-normalized* bytes,
  so formatting noise is not a content change (revalidation/cache membership
  later depends on this, ROAD-04);
* ``body_ref`` / ``structured_payload_ref`` — content-addressed references.
  The envelope is the only place full bytes live; consumers pass references,
  so untrusted bodies are never copied into every evidence row;
* ``security_policy_result`` / ``evidence_refs`` / ``cache_representation_ref``
  — what the host policy decided and which evidence rows belong to this result.

Redaction rules are unchanged: secret-bearing response headers never enter the
envelope (04 SEC-08).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from jobscraper.acquisition.failures import FailureRecord

#: Versioned result contract (02 ACQ-09).  Bumped only by a compatible
#: additive change; parsers record the version they were handed.
RESULT_CONTRACT_VERSION = 2

_REDACTED_RESPONSE_HEADERS = frozenset(
    {"set-cookie", "www-authenticate", "proxy-authenticate"}
)

_TAG_RE = re.compile(rb"<[^>]+>")
_WS_RE = re.compile(rb"\s+")
_SCRIPT_STYLE_RE = re.compile(
    rb"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in headers.items() if k.lower() not in _REDACTED_RESPONSE_HEADERS
    }


def normalized_content_hash(body: bytes, content_type: str | None) -> str | None:
    """Deterministic content hash over *class-normalized* bytes.

    JSON: parsed and re-serialized with sorted keys (key order and whitespace
    are not content).  HTML: scripts/styles removed, tags dropped, whitespace
    collapsed (a template re-flow is not a content change).  Anything else —
    or unparseable content — falls back to the raw bytes: hashing nothing is
    never allowed to look like "no change".
    """
    if body is None:
        return None
    ctype = (content_type or "").lower()
    if not body.strip():
        return hashlib.sha256(b"").hexdigest()
    if "json" in ctype or not ctype:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if payload is not None:
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(canonical.encode()).hexdigest()
    if "html" in " " + ctype or "xml" in ctype:
        stripped = _SCRIPT_STYLE_RE.sub(b" ", body)
        text = _TAG_RE.sub(b" ", stripped)
        collapsed = _WS_RE.sub(b" ", text).strip()
        return hashlib.sha256(collapsed).hexdigest()
    return hashlib.sha256(body).hexdigest()


def refs_for(envelope: "ResultEnvelope") -> tuple[str | None, str | None]:
    """Content-addressed ``body_ref`` and ``structured_payload_ref``.

    A ``304`` (or any empty body) produces no new reference: it re-validates
    the retained representation instead of pretending a body changed.
    """
    if not envelope.body:
        return None, None
    digest = envelope.body_hash or ResultEnvelope.make_body_hash(envelope.body)
    body_ref = f"result://{envelope.attempt_id}/body/{digest[:16]}"
    ctype = (envelope.content_type or "").lower()
    structured = None
    if envelope.structured_payload is not None or "json" in ctype or "xml" in ctype:
        # the retained representation for a structured response *is* the
        # structured payload: §11.3 offers one of the two per result class
        structured = f"result://{envelope.attempt_id}/structured/{digest[:16]}"
    return body_ref, structured


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
    # ---- Slice 2 additions (02 §11.3 completeness + ACQ-09 / 03 §30) ----
    contract_version: int = RESULT_CONTRACT_VERSION
    body_ref: str | None = None
    structured_payload_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    security_policy_result: str = "ALLOWED"
    cache_representation_ref: str | None = None
    structured_payload: object | None = None

    @property
    def content_kind(self) -> str:
        """Coarse content class the provenance quality ordering needs (§39).

        Derived from the response's own declared type — never from what the
        parser hoped to find.
        """
        ctype = (self.content_type or "").lower()
        if "json" in ctype or "xml" in ctype or "x-ndjson" in ctype:
            return "STRUCTURED"
        if "html" in ctype or "xml" in ctype:
            return "HTML"
        return "UNKNOWN"

    @staticmethod
    def make_body_hash(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def finalize(self) -> "ResultEnvelope":
        """Compute the derived hashes/refs (idempotent)."""
        if self.body and not self.was_304:
            if self.body_hash is None:
                self.body_hash = ResultEnvelope.make_body_hash(self.body)
            if self.normalized_content_hash is None:
                self.normalized_content_hash = normalized_content_hash(
                    self.body, self.content_type
                )
        self.body_ref, self.structured_payload_ref = refs_for(self)
        return self

    def redact_headers(self, headers: dict[str, str]) -> dict[str, str]:
        self.headers_redacted = _redact_headers(headers)
        return self.headers_redacted

    def as_evidence(self) -> dict:
        """The redacted, JSON-safe evidence projection of this result.

        Deliberately excludes ``body``/``structured_payload``: the durable
        evidence plane records hashes, references and metadata (03 §30), not
        copies of untrusted content.
        """
        return {
            "contract_version": self.contract_version,
            "execution_plan_id": self.execution_plan_id,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "run_source_plan_id": self.run_source_plan_id,
            "source_id": self.source_id,
            "binding_id": self.binding_id,
            "binding_revision_id": self.binding_revision_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "strategy": self.strategy,
            "execution_class": self.execution_class,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "headers_redacted": dict(self.headers_redacted),
            "body_hash": self.body_hash,
            "normalized_content_hash": self.normalized_content_hash,
            "bytes_downloaded": self.bytes_downloaded,
            "duration_ms": self.duration_ms,
            "redirect_chain": list(self.redirect_chain),
            "transport": self.transport,
            "browser_used": self.browser_used,
            "robots_decision": self.robots_decision,
            "validators_sent": list(self.validators_sent),
            "was_304": self.was_304,
            "resource_blocking_applied": self.resource_blocking_applied,
            "security_policy_result": self.security_policy_result,
            "body_ref": self.body_ref,
            "structured_payload_ref": self.structured_payload_ref,
            "cache_representation_ref": self.cache_representation_ref,
            "failure_kind": self.failure.kind.value if self.failure else None,
        }


__all__ = [
    "RESULT_CONTRACT_VERSION",
    "ResultEnvelope",
    "_redact_headers",
    "normalized_content_hash",
    "refs_for",
]
