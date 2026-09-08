"""Origin resolver (02 §32) — network-inert by construction.

For an aggregator or intermediate source:

    observation
    → bounded redirect unwrap        (already-recorded chain only)
    → tracking cleanup               (net/urlnorm, versioned parameter list)
    → ATS fingerprint                (acquisition/atsendpoints, versioned)
    → origin provider / board / job id
    → direct application URL candidate
    → evidence-backed origin relation

Hard rules implemented here:

* **No I/O.** The resolver never opens a socket, never "follows" a redirect,
  and never fetches a better origin: it consumes the durable evidence the host
  already recorded (final URL + redirect chain) and the observation's own URL
  candidates. A durable bounded unwrap is frontier work and belongs to ROAD-04.
* **Never overwrite the discovery path** (02 §31): the original source link is
  returned untouched alongside the resolution.
* **Confidence, not guesswork.** Below :data:`ORIGIN_CONFIDENCE_THRESHOLD` the
  result is ``UNRESOLVED`` with no origin fields at all, so neither dedup nor
  presentation can consume a half-trusted origin.
* **Hostile input refusal.** Non-http(s) schemes, embedded credentials and
  control characters are rejected and recorded, never resolved or re-emitted
  as a link (04 §5.1, SEC-03).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from jobscraper.acquisition.atsendpoints import (
    ENDPOINT_RULES_VERSION,
    AtsMatch,
    identify_url,
)
from jobscraper.net.urlnorm import has_embedded_credentials, normalize_url

#: Resolver contract version (02 ACQ-09 versioned data contracts).
ORIGIN_RESOLVER_VERSION = 1

#: Below this, a resolution is UNRESOLVED.  A single job-specific ATS match is
#: enough to be useful; a board-only or host-only signal never is.
ORIGIN_CONFIDENCE_THRESHOLD = 0.75

#: Bounded redirect unwrap: how many already-recorded hops may be examined,
#: nearest-to-final first.  The bound is a stop policy, not a budget to spend.
MAX_UNWRAP_HOPS = 4

_CONFIDENCE = {
    "CORROBORATED": 0.95,
    "JOB": 0.90,
    "JOB_VIA_REDIRECT": 0.85,
    "BOARD": 0.40,
    "HOST_ONLY": 0.20,
    "CONFLICT": 0.30,
    "NONE": 0.0,
}


class OriginStatus(str, enum.Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class OriginEvidence:
    """One recorded reason in the origin-resolution evidence chain (03 §30)."""

    kind: str
    field: str
    value: str
    pattern_id: str | None = None
    strength: str = "SUPPORTING"
    observed_at: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "field": self.field,
            "value": self.value,
            "pattern_id": self.pattern_id,
            "strength": self.strength,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class OriginResolution:
    status: OriginStatus
    confidence: float
    evidence: tuple[OriginEvidence, ...] = ()
    origin_provider: str | None = None
    origin_board: str | None = None
    origin_job_id: str | None = None
    origin_url: str | None = None
    direct_application_url: str | None = None
    preserved_source_url: str | None = None
    same_host_as_source: bool = False
    conflict: Mapping[str, str] | None = None
    rejected_candidates: tuple[dict, ...] = ()
    resolver_version: int = ORIGIN_RESOLVER_VERSION
    endpoint_rules_version: int = ENDPOINT_RULES_VERSION
    resolved_at: str = ""
    max_unwrap_hops: int = MAX_UNWRAP_HOPS
    hops_considered: int = 0

    @property
    def resolved(self) -> bool:
        return self.status is OriginStatus.RESOLVED

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "confidence": self.confidence,
            "origin_provider": self.origin_provider,
            "origin_board": self.origin_board,
            "origin_job_id": self.origin_job_id,
            "origin_url": self.origin_url,
            "direct_application_url": self.direct_application_url,
            "preserved_source_url": self.preserved_source_url,
            "same_host_as_source": self.same_host_as_source,
            "conflict": dict(self.conflict) if self.conflict else None,
            "rejected_candidates": [dict(item) for item in self.rejected_candidates],
            "evidence": [item.as_dict() for item in self.evidence],
            "resolver_version": self.resolver_version,
            "endpoint_rules_version": self.endpoint_rules_version,
            "resolved_at": self.resolved_at,
            "max_unwrap_hops": self.max_unwrap_hops,
            "hops_considered": self.hops_considered,
        }


def _clean(url: str) -> tuple[str | None, str | None]:
    """Tracking-cleaned identity for comparison, or ``(None, rejection)``.

    The resolver applies its own refusal rules before any matching — it never
    trusts an inbound URL field to already be safe (04 §5.1): credentials
    embedded in a URL and non-http(s) schemes are rejected, not normalized
    away, and the rejection is recorded as evidence.
    """
    if not isinstance(url, str) or not url.strip():
        return None, "EMPTY_URL"
    if has_embedded_credentials(url):
        return None, "EMBEDDED_CREDENTIALS"
    try:
        normalized = normalize_url(url)
    except Exception as exc:  # UrlNormalizationError and any parse failure
        return None, type(exc).__name__
    if normalized.scheme not in ("http", "https"):
        return None, "UNSUPPORTED_SCHEME"
    return normalized.normalized, None


def _candidate_order(
    *,
    canonical_job_url: str | None,
    application_url: str | None,
    final_url: str | None,
    redirect_chain: Iterable[str] | None,
    raw_source_url: str | None,
    discovery_url: str | None,
) -> tuple[list[tuple[str, str]], int]:
    """Fixed, deduplicated candidate order + how many hops were examined.

    Observation-reported links are preferred over transport metadata, which is
    preferred over the entry/discovery path (which must never win by default).
    Redirect hops are taken from the *recorded* chain, bounded, and nearest to
    the final URL first.
    """
    chain = [str(hop) for hop in (redirect_chain or ()) if isinstance(hop, str) and hop.strip()]
    considered = min(len(chain), MAX_UNWRAP_HOPS)
    unwrapped = list(reversed(chain[-MAX_UNWRAP_HOPS:])) if considered else []
    raw_pairs: list[tuple[str, str | None]] = [
        ("canonical_job_url", canonical_job_url),
        ("application_url", application_url),
        ("final_url", final_url),
        *((f"redirect_hop[{index}]", hop) for index, hop in enumerate(unwrapped)),
        ("raw_source_url", raw_source_url),
        ("discovery_url", discovery_url),
    ]
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in raw_pairs:
        if not value or value in seen:
            continue
        seen.add(value)
        candidates.append((name, value))
    return candidates, considered


def resolve_origin(
    *,
    discovery_url: str | None,
    raw_source_url: str | None,
    canonical_job_url: str | None,
    application_url: str | None,
    redirect_chain: Iterable[str] | None = (),
    final_url: str | None,
    observed_at: str,
    fingerprint_family: str | None = None,
) -> OriginResolution:
    """Resolve one observation's employer origin from existing evidence only."""
    candidates, hops_considered = _candidate_order(
        canonical_job_url=canonical_job_url,
        application_url=application_url,
        final_url=final_url,
        redirect_chain=redirect_chain,
        raw_source_url=raw_source_url,
        discovery_url=discovery_url,
    )

    evidence: list[OriginEvidence] = []
    rejected: list[dict] = []
    job_matches: dict[str, tuple[AtsMatch, str]] = {}  # field -> (match, cleaned)
    board_matches: dict[str, AtsMatch] = {}
    host_matches: dict[str, AtsMatch] = {}

    # Candidate priority is the documented order, not an accident of field
    # names: the earliest matching candidate supplies the resolved URL, while
    # later matches only corroborate the identity (confidence raising).
    order = {name: index for index, (name, _value) in enumerate(candidates)}

    for name, value in candidates:
        cleaned, reason = _clean(value)
        if cleaned is None:
            rejected.append({"field": name, "reason": reason or "UNUSABLE_URL", "value": ""})
            continue
        if cleaned != value:
            evidence.append(
                OriginEvidence(
                    kind="TRACKING_CLEANED",
                    field=name,
                    value=cleaned,
                    pattern_id=f"urlnorm:tracking-v{ENDPOINT_RULES_VERSION}",
                    strength="CLEANED",
                    observed_at=observed_at,
                )
            )
        match = identify_url(cleaned)
        if not match.matched:
            continue
        entry = OriginEvidence(
            kind="ATS_ENDPOINT",
            field=name,
            value=cleaned,
            pattern_id=match.pattern_id,
            strength=match.strength,
            observed_at=observed_at,
        )
        evidence.append(entry)
        if match.strength == "JOB":
            job_matches[name] = (match, cleaned)
        elif match.strength == "BOARD":
            board_matches[name] = match
        else:
            host_matches[name] = match

    def _finish(
        *,
        status: OriginStatus,
        confidence: float,
        match: AtsMatch | None = None,
        origin_url: str | None = None,
        conflict: Mapping[str, str] | None = None,
    ) -> OriginResolution:
        resolved = status is OriginStatus.RESOLVED and match is not None
        source_host = _host_of(discovery_url or raw_source_url)
        origin_host = _host_of(origin_url) if resolved else None
        return OriginResolution(
            status=status,
            confidence=confidence,
            evidence=tuple(evidence),
            origin_provider=match.provider if resolved else None,
            origin_board=match.board if resolved else None,
            origin_job_id=match.job_id if resolved else None,
            origin_url=origin_url if resolved else None,
            direct_application_url=match.application_url if resolved else None,
            preserved_source_url=discovery_url or raw_source_url,
            same_host_as_source=bool(origin_host and source_host and origin_host == source_host),
            conflict=dict(conflict) if conflict else None,
            rejected_candidates=tuple(rejected),
            resolved_at=observed_at,
            hops_considered=hops_considered,
        )

    # Cross-provider disagreement is never resolved by picking one silently.
    providers = {match.provider for (match, _cleaned) in job_matches.values()}
    if len(providers) > 1:
        evidence.append(
            OriginEvidence(
                kind="PROVIDER_CONFLICT",
                field=",".join(sorted(job_matches)),
                value=";".join(sorted(providers)),
                pattern_id=f"origin:resolver-v{ORIGIN_RESOLVER_VERSION}",
                strength="CONFLICT",
                observed_at=observed_at,
            )
        )
        conflict = {name: match.provider for name, (match, _c) in sorted(job_matches.items())}
        return _finish(status=OriginStatus.UNRESOLVED, confidence=_CONFIDENCE["CONFLICT"],
                       conflict=conflict)

    if job_matches:
        # all job matches share one provider here (conflicts returned above):
        # corroboration across independent fields raises confidence
        names = sorted(job_matches)
        primary = min(job_matches, key=lambda name: order[name])
        match, origin_url = job_matches[primary]
        identities = {(m.provider, m.board, m.job_id) for m, _c in job_matches.values()}
        corroborated = len(identities) == 1 and len(names) > 1
        via_redirect = all(name.startswith("redirect_hop[") or name == "final_url" for name in names)
        if corroborated:
            level = "CORROBORATED"
        elif via_redirect:
            level = "JOB_VIA_REDIRECT"
        else:
            level = "JOB"
        if via_redirect:
            evidence.append(
                OriginEvidence(
                    kind="REDIRECT_UNWRAP",
                    field=primary,
                    value=origin_url,
                    pattern_id=f"origin:bounded-unwrap-v{MAX_UNWRAP_HOPS}",
                    strength="BOUNDED",
                    observed_at=observed_at,
                )
            )
        if fingerprint_family and match.provider and fingerprint_family != match.provider:
            evidence.append(
                OriginEvidence(
                    kind="FINGERPRINT_DISAGREEMENT",
                    field="fingerprint_family",
                    value=str(fingerprint_family),
                    pattern_id="origin:cross-check",
                    strength="REVIEW",
                    observed_at=observed_at,
                )
            )
        confidence = _CONFIDENCE[level]
        status = (
            OriginStatus.RESOLVED
            if confidence >= ORIGIN_CONFIDENCE_THRESHOLD
            else OriginStatus.UNRESOLVED
        )
        return _finish(status=status, confidence=confidence, match=match, origin_url=origin_url)

    if board_matches or host_matches:
        # A board/host signal alone never identifies a job: it is recorded for
        # routing/health but the relation stays unresolved (no guessing).
        return _finish(status=OriginStatus.UNRESOLVED, confidence=_CONFIDENCE["BOARD"])

    evidence.append(
        OriginEvidence(
            kind="NO_ATS_PATTERN",
            field="observation",
            value=",".join(name for name, _ in candidates) or "none",
            pattern_id=f"ats:endpoints-v{ENDPOINT_RULES_VERSION}",
            strength="ABSENT",
            observed_at=observed_at,
        )
    )
    return _finish(status=OriginStatus.UNRESOLVED, confidence=_CONFIDENCE["NONE"])


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return normalize_url(url).host
    except Exception:
        return None


__all__ = [
    "MAX_UNWRAP_HOPS",
    "ORIGIN_CONFIDENCE_THRESHOLD",
    "ORIGIN_RESOLVER_VERSION",
    "OriginEvidence",
    "OriginResolution",
    "OriginStatus",
    "resolve_origin",
]
