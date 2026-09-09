"""Corrective regression tests for audited S2.4 fingerprint/routing defects.

These tests intentionally describe the normative behavior, not the behavior
of the original S2.4 implementation.  They are kept separate so the red/green
corrective history remains obvious during review.
"""

from __future__ import annotations

from jobscraper.adapters.fingerprint import AtsFingerprint, classify_content
from jobscraper.adapters.registry import BUILTIN_ADAPTERS
from jobscraper.adapters.router import RouteOutcome, plan_routes


def test_conflicting_provider_signals_do_not_cross_subsidize_confidence():
    """Evidence for one ATS family must not raise another family's confidence.

    The URL contributes only a weak Greenhouse suffix signal while the body
    contributes a weak Lever marker.  Neither provider independently reaches
    the specialized-route threshold, so the final fingerprint must remain
    below it even though the two unrelated signal kinds would exceed it if
    pooled globally.
    """
    fp = classify_content(
        url="https://careers.greenhouse.io/acme",
        body=b'<html><body><div class="postings-group"></div></body></html>',
        content_type="text/html",
    )

    assert fp.confidence < 0.70


def test_unimplemented_provider_never_becomes_a_runnable_candidate():
    """S2.5 has no Lever adapter yet, so routing must be capability-honest."""
    fp = AtsFingerprint(
        family="LEVER",
        confidence=0.95,
        evidence=(),
        recommended_adapter_id="lever",
    )

    decision = plan_routes(
        fingerprint=fp,
        supported_execution_classes=frozenset({"HTTP"}),
    )

    assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK
    assert all(candidate.adapter_id in BUILTIN_ADAPTERS for candidate in decision.candidates)


def test_low_confidence_fallback_never_advertises_an_unregistered_adapter():
    """A generic-discovery status is not permission to invent adapter code."""
    fp = AtsFingerprint(
        family=None,
        confidence=0.10,
        evidence=(),
        recommended_adapter_id=None,
    )

    decision = plan_routes(
        fingerprint=fp,
        supported_execution_classes=frozenset({"HTTP"}),
    )

    assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK
    assert all(candidate.adapter_id in BUILTIN_ADAPTERS for candidate in decision.candidates)


def test_greenhouse_only_advertises_the_strategy_its_adapter_implements():
    """Changing a strategy label must not pretend one adapter has new I/O logic."""
    fp = AtsFingerprint(
        family="GREENHOUSE",
        confidence=0.95,
        evidence=(),
        recommended_adapter_id="greenhouse",
    )

    decision = plan_routes(
        fingerprint=fp,
        supported_execution_classes=frozenset({"HTTP", "BROWSER", "BROWSER_INTERACTIVE"}),
    )

    assert [(c.strategy, c.adapter_id) for c in decision.candidates] == [
        ("PROVIDER_NATIVE", "greenhouse")
    ]
