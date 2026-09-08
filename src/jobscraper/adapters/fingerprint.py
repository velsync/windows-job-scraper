"""ATS fingerprint classifier (02 §12.1).

Deterministic, evidence-first ATS classification.  Operates only on
already-acquired content (URL + body bytes + content-type header) — no I/O
and no database access.  The output is a versioned ``AtsFingerprint`` carrying
the classified family, confidence score, supporting evidence, and the
recommended built-in adapter id.

Evidence signals inspected:

* **host/domain patterns** — known ATS host suffixes and subdomains;
* **script URLs** — embedded script tags pointing at known ATS endpoints;
* **iframe hosts** — iframes hosted on known ATS domains;
* **HTML markers** — ``data-*`` attributes and class names specific to
  known ATS platforms;
* **canonical links** — ``<link rel="canonical">`` pointing at a known
  ATS domain;
* **JSON-LD** — ``@type: JobPosting`` URLs pointing at a known ATS domain;
* **JSON keys** — response shapes characteristic of a known ATS API;
* **public API paths** — URL path patterns matching a known ATS API.

All matching is against the versioned endpoint table in
:mod:`jobscraper.acquisition.atsendpoints`.

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md §12.1.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from jobscraper.acquisition.atsendpoints import (
    ATS_ENDPOINT_SPECS,
    ENDPOINT_RULES_VERSION,
    identify_url,
    AtsEndpointSpec,
)
from jobscraper.net.urlnorm import has_embedded_credentials

#: Fingerprint contract version — bumped only by a compatible additive change.
FINGERPRINT_VERSION = 1

# ---------------------------------------------------------------------------
# Provider → recommended built-in adapter id
# ---------------------------------------------------------------------------
# When a dedicated provider adapter is graduated (e.g. greenhouse), its id
# replaces the generic feed adapter id below.  The current slice ships only
# ``json_api_feed``; all recognized providers therefore map to it for now.

_PROVIDER_ADAPTER: dict[str, str] = {
    "GREENHOUSE": "greenhouse",
    "LEVER": "lever",
    "ASHBY": "ashby",
}


# ---------------------------------------------------------------------------
# Confidence tiers
# ---------------------------------------------------------------------------
# Each evidence kind carries a weight.  Weights are summed (clamped to 1.0)
# to produce the final confidence score.  The tiers are:
#
#   STRONG_HOST alone          ≥ 0.90   (label-boundary ATS host match)
#   HOST + corroborating       ≥ 0.95   (host + script / iframe / canonical)
#   WEAK_HOST alone            ≥ 0.50   (known suffix, unknown subdomain)
#   script/iframe/canonical    ≥ 0.70   (standalone content signals)
#   JSON-LD / JSON keys        ≥ 0.70   (structured data signals)
#   HTML class markers         ≥ 0.60   (weaker content signals)
#   No signal                  < 0.50   (unclassified)

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


# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-_]*[a-z0-9])?(:\d{2,5})?$", re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(
    rb"""(?:src|data-src)\s*=\s*["']?(https?://[^"'\s>]+)""", re.IGNORECASE
)
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
_ATTR_RE = re.compile(rb"""(\w[\w-]*)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _parse_url_host(url: str) -> str | None:
    """Extract lowercased host from a URL, or None if unusable."""
    if not isinstance(url, str) or not url.strip():
        return None
    if has_embedded_credentials(url):
        return None
    if not _SCHEME_RE.match(url.strip()):
        return None
    rest = url.strip().split("://", 1)[1]
    authority, *_ = rest.split("/", 1)
    host = authority.lower().rsplit(":", 1)[0] if ":" in authority else authority.lower()
    if not host or not _HOST_RE.match(authority.lower()):
        return None
    return host


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    """Label-boundary suffix match."""
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _find_spec_for_host(host: str) -> AtsEndpointSpec | None:
    """Return the first ATS spec whose suffix matches, or None."""
    for spec in ATS_ENDPOINT_SPECS:
        if _host_matches(host, spec.host_suffixes):
            return spec
    return None


def _host_strength(host: str, spec: AtsEndpointSpec) -> str:
    if host in spec.api_hosts or host in spec.hosted_hosts:
        return "STRONG"
    return "WEAK"


# ---------------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------------

def _url_host_evidence(url_host: str) -> list[dict]:
    """Check the URL's own host against the ATS endpoint table."""
    if not url_host:
        return []
    match = identify_url(f"https://{url_host}/")
    if match.provider and match.strength in ("JOB", "BOARD", "HOST_ONLY"):
        strength = "HOST_STRONG" if match.strength != "HOST_ONLY" else "HOST_WEAK"
        return [{
            "kind": "HOST",
            "value": url_host,
            "provider": match.provider,
            "strength": strength,
        }]
    return []


def _script_url_evidence(body: bytes) -> list[dict]:
    """Extract script src URLs and check for known ATS hosts."""
    evidence: list[dict] = []
    for m in _SCRIPT_RE.finditer(body):
        script_url = m.group(1).decode("utf-8", "replace")
        host = _parse_url_host(script_url)
        if not host:
            continue
        for spec in ATS_ENDPOINT_SPECS:
            if _host_matches(host, spec.host_suffixes):
                evidence.append({
                    "kind": "SCRIPT_URL",
                    "value": script_url,
                    "provider": spec.provider,
                    "strength": "SCRIPT_URL",
                })
                break
    return evidence


def _iframe_host_evidence(body: bytes) -> list[dict]:
    """Extract iframe src URLs and check for known ATS hosts."""
    evidence: list[dict] = []
    for m in _IFRAME_SRC_RE.finditer(body):
        iframe_url = m.group(1).decode("utf-8", "replace")
        host = _parse_url_host(iframe_url)
        if not host:
            continue
        for spec in ATS_ENDPOINT_SPECS:
            if _host_matches(host, spec.host_suffixes):
                evidence.append({
                    "kind": "IFRAME_HOST",
                    "value": iframe_url,
                    "provider": spec.provider,
                    "strength": "IFRAME_HOST",
                })
                break
    return evidence


def _canonical_link_evidence(body: bytes) -> list[dict]:
    """Check <link rel="canonical"> for known ATS hosts."""
    evidence: list[dict] = []
    for m in _CANONICAL_RE.finditer(body):
        href = m.group(1).decode("utf-8", "replace")
        host = _parse_url_host(href)
        if not host:
            continue
        for spec in ATS_ENDPOINT_SPECS:
            if _host_matches(host, spec.host_suffixes):
                evidence.append({
                    "kind": "CANONICAL_LINK",
                    "value": href,
                    "provider": spec.provider,
                    "strength": "CANONICAL_LINK",
                })
                break
    return evidence


def _html_marker_evidence(body: bytes) -> list[dict]:
    """Check for ATS-specific HTML data attributes and class names."""
    evidence: list[dict] = []
    body_lower = body.lower()

    # Greenhouse markers
    gh_markers = [b"grnhse_app", b"grnhse_app_container", b"grnhse-embed",
                  b"data-department", b"boards.greenhouse.io/embed"]
    for marker in gh_markers:
        if marker in body_lower:
            evidence.append({
                "kind": "HTML_MARKER",
                "value": marker.decode("utf-8", "replace"),
                "provider": "GREENHOUSE",
                "strength": "HTML_MARKER",
            })
            break  # one Greenhouse marker is enough

    # Lever markers
    lever_markers = [b"postings-group", b"data-qa=\"posting\"",
                     b"lever-jobs-app", b"lever.co/embed"]
    for marker in lever_markers:
        if marker in body_lower:
            evidence.append({
                "kind": "HTML_MARKER",
                "value": marker.decode("utf-8", "replace"),
                "provider": "LEVER",
                "strength": "HTML_MARKER",
            })
            break

    # Ashby markers
    ashby_markers = [b"ashbyhq.com/embed", b"ashby_job_board"]
    for marker in ashby_markers:
        if marker in body_lower:
            evidence.append({
                "kind": "HTML_MARKER",
                "value": marker.decode("utf-8", "replace"),
                "provider": "ASHBY",
                "strength": "HTML_MARKER",
            })
            break

    return evidence


def _json_ld_evidence(body: bytes, content_type: str) -> list[dict]:
    """Inspect JSON-LD for known ATS URLs."""
    evidence: list[dict] = []
    # Try JSON-LD blocks embedded in HTML
    for m in _JSON_LD_RE.finditer(body):
        try:
            payload = json.loads(m.group(1))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        _collect_json_ld_urls(payload, evidence)

    # Also try top-level JSON if content type is JSON-LD
    if "ld+json" in content_type.lower() or "json" in content_type.lower():
        try:
            payload = json.loads(body.decode("utf-8"))
            _collect_json_ld_urls(payload, evidence)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return evidence


def _collect_json_ld_urls(payload: Any, evidence: list[dict]) -> None:
    """Walk a parsed JSON-LD object looking for ATS provider URLs."""
    if isinstance(payload, Mapping):
        for key in ("url", "applicationUrl", "hiringOrganization"):
            value = payload.get(key)
            if isinstance(value, str):
                _check_url_field(value, "JSON_LD_URL", evidence)
            elif isinstance(value, Mapping):
                _collect_json_ld_urls(value, evidence)
        # Recurse into nested objects/lists
        for key, value in payload.items():
            if key in ("url", "applicationUrl", "hiringOrganization"):
                continue
            if isinstance(value, (Mapping, list)):
                _collect_json_ld_urls(value, evidence)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, (Mapping, list)):
                _collect_json_ld_urls(item, evidence)


def _check_url_field(url: str, kind: str, evidence: list[dict]) -> None:
    """If a URL field points at a known ATS host, add evidence."""
    host = _parse_url_host(url)
    if not host:
        return
    for spec in ATS_ENDPOINT_SPECS:
        if _host_matches(host, spec.host_suffixes):
            evidence.append({
                "kind": kind,
                "value": url,
                "provider": spec.provider,
                "strength": kind,
            })
            break


def _json_api_shape_evidence(
    body: bytes, content_type: str, url: str,
) -> list[dict]:
    """Check JSON response shapes characteristic of known ATS APIs."""
    if "json" not in content_type.lower() and content_type:
        return []
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []

    evidence: list[dict] = []
    url_host = _parse_url_host(url) or ""

    # Greenhouse: object with "jobs" key at greenhouse host
    if isinstance(payload, Mapping) and "jobs" in payload:
        if _host_matches(url_host, ("greenhouse.io",)):
            evidence.append({
                "kind": "JSON_API_SHAPE",
                "value": "greenhouse:jobs_key",
                "provider": "GREENHOUSE",
                "strength": "JSON_API_SHAPE",
            })

    # Lever: bare array at lever host
    if isinstance(payload, list) and len(payload) > 0:
        if _host_matches(url_host, ("lever.co",)):
            evidence.append({
                "kind": "JSON_API_SHAPE",
                "value": "lever:bare_array",
                "provider": "LEVER",
                "strength": "JSON_API_SHAPE",
            })

    # Ashby: object with "status"+"data" containing "jobs"
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if (
            payload.get("status") == "success"
            and isinstance(data, Mapping)
            and "jobs" in data
            and _host_matches(url_host, ("ashbyhq.com",))
        ):
            evidence.append({
                "kind": "JSON_API_SHAPE",
                "value": "ashby:status_data_jobs",
                "provider": "ASHBY",
                "strength": "JSON_API_SHAPE",
            })

    return evidence


# ---------------------------------------------------------------------------
# Confidence computation
# ---------------------------------------------------------------------------

def _compute_confidence(evidence: tuple[dict, ...]) -> float:
    """Aggregate evidence weights into a single confidence score.

    Only the highest-weight evidence per unique strength kind is counted
    (so a page with three Greenhouse script tags is not triple-confident).
    Corroboration from *different* kinds does raise confidence.
    """
    best_by_kind: dict[str, float] = {}
    for entry in evidence:
        kind = entry.get("strength", "")
        weight = _WEIGHTS.get(kind, 0.5)
        best_by_kind[kind] = max(best_by_kind.get(kind, 0.0), weight)

    if not best_by_kind:
        return 0.0

    # The highest-weight evidence kind sets the base.
    base = max(best_by_kind.values())
    # Each additional corroborating kind adds a diminishing bonus.
    bonus = 0.0
    for kind, weight in sorted(best_by_kind.items(), key=lambda x: -x[1]):
        if weight == base:
            continue
        bonus += max(0.0, (1.0 - base + weight) * 0.15)
        if base + bonus >= 0.98:
            break

    return round(_clamp(base + bonus), 4)


def _pick_provider(evidence: tuple[dict, ...]) -> str | None:
    """The provider asserted by the highest-confidence evidence."""
    provider_scores: dict[str, float] = {}
    for entry in evidence:
        provider = entry.get("provider")
        if not provider:
            continue
        kind = entry.get("strength", "")
        weight = _WEIGHTS.get(kind, 0.5)
        provider_scores[provider] = max(provider_scores.get(provider, 0.0), weight)
    if not provider_scores:
        return None
    return max(provider_scores, key=lambda p: provider_scores[p])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AtsFingerprint:
    """Versioned ATS fingerprint (02 §12.1).

    ``family`` is the provider identifier (e.g. ``GREENHOUSE``) or ``None``
    when no signal was found.  ``confidence`` is a float in ``[0, 1]``.
    ``evidence`` is an immutable tuple of dicts, each carrying ``kind``,
    ``value``, ``provider`` (when applicable), and ``strength``.
    ``recommended_adapter_id`` is the built-in adapter id the router should
    try first, or ``None`` when no signal was found.
    """

    family: str | None
    confidence: float
    evidence: tuple[dict[str, Any], ...]
    recommended_adapter_id: str | None
    fingerprint_version: int = FINGERPRINT_VERSION

    def as_dict(self) -> dict:
        return {
            "family": self.family,
            "confidence": self.confidence,
            "evidence": [dict(e) for e in self.evidence],
            "recommended_adapter_id": self.recommended_adapter_id,
            "fingerprint_version": self.fingerprint_version,
        }


def classify_content(
    *,
    url: str,
    body: bytes | None,
    content_type: str = "",
) -> AtsFingerprint:
    """Classify already-acquired content into an ATS fingerprint.

    Parameters
    ----------
    url:
        The URL that produced this content.
    body:
        The response body bytes.  ``None`` or empty is handled gracefully.
    content_type:
        The response ``Content-Type`` header value.

    Returns
    -------
    AtsFingerprint
        Versioned fingerprint with family, confidence, evidence, and
        recommended adapter id.
    """
    body = body or b""
    evidence: list[dict] = []

    # 1. Host/domain pattern — the cheapest, strongest signal.
    url_host = _parse_url_host(url)
    if url_host:
        # a) Direct endpoint match (job URL on an ATS host)
        url_match = identify_url(url)
        if url_match.provider:
            strength = "HOST_ENDPOINT" if url_match.strength in ("JOB", "BOARD") else "HOST_WEAK"
            evidence.append({
                "kind": "HOST",
                "value": url_host,
                "provider": url_match.provider,
                "strength": strength,
            })
        else:
            # b) Host suffix match (known suffix, unknown subdomain)
            spec = _find_spec_for_host(url_host)
            if spec:
                evidence.append({
                    "kind": "HOST",
                    "value": url_host,
                    "provider": spec.provider,
                    "strength": "HOST_WEAK",
                })

    # 2. Script URLs
    evidence.extend(_script_url_evidence(body))

    # 3. Iframe hosts
    evidence.extend(_iframe_host_evidence(body))

    # 4. HTML markers (data attributes, class names)
    evidence.extend(_html_marker_evidence(body))

    # 5. Canonical links
    evidence.extend(_canonical_link_evidence(body))

    # 6. JSON-LD URLs
    evidence.extend(_json_ld_evidence(body, content_type))

    # 7. JSON API response shapes
    evidence.extend(_json_api_shape_evidence(body, content_type, url))

    # Freeze evidence
    evidence_tuple = tuple(evidence)

    # Pick provider and compute confidence
    provider = _pick_provider(evidence_tuple)
    confidence = _compute_confidence(evidence_tuple)
    adapter_id = _PROVIDER_ADAPTER.get(provider) if provider else None

    return AtsFingerprint(
        family=provider,
        confidence=confidence,
        evidence=evidence_tuple,
        recommended_adapter_id=adapter_id,
    )


__all__ = [
    "AtsFingerprint",
    "FINGERPRINT_VERSION",
    "classify_content",
]
