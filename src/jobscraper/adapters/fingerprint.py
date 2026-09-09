"""ATS fingerprint classifier (02 §12.1).

Deterministic, evidence-first ATS classification over already-acquired content.
The classifier performs no I/O and no database access.  Evidence remains
provider-tagged so confidence is calculated for one provider at a time: a
Lever marker can never make a weak Greenhouse hypothesis more confident (or
vice versa).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from jobscraper.acquisition.atsendpoints import ATS_ENDPOINT_SPECS, AtsEndpointSpec, identify_url
from jobscraper.net.urlnorm import has_embedded_credentials

# S2.4 corrective: scoring semantics changed, so historical evidence must name
# a new classifier version rather than being interpreted under the v1 rules.
FINGERPRINT_VERSION = 2

_PROVIDER_ADAPTER: dict[str, str] = {
    "GREENHOUSE": "greenhouse",
    "LEVER": "lever",
    "ASHBY": "ashby",
}

_WEIGHTS: dict[str, float] = {
    "HOST_STRONG": 0.92,
    "HOST_WEAK": 0.50,
    "SCRIPT_URL": 0.70,
    "IFRAME_HOST": 0.70,
    "CANONICAL_LINK": 0.70,
    "JSON_LD_URL": 0.72,
    "HTML_MARKER": 0.60,
    "JSON_API_SHAPE": 0.70,
    "HOST_ENDPOINT": 0.92,
}


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-_]*[a-z0-9])?(:\d{2,5})?$", re.IGNORECASE)
_IFRAME_SRC_RE = re.compile(
    rb"""<iframe[^>]+src\s*=\s*["']?(https?://[^"'\s>]+)""", re.IGNORECASE
)
_CANONICAL_RE = re.compile(
    rb"""<link[^>]+rel\s*=\s*["']canonical["'][^>]+href\s*=\s*["']?(https?://[^"'\s>]+)""",
    re.IGNORECASE,
)
_SCRIPT_RE = re.compile(
    rb"""<script[^>]+src\s*=\s*["']?(https?://[^"'\s>]+)""", re.IGNORECASE,
)
_JSON_LD_RE = re.compile(
    rb"""<script[^>]*type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)


def _parse_url_host(url: str) -> str | None:
    if not isinstance(url, str) or not url.strip() or has_embedded_credentials(url):
        return None
    candidate = url.strip()
    if not _SCHEME_RE.match(candidate):
        return None
    rest = candidate.split("://", 1)[1]
    authority, *_ = rest.split("/", 1)
    host = authority.lower().rsplit(":", 1)[0] if ":" in authority else authority.lower()
    if not host or not _HOST_RE.match(authority.lower()):
        return None
    return host


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _find_spec_for_host(host: str) -> AtsEndpointSpec | None:
    for spec in ATS_ENDPOINT_SPECS:
        if _host_matches(host, spec.host_suffixes):
            return spec
    return None


def _script_url_evidence(body: bytes) -> list[dict]:
    evidence: list[dict] = []
    for match in _SCRIPT_RE.finditer(body):
        url = match.group(1).decode("utf-8", "replace")
        host = _parse_url_host(url)
        if not host:
            continue
        for spec in ATS_ENDPOINT_SPECS:
            if _host_matches(host, spec.host_suffixes):
                evidence.append({"kind": "SCRIPT_URL", "value": url,
                                 "provider": spec.provider, "strength": "SCRIPT_URL"})
                break
    return evidence


def _iframe_host_evidence(body: bytes) -> list[dict]:
    evidence: list[dict] = []
    for match in _IFRAME_SRC_RE.finditer(body):
        url = match.group(1).decode("utf-8", "replace")
        host = _parse_url_host(url)
        if not host:
            continue
        for spec in ATS_ENDPOINT_SPECS:
            if _host_matches(host, spec.host_suffixes):
                evidence.append({"kind": "IFRAME_HOST", "value": url,
                                 "provider": spec.provider, "strength": "IFRAME_HOST"})
                break
    return evidence


def _canonical_link_evidence(body: bytes) -> list[dict]:
    evidence: list[dict] = []
    for match in _CANONICAL_RE.finditer(body):
        url = match.group(1).decode("utf-8", "replace")
        host = _parse_url_host(url)
        if not host:
            continue
        for spec in ATS_ENDPOINT_SPECS:
            if _host_matches(host, spec.host_suffixes):
                evidence.append({"kind": "CANONICAL_LINK", "value": url,
                                 "provider": spec.provider, "strength": "CANONICAL_LINK"})
                break
    return evidence


def _html_marker_evidence(body: bytes) -> list[dict]:
    evidence: list[dict] = []
    lower = body.lower()
    markers = (
        ("GREENHOUSE", (b"grnhse_app", b"grnhse_app_container", b"grnhse-embed",
                        b"data-department", b"boards.greenhouse.io/embed")),
        ("LEVER", (b"postings-group", b"data-qa=\"posting\"", b"lever-jobs-app",
                   b"lever.co/embed")),
        ("ASHBY", (b"ashbyhq.com/embed", b"ashby_job_board")),
    )
    for provider, provider_markers in markers:
        for marker in provider_markers:
            if marker in lower:
                evidence.append({"kind": "HTML_MARKER",
                                 "value": marker.decode("utf-8", "replace"),
                                 "provider": provider, "strength": "HTML_MARKER"})
                break
    return evidence


def _check_url_field(url: str, kind: str, evidence: list[dict]) -> None:
    host = _parse_url_host(url)
    if not host:
        return
    for spec in ATS_ENDPOINT_SPECS:
        if _host_matches(host, spec.host_suffixes):
            evidence.append({"kind": kind, "value": url,
                             "provider": spec.provider, "strength": kind})
            return


def _collect_json_ld_urls(payload: Any, evidence: list[dict]) -> None:
    if isinstance(payload, Mapping):
        for key in ("url", "applicationUrl", "hiringOrganization"):
            value = payload.get(key)
            if isinstance(value, str):
                _check_url_field(value, "JSON_LD_URL", evidence)
            elif isinstance(value, Mapping):
                _collect_json_ld_urls(value, evidence)
        for key, value in payload.items():
            if key not in ("url", "applicationUrl", "hiringOrganization") and isinstance(
                value, (Mapping, list)
            ):
                _collect_json_ld_urls(value, evidence)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, (Mapping, list)):
                _collect_json_ld_urls(item, evidence)


def _json_ld_evidence(body: bytes, content_type: str) -> list[dict]:
    evidence: list[dict] = []
    for match in _JSON_LD_RE.finditer(body):
        try:
            payload = json.loads(match.group(1))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        _collect_json_ld_urls(payload, evidence)
    if "ld+json" in content_type.lower() or "json" in content_type.lower():
        try:
            _collect_json_ld_urls(json.loads(body.decode("utf-8")), evidence)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return evidence


def _json_api_shape_evidence(body: bytes, content_type: str, url: str) -> list[dict]:
    if "json" not in content_type.lower() and content_type:
        return []
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    host = _parse_url_host(url) or ""
    evidence: list[dict] = []
    if isinstance(payload, Mapping) and "jobs" in payload and _host_matches(host, ("greenhouse.io",)):
        evidence.append({"kind": "JSON_API_SHAPE", "value": "greenhouse:jobs_key",
                         "provider": "GREENHOUSE", "strength": "JSON_API_SHAPE"})
    if isinstance(payload, list) and payload and _host_matches(host, ("lever.co",)):
        evidence.append({"kind": "JSON_API_SHAPE", "value": "lever:bare_array",
                         "provider": "LEVER", "strength": "JSON_API_SHAPE"})
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if (payload.get("status") == "success" and isinstance(data, Mapping)
                and "jobs" in data and _host_matches(host, ("ashbyhq.com",))):
            evidence.append({"kind": "JSON_API_SHAPE", "value": "ashby:status_data_jobs",
                             "provider": "ASHBY", "strength": "JSON_API_SHAPE"})
    return evidence


def _compute_confidence(evidence: tuple[dict, ...]) -> float:
    """Aggregate corroborating evidence belonging to one provider hypothesis."""
    best_by_kind: dict[str, float] = {}
    for entry in evidence:
        strength = entry.get("strength", "")
        weight = _WEIGHTS.get(strength, 0.5)
        best_by_kind[strength] = max(best_by_kind.get(strength, 0.0), weight)
    if not best_by_kind:
        return 0.0
    base = max(best_by_kind.values())
    bonus = 0.0
    for _kind, weight in sorted(best_by_kind.items(), key=lambda item: -item[1]):
        if weight == base:
            continue
        bonus += max(0.0, (1.0 - base + weight) * 0.15)
        if base + bonus >= 0.98:
            break
    return round(_clamp(base + bonus), 4)


def _provider_confidences(evidence: tuple[dict, ...]) -> dict[str, float]:
    """Score each provider independently; cross-provider corroboration is forbidden."""
    grouped: dict[str, list[dict]] = {}
    for entry in evidence:
        provider = entry.get("provider")
        if provider:
            grouped.setdefault(provider, []).append(entry)
    return {
        provider: _compute_confidence(tuple(entries))
        for provider, entries in grouped.items()
    }


def _pick_provider(provider_scores: Mapping[str, float]) -> str | None:
    if not provider_scores:
        return None
    ranked = sorted(provider_scores.items(), key=lambda item: (-item[1], item[0]))
    # Exact ties are contradictory evidence, not a coin flip based on dict order.
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


@dataclass(frozen=True)
class AtsFingerprint:
    family: str | None
    confidence: float
    evidence: tuple[dict[str, Any], ...]
    recommended_adapter_id: str | None
    fingerprint_version: int = FINGERPRINT_VERSION

    def as_dict(self) -> dict:
        return {
            "family": self.family,
            "confidence": self.confidence,
            "evidence": [dict(item) for item in self.evidence],
            "recommended_adapter_id": self.recommended_adapter_id,
            "fingerprint_version": self.fingerprint_version,
        }


def classify_content(*, url: str, body: bytes | None, content_type: str = "") -> AtsFingerprint:
    body = body or b""
    evidence: list[dict] = []
    url_host = _parse_url_host(url)
    if url_host:
        url_match = identify_url(url)
        if url_match.provider:
            strength = "HOST_ENDPOINT" if url_match.strength in ("JOB", "BOARD") else "HOST_WEAK"
            evidence.append({"kind": "HOST", "value": url_host,
                             "provider": url_match.provider, "strength": strength})
        else:
            spec = _find_spec_for_host(url_host)
            if spec:
                evidence.append({"kind": "HOST", "value": url_host,
                                 "provider": spec.provider, "strength": "HOST_WEAK"})
    evidence.extend(_script_url_evidence(body))
    evidence.extend(_iframe_host_evidence(body))
    evidence.extend(_html_marker_evidence(body))
    evidence.extend(_canonical_link_evidence(body))
    evidence.extend(_json_ld_evidence(body, content_type))
    evidence.extend(_json_api_shape_evidence(body, content_type, url))

    frozen = tuple(evidence)
    scores = _provider_confidences(frozen)
    provider = _pick_provider(scores)
    confidence = scores.get(provider, 0.0) if provider else 0.0
    return AtsFingerprint(
        family=provider,
        confidence=confidence,
        evidence=frozen,
        recommended_adapter_id=_PROVIDER_ADAPTER.get(provider) if provider else None,
    )


__all__ = ["AtsFingerprint", "FINGERPRINT_VERSION", "classify_content"]
