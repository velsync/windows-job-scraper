"""Strategy router (02 §12.2, §14).

Pure logic — no I/O, no database access.  Takes a fingerprint and the set
of execution classes the host can enforce, and produces a ``RouteDecision``
containing ordered candidates in the authoritative preference order:

    PROVIDER_NATIVE → FEED_OR_PUBLIC_STRUCTURED_ENDPOINT → STRUCTURED_PAGE →
    HTTP_HTML → PLAYWRIGHT_PUBLIC → PLAYWRIGHT_AUTHENTICATED → MANUAL_UNSUPPORTED

Rules (normative):

* Candidates are filtered by execution classes the host actually supports.
  Slice 2 may execute **HTTP class only**.
* Browser-class candidates that the host cannot enforce are recorded in
  ``unsupported_candidates`` as ``UNSUPPORTED_EXECUTION_CLASS`` — never
  silently substituted.
* Low-confidence fingerprint (below :data:`MIN_CONFIDENCE_FOR_SPECIALIZED`)
  produces no specialized route; the decision records
  ``GENERIC_DISCOVERY_FALLBACK`` with one ``HTTP_HTML`` generic candidate.
* A fingerprint with ``family=None`` always yields
  ``GENERIC_DISCOVERY_FALLBACK`` regardless of confidence.

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md §12.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from jobscraper.adapters.fingerprint import AtsFingerprint

#: Router contract version.
ROUTER_VERSION = 1

#: Below this confidence, no specialized adapter route is attempted.
MIN_CONFIDENCE_FOR_SPECIALIZED = 0.70

#: Authoritative preference order (02 §12.2, ARC-08).
STRATEGY_ORDER: tuple[str, ...] = (
    "PROVIDER_NATIVE",
    "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
    "STRUCTURED_PAGE",
    "HTTP_HTML",
    "PLAYWRIGHT_PUBLIC",
    "PLAYWRIGHT_AUTHENTICATED",
    "MANUAL_UNSUPPORTED",
)

#: Strategy → execution class mapping.  Each strategy declares the class it
#: needs; the router filters against what the host can enforce.
_STRATEGY_EXECUTION_CLASS: dict[str, str] = {
    "PROVIDER_NATIVE": "HTTP",
    "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT": "HTTP",
    "STRUCTURED_PAGE": "HTTP",
    "HTTP_HTML": "HTTP",
    "PLAYWRIGHT_PUBLIC": "BROWSER",
    "PLAYWRIGHT_AUTHENTICATED": "BROWSER_INTERACTIVE",
    "MANUAL_UNSUPPORTED": "HTTP",  # sentinel; never actually executed
}

#: Family → ordered strategy candidates.  Each tuple entry is
#: ``(strategy, adapter_id_hint)``.
_FAMILY_CANDIDATES: dict[str, tuple[tuple[str, str], ...]] = {
    "GREENHOUSE": (
        ("PROVIDER_NATIVE", "greenhouse"),
        ("FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", "greenhouse"),
        ("STRUCTURED_PAGE", "greenhouse"),
        ("HTTP_HTML", "greenhouse"),
        ("PLAYWRIGHT_PUBLIC", "greenhouse"),
        ("PLAYWRIGHT_AUTHENTICATED", "greenhouse"),
        ("MANUAL_UNSUPPORTED", "greenhouse"),
    ),
    "LEVER": (
        ("PROVIDER_NATIVE", "lever"),
        ("FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", "lever"),
        ("STRUCTURED_PAGE", "lever"),
        ("HTTP_HTML", "lever"),
        ("PLAYWRIGHT_PUBLIC", "lever"),
        ("PLAYWRIGHT_AUTHENTICATED", "lever"),
        ("MANUAL_UNSUPPORTED", "lever"),
    ),
    "ASHBY": (
        ("PROVIDER_NATIVE", "ashby"),
        ("FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", "ashby"),
        ("STRUCTURED_PAGE", "ashby"),
        ("HTTP_HTML", "ashby"),
        ("PLAYWRIGHT_PUBLIC", "ashby"),
        ("PLAYWRIGHT_AUTHENTICATED", "ashby"),
        ("MANUAL_UNSUPPORTED", "ashby"),
    ),
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class RouteOutcome(str, Enum):
    """Outcome of the routing decision."""
    SPECIALIZED = "SPECIALIZED"
    GENERIC_DISCOVERY_FALLBACK = "GENERIC_DISCOVERY_FALLBACK"


@dataclass(frozen=True)
class RouteCandidate:
    """One candidate strategy the host may attempt."""
    strategy: str
    execution_class: str
    adapter_id: str
    adapter_version: str = "1.0.0"
    priority: int = 0


@dataclass(frozen=True)
class UnsupportedExecutionClass:
    """A candidate that was filtered out because the host cannot enforce
    its execution class.  Recorded — never silently dropped.
    """
    strategy: str
    execution_class: str
    reason: str = "UNSUPPORTED_EXECUTION_CLASS"


@dataclass(frozen=True)
class RouteDecision:
    """The complete routing decision for one fingerprinted source."""
    outcome: RouteOutcome
    fingerprint_family: str | None
    fingerprint_confidence: float
    candidates: tuple[RouteCandidate, ...]
    unsupported_candidates: tuple[UnsupportedExecutionClass, ...]
    router_version: int = ROUTER_VERSION
    fallback_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "fingerprint_family": self.fingerprint_family,
            "fingerprint_confidence": self.fingerprint_confidence,
            "candidates": [
                {
                    "strategy": c.strategy,
                    "execution_class": c.execution_class,
                    "adapter_id": c.adapter_id,
                    "adapter_version": c.adapter_version,
                    "priority": c.priority,
                }
                for c in self.candidates
            ],
            "unsupported_candidates": [
                {
                    "strategy": u.strategy,
                    "execution_class": u.execution_class,
                    "reason": u.reason,
                }
                for u in self.unsupported_candidates
            ],
            "router_version": self.router_version,
            "fallback_reason": self.fallback_reason,
        }


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------

def plan_routes(
    *,
    fingerprint: AtsFingerprint,
    supported_execution_classes: frozenset[str],
) -> RouteDecision:
    """Produce a route decision from a fingerprint and host capabilities.

    Parameters
    ----------
    fingerprint:
        The versioned fingerprint output from
        :func:`~jobscraper.adapters.fingerprint.classify_content`.
    supported_execution_classes:
        The execution classes the host can enforce right now.
        Slice 2 passes ``frozenset({"HTTP"})``.

    Returns
    -------
    RouteDecision
        Ordered candidates the host may attempt, plus any filtered-out
        browser-class candidates recorded as unsupported.
    """
    # Low confidence or no family → generic discovery fallback
    if (
        fingerprint.family is None
        or fingerprint.confidence < MIN_CONFIDENCE_FOR_SPECIALIZED
    ):
        reason_parts: list[str] = []
        if fingerprint.family is None:
            reason_parts.append("no ATS family identified")
        if fingerprint.confidence < MIN_CONFIDENCE_FOR_SPECIALIZED:
            reason_parts.append(
                f"confidence {fingerprint.confidence:.2f} below threshold "
                f"{MIN_CONFIDENCE_FOR_SPECIALIZED:.2f}"
            )
        reason = "; ".join(reason_parts)

        # The generic fallback always offers HTTP_HTML.
        candidates: list[RouteCandidate] = []
        unsupported: list[UnsupportedExecutionClass] = []
        exec_class = _STRATEGY_EXECUTION_CLASS["HTTP_HTML"]
        if exec_class in supported_execution_classes:
            candidates.append(RouteCandidate(
                strategy="HTTP_HTML",
                execution_class=exec_class,
                adapter_id="generic",
            ))
        else:
            unsupported.append(UnsupportedExecutionClass(
                strategy="HTTP_HTML",
                execution_class=exec_class,
            ))

        return RouteDecision(
            outcome=RouteOutcome.GENERIC_DISCOVERY_FALLBACK,
            fingerprint_family=fingerprint.family,
            fingerprint_confidence=fingerprint.confidence,
            candidates=tuple(candidates),
            unsupported_candidates=tuple(unsupported),
            fallback_reason=reason,
        )

    # Specialized routing
    family = fingerprint.family
    candidate_specs = _FAMILY_CANDIDATES.get(family, ())

    if not candidate_specs:
        # Unknown family with high confidence — treat as generic
        return RouteDecision(
            outcome=RouteOutcome.GENERIC_DISCOVERY_FALLBACK,
            fingerprint_family=family,
            fingerprint_confidence=fingerprint.confidence,
            candidates=(
                RouteCandidate(
                    strategy="HTTP_HTML",
                    execution_class="HTTP",
                    adapter_id="generic",
                ),
            ) if "HTTP" in supported_execution_classes else (),
            unsupported_candidates=(
                UnsupportedExecutionClass(
                    strategy="HTTP_HTML",
                    execution_class="HTTP",
                ),
            ) if "HTTP" not in supported_execution_classes else (),
            fallback_reason=f"no candidate spec for family {family!r}",
        )

    candidates = []
    unsupported = []
    for strategy, adapter_id in candidate_specs:
        exec_class = _STRATEGY_EXECUTION_CLASS.get(strategy, "HTTP")
        if exec_class in supported_execution_classes:
            candidates.append(RouteCandidate(
                strategy=strategy,
                execution_class=exec_class,
                adapter_id=adapter_id,
            ))
        else:
            unsupported.append(UnsupportedExecutionClass(
                strategy=strategy,
                execution_class=exec_class,
            ))

    return RouteDecision(
        outcome=RouteOutcome.SPECIALIZED,
        fingerprint_family=family,
        fingerprint_confidence=fingerprint.confidence,
        candidates=tuple(candidates),
        unsupported_candidates=tuple(unsupported),
    )


__all__ = [
    "MIN_CONFIDENCE_FOR_SPECIALIZED",
    "ROUTER_VERSION",
    "RouteCandidate",
    "RouteDecision",
    "RouteOutcome",
    "STRATEGY_ORDER",
    "UnsupportedExecutionClass",
    "plan_routes",
]
