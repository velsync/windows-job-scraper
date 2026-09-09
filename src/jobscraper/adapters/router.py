"""Capability-honest strategy router (02 §12.2, §14).

Pure logic — no I/O or database access.  The authoritative strategy order is
preserved, but a ``RouteCandidate`` is emitted only when the corresponding
built-in adapter and that exact strategy are implemented *and* the host can
enforce its execution class.  Future or manual routes remain durable
unsupported evidence; they are never advertised as runnable work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from jobscraper.adapters.fingerprint import AtsFingerprint

# S2.4 corrective changes routing semantics; evidence must record that version.
ROUTER_VERSION = 2
MIN_CONFIDENCE_FOR_SPECIALIZED = 0.70

STRATEGY_ORDER: tuple[str, ...] = (
    "PROVIDER_NATIVE",
    "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
    "STRUCTURED_PAGE",
    "HTTP_HTML",
    "PLAYWRIGHT_PUBLIC",
    "PLAYWRIGHT_AUTHENTICATED",
    "MANUAL_UNSUPPORTED",
)

_STRATEGY_EXECUTION_CLASS: dict[str, str] = {
    "PROVIDER_NATIVE": "HTTP",
    "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT": "HTTP",
    "STRUCTURED_PAGE": "HTTP",
    "HTTP_HTML": "HTTP",
    "PLAYWRIGHT_PUBLIC": "BROWSER",
    "PLAYWRIGHT_AUTHENTICATED": "BROWSER_INTERACTIVE",
    # A sentinel only.  It is deliberately not mapped to an executable class.
    "MANUAL_UNSUPPORTED": "MANUAL",
}

# Ordered hypotheses.  Being present here means "consider/record this route",
# not "the route is executable".  Executability is checked below against the
# built-in registry and the strategy contract actually implemented by the
# adapter.
_FAMILY_CANDIDATES: dict[str, tuple[tuple[str, str], ...]] = {
    "GREENHOUSE": tuple((strategy, "greenhouse") for strategy in STRATEGY_ORDER),
    "LEVER": tuple((strategy, "lever") for strategy in STRATEGY_ORDER),
    "ASHBY": tuple((strategy, "ashby") for strategy in STRATEGY_ORDER),
}

# Explicit strategy contracts implemented by today's built-ins.  One code
# adapter does not gain new behavior merely because a different strategy label
# is attached to it.
_IMPLEMENTED_STRATEGIES: dict[str, frozenset[str]] = {
    "greenhouse": frozenset({"PROVIDER_NATIVE"}),
    "lever": frozenset({"PROVIDER_NATIVE"}),
    "ashby": frozenset({"PROVIDER_NATIVE"}),
    "json_api_feed": frozenset({"FEED_OR_PUBLIC_STRUCTURED_ENDPOINT"}),
}


class RouteOutcome(str, Enum):
    SPECIALIZED = "SPECIALIZED"
    GENERIC_DISCOVERY_FALLBACK = "GENERIC_DISCOVERY_FALLBACK"


@dataclass(frozen=True)
class RouteCandidate:
    strategy: str
    execution_class: str
    adapter_id: str
    adapter_version: str = "1.0.0"
    priority: int = 0


@dataclass(frozen=True)
class UnsupportedExecutionClass:
    """One non-runnable route hypothesis retained as audit evidence.

    The historical class name is preserved for the public contract, while
    ``reason`` distinguishes execution-class, registration, strategy and manual
    limitations.
    """

    strategy: str
    execution_class: str
    reason: str = "UNSUPPORTED_EXECUTION_CLASS"


@dataclass(frozen=True)
class RouteDecision:
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
                    "strategy": candidate.strategy,
                    "execution_class": candidate.execution_class,
                    "adapter_id": candidate.adapter_id,
                    "adapter_version": candidate.adapter_version,
                    "priority": candidate.priority,
                }
                for candidate in self.candidates
            ],
            "unsupported_candidates": [
                {
                    "strategy": item.strategy,
                    "execution_class": item.execution_class,
                    "reason": item.reason,
                }
                for item in self.unsupported_candidates
            ],
            "router_version": self.router_version,
            "fallback_reason": self.fallback_reason,
        }


def _generic_fallback(
    fingerprint: AtsFingerprint,
    *,
    reason: str,
    additional_unsupported: tuple[UnsupportedExecutionClass, ...] = (),
) -> RouteDecision:
    """Return an honest generic-discovery status for Slice 2.

    v0.3.1.3 requires a durable generic discovery binding before such work can
    execute.  S2.5 has not graduated that adapter yet, so an unregistered
    ``generic`` candidate would be fictitious capability.  We record the
    missing route as unsupported and emit no runnable candidate.
    """

    generic = UnsupportedExecutionClass(
        strategy="HTTP_HTML",
        execution_class="HTTP",
        reason="GENERIC_DISCOVERY_NOT_IMPLEMENTED",
    )
    return RouteDecision(
        outcome=RouteOutcome.GENERIC_DISCOVERY_FALLBACK,
        fingerprint_family=fingerprint.family,
        fingerprint_confidence=fingerprint.confidence,
        candidates=(),
        unsupported_candidates=(*additional_unsupported, generic),
        fallback_reason=reason,
    )


def plan_routes(
    *,
    fingerprint: AtsFingerprint,
    supported_execution_classes: frozenset[str],
) -> RouteDecision:
    from jobscraper.adapters.registry import BUILTIN_ADAPTERS

    if fingerprint.family is None or fingerprint.confidence < MIN_CONFIDENCE_FOR_SPECIALIZED:
        reason_parts: list[str] = []
        if fingerprint.family is None:
            reason_parts.append("no ATS family identified")
        if fingerprint.confidence < MIN_CONFIDENCE_FOR_SPECIALIZED:
            reason_parts.append(
                f"confidence {fingerprint.confidence:.2f} below threshold "
                f"{MIN_CONFIDENCE_FOR_SPECIALIZED:.2f}"
            )
        return _generic_fallback(fingerprint, reason="; ".join(reason_parts))

    family = fingerprint.family
    candidate_specs = _FAMILY_CANDIDATES.get(family, ())
    if not candidate_specs:
        return _generic_fallback(
            fingerprint,
            reason=f"no candidate specification for family {family!r}",
        )

    candidates: list[RouteCandidate] = []
    unsupported: list[UnsupportedExecutionClass] = []
    for strategy, adapter_id in candidate_specs:
        execution_class = _STRATEGY_EXECUTION_CLASS[strategy]
        if strategy == "MANUAL_UNSUPPORTED":
            unsupported.append(
                UnsupportedExecutionClass(
                    strategy=strategy,
                    execution_class="MANUAL",
                    reason="MANUAL_UNSUPPORTED",
                )
            )
            continue
        if adapter_id not in BUILTIN_ADAPTERS:
            unsupported.append(
                UnsupportedExecutionClass(
                    strategy=strategy,
                    execution_class=execution_class,
                    reason="ADAPTER_NOT_REGISTERED",
                )
            )
            continue
        if strategy not in _IMPLEMENTED_STRATEGIES.get(adapter_id, frozenset()):
            unsupported.append(
                UnsupportedExecutionClass(
                    strategy=strategy,
                    execution_class=execution_class,
                    reason="STRATEGY_NOT_IMPLEMENTED",
                )
            )
            continue
        if execution_class not in supported_execution_classes:
            unsupported.append(
                UnsupportedExecutionClass(
                    strategy=strategy,
                    execution_class=execution_class,
                    reason="UNSUPPORTED_EXECUTION_CLASS",
                )
            )
            continue
        candidates.append(
            RouteCandidate(
                strategy=strategy,
                execution_class=execution_class,
                adapter_id=adapter_id,
            )
        )

    if not candidates and all(
        item.reason == "ADAPTER_NOT_REGISTERED" or item.reason == "MANUAL_UNSUPPORTED"
        for item in unsupported
    ):
        return _generic_fallback(
            fingerprint,
            reason=f"no registered specialized route for family {family!r}",
            additional_unsupported=tuple(unsupported),
        )

    # A known, implemented family may still have no runnable candidate because
    # the host lacks the required execution class.  Keeping SPECIALIZED here is
    # truthful: the route is known and its exact enforcement blocker is recorded.
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
