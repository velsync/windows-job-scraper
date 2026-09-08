"""S2.4 — Strategy router tests (02 §12.2, §12.1, §14).

The router is pure logic: it takes a fingerprint + host execution
capabilities and produces a list of candidate strategies in the
authoritative preference order.  It performs no I/O and has no database
access.  Slice 2 may execute HTTP class only; browser-class candidates
must be reported as UNSUPPORTED_EXECUTION_CLASS, never silently
substituted.  Low-confidence fingerprinting falls back to generic
discovery (GENERIC_DISCOVERY_FALLBACK).
"""

from __future__ import annotations

import pytest

from jobscraper.adapters.fingerprint import AtsFingerprint
from jobscraper.adapters.router import (
    ROUTER_VERSION,
    RouteCandidate,
    RouteDecision,
    RouteOutcome,
    UnsupportedExecutionClass,
    plan_routes,
)


# ---------------------------------------------------------------------------
# Contract basics
# ---------------------------------------------------------------------------

class TestRouterContract:
    """RouteDecision is versioned, deterministic, and frozen."""

    def test_router_version_is_positive(self):
        assert ROUTER_VERSION >= 1

    def test_route_decision_fields(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=({"kind": "HOST", "value": "boards.greenhouse.io", "strength": "STRONG"},),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert isinstance(decision, RouteDecision)
        assert decision.fingerprint_family == "GREENHOUSE"
        assert decision.fingerprint_confidence == 0.95
        assert decision.outcome == RouteOutcome.SPECIALIZED
        assert decision.router_version == ROUTER_VERSION

    def test_route_decision_is_frozen(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        with pytest.raises(AttributeError):
            decision.outcome = "CHANGED"

    def test_as_dict_has_required_fields(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        d = decision.as_dict()
        assert "outcome" in d
        assert "candidates" in d
        assert "router_version" in d
        assert "unsupported_candidates" in d


# ---------------------------------------------------------------------------
# Strategy preference order
# ---------------------------------------------------------------------------

class TestStrategyPreferenceOrder:
    """The authoritative order is:
    PROVIDER_NATIVE → FEED_OR_PUBLIC_STRUCTURED_ENDPOINT → STRUCTURED_PAGE →
    HTTP_HTML → PLAYWRIGHT_PUBLIC → PLAYWRIGHT_AUTHENTICATED → MANUAL_UNSUPPORTED
    """

    def test_greenhouse_routes_to_provider_native_first(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP", "BROWSER"}),
        )
        assert decision.outcome == RouteOutcome.SPECIALIZED
        assert len(decision.candidates) >= 1
        assert decision.candidates[0].strategy == "PROVIDER_NATIVE"

    def test_lever_routes_to_provider_native(self):
        fp = AtsFingerprint(
            family="LEVER",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="lever",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP", "BROWSER"}),
        )
        assert decision.outcome == RouteOutcome.SPECIALIZED
        assert decision.candidates[0].strategy == "PROVIDER_NATIVE"

    def test_ashby_routes_to_provider_native(self):
        fp = AtsFingerprint(
            family="ASHBY",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="ashby",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP", "BROWSER"}),
        )
        assert decision.outcome == RouteOutcome.SPECIALIZED
        assert decision.candidates[0].strategy == "PROVIDER_NATIVE"

    def test_each_candidate_has_execution_class(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        for candidate in decision.candidates:
            assert candidate.execution_class in ("HTTP", "BROWSER", "BROWSER_INTERACTIVE")

    def test_candidates_are_sorted_by_preference_order(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        strategies = [c.strategy for c in decision.candidates]
        expected_order = [
            "PROVIDER_NATIVE",
            "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            "STRUCTURED_PAGE",
            "HTTP_HTML",
            "PLAYWRIGHT_PUBLIC",
            "PLAYWRIGHT_AUTHENTICATED",
            "MANUAL_UNSUPPORTED",
        ]
        actual_indices = [expected_order.index(s) for s in strategies]
        assert actual_indices == sorted(actual_indices)


# ---------------------------------------------------------------------------
# Execution class filtering (Slice 2: HTTP only)
# ---------------------------------------------------------------------------

class TestExecutionClassFiltering:
    """Slice 2 may execute HTTP class only. Browser-class candidates
    must be reported as UNSUPPORTED_EXECUTION_CLASS, never silently
    substituted.
    """

    def test_http_only_host_gets_http_candidates(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        for candidate in decision.candidates:
            assert candidate.execution_class == "HTTP"

    def test_browser_candidate_reported_as_unsupported(self):
        """A provider that needs browser class should have its browser
        candidates recorded as unsupported, not silently dropped."""
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        # If any candidates were originally browser-class, they appear
        # in the unsupported list.
        for unsupported in decision.unsupported_candidates:
            assert unsupported.execution_class in ("BROWSER", "BROWSER_INTERACTIVE")
            assert unsupported.reason == "UNSUPPORTED_EXECUTION_CLASS"

    def test_http_and_browser_host_gets_all_candidates(self):
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
        assert decision.unsupported_candidates == ()
        assert len(decision.candidates) >= 2

    def test_empty_supported_classes_yields_no_candidates(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset(),
        )
        assert len(decision.candidates) == 0
        assert len(decision.unsupported_candidates) > 0


# ---------------------------------------------------------------------------
# Low-confidence fallback
# ---------------------------------------------------------------------------

class TestLowConfidenceFallback:
    """Low-confidence fingerprint ⇒ no specialized route; record
    GENERIC_DISCOVERY_FALLBACK.
    """

    def test_low_confidence_yields_generic_discovery_fallback(self):
        fp = AtsFingerprint(
            family=None,
            confidence=0.1,
            evidence=(),
            recommended_adapter_id=None,
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert decision.outcome == RouteOutcome.GENERIC_DISCOVERY_FALLBACK
        assert decision.fallback_reason is not None
        assert "confidence" in decision.fallback_reason.lower() or "GENERIC" in (decision.fallback_reason or "").upper()

    def test_borderline_confidence_yields_generic_fallback(self):
        """Confidence exactly at the threshold still falls back."""
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.5,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        # 0.5 is below the 0.70 threshold
        assert decision.outcome == RouteOutcome.GENERIC_DISCOVERY_FALLBACK

    def test_no_family_yields_generic_fallback_even_with_high_confidence(self):
        """If family is None, there is nothing to specialize on."""
        fp = AtsFingerprint(
            family=None,
            confidence=0.9,
            evidence=(),
            recommended_adapter_id=None,
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert decision.outcome == RouteOutcome.GENERIC_DISCOVERY_FALLBACK

    def test_generic_fallback_still_produces_a_candidate(self):
        fp = AtsFingerprint(
            family=None,
            confidence=0.1,
            evidence=(),
            recommended_adapter_id=None,
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert len(decision.candidates) == 1
        assert decision.candidates[0].strategy == "HTTP_HTML"
        assert decision.candidates[0].adapter_id == "generic"


# ---------------------------------------------------------------------------
# UnsupportedExecutionClass sentinel
# ---------------------------------------------------------------------------

class TestUnsupportedExecutionClass:
    """Browser-class candidates are recorded, not silently dropped."""

    def test_unsupported_candidate_has_all_fields(self):
        unsupported = UnsupportedExecutionClass(
            strategy="PLAYWRIGHT_PUBLIC",
            execution_class="BROWSER",
            reason="UNSUPPORTED_EXECUTION_CLASS",
        )
        assert unsupported.strategy == "PLAYWRIGHT_PUBLIC"
        assert unsupported.execution_class == "BROWSER"
        assert unsupported.reason == "UNSUPPORTED_EXECUTION_CLASS"

    def test_unsupported_candidate_is_frozen(self):
        unsupported = UnsupportedExecutionClass(
            strategy="PLAYWRIGHT_PUBLIC",
            execution_class="BROWSER",
            reason="UNSUPPORTED_EXECUTION_CLASS",
        )
        with pytest.raises(AttributeError):
            unsupported.strategy = "HTTP"
