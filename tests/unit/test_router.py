"""S2.4 — strategy router tests (02 §12.2, §14).

The router is pure logic and capability-honest: preference order is preserved,
but a runnable candidate exists only for an adapter/strategy implementation
that is actually registered and whose execution class the host can enforce.
"""

from __future__ import annotations

import pytest

from jobscraper.adapters.fingerprint import AtsFingerprint
from jobscraper.adapters.registry import BUILTIN_ADAPTERS
from jobscraper.adapters.router import (
    ROUTER_VERSION,
    RouteCandidate,
    RouteDecision,
    RouteOutcome,
    UnsupportedExecutionClass,
    plan_routes,
)


def _fp(family="GREENHOUSE", confidence=0.95, adapter="greenhouse"):
    return AtsFingerprint(
        family=family,
        confidence=confidence,
        evidence=(),
        recommended_adapter_id=adapter,
    )


class TestRouterContract:
    def test_router_version_is_positive(self):
        assert ROUTER_VERSION >= 1

    def test_route_decision_fields(self):
        decision = plan_routes(
            fingerprint=_fp(), supported_execution_classes=frozenset({"HTTP"})
        )
        assert isinstance(decision, RouteDecision)
        assert decision.fingerprint_family == "GREENHOUSE"
        assert decision.fingerprint_confidence == 0.95
        assert decision.outcome is RouteOutcome.SPECIALIZED
        assert decision.router_version == ROUTER_VERSION

    def test_route_decision_is_frozen(self):
        decision = plan_routes(
            fingerprint=_fp(), supported_execution_classes=frozenset({"HTTP"})
        )
        with pytest.raises(AttributeError):
            decision.outcome = "CHANGED"

    def test_as_dict_has_required_fields(self):
        result = plan_routes(
            fingerprint=_fp(), supported_execution_classes=frozenset({"HTTP"})
        ).as_dict()
        assert {"outcome", "candidates", "router_version", "unsupported_candidates"} <= set(result)


class TestStrategyPreferenceAndAvailability:
    def test_greenhouse_routes_to_the_implemented_provider_native_strategy(self):
        decision = plan_routes(
            fingerprint=_fp(), supported_execution_classes=frozenset({"HTTP"})
        )
        assert decision.outcome is RouteOutcome.SPECIALIZED
        assert [(c.strategy, c.adapter_id) for c in decision.candidates] == [
            ("PROVIDER_NATIVE", "greenhouse")
        ]

    def test_lever_routes_to_the_implemented_provider_native_strategy(self):
        """S2.6 graduated Lever: PROVIDER_NATIVE only, exactly like Greenhouse."""
        decision = plan_routes(
            fingerprint=_fp("LEVER", adapter="lever"),
            supported_execution_classes=frozenset({"HTTP", "BROWSER"}),
        )
        assert decision.outcome is RouteOutcome.SPECIALIZED
        assert [(c.strategy, c.adapter_id) for c in decision.candidates] == [
            ("PROVIDER_NATIVE", "lever")
        ]
        # every other strategy stays an honest non-runnable hypothesis
        assert {c.reason for c in decision.unsupported_candidates} == {
            "STRATEGY_NOT_IMPLEMENTED", "MANUAL_UNSUPPORTED"
        }
        assert "ADAPTER_NOT_REGISTERED" not in {
            c.reason for c in decision.unsupported_candidates
        }

    @pytest.mark.parametrize("family,adapter_id", [("ASHBY", "ashby")])
    def test_future_provider_is_not_advertised_as_runnable(self, family, adapter_id):
        assert adapter_id not in BUILTIN_ADAPTERS
        decision = plan_routes(
            fingerprint=_fp(family, adapter=adapter_id),
            supported_execution_classes=frozenset({"HTTP", "BROWSER"}),
        )
        assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK
        assert decision.candidates == ()
        assert any(item.reason == "ADAPTER_NOT_REGISTERED" for item in decision.unsupported_candidates)

    def test_candidates_remain_in_authoritative_preference_order(self):
        decision = plan_routes(
            fingerprint=_fp(), supported_execution_classes=frozenset({"HTTP"})
        )
        order = [
            "PROVIDER_NATIVE",
            "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            "STRUCTURED_PAGE",
            "HTTP_HTML",
            "PLAYWRIGHT_PUBLIC",
            "PLAYWRIGHT_AUTHENTICATED",
            "MANUAL_UNSUPPORTED",
        ]
        positions = [order.index(candidate.strategy) for candidate in decision.candidates]
        assert positions == sorted(positions)

    def test_unimplemented_greenhouse_strategy_is_recorded_not_runnable(self):
        decision = plan_routes(
            fingerprint=_fp(),
            supported_execution_classes=frozenset({"HTTP", "BROWSER", "BROWSER_INTERACTIVE"}),
        )
        assert [(c.strategy, c.adapter_id) for c in decision.candidates] == [
            ("PROVIDER_NATIVE", "greenhouse")
        ]
        unsupported = {(u.strategy, u.reason) for u in decision.unsupported_candidates}
        assert ("HTTP_HTML", "STRATEGY_NOT_IMPLEMENTED") in unsupported
        assert ("PLAYWRIGHT_PUBLIC", "STRATEGY_NOT_IMPLEMENTED") in unsupported
        assert ("MANUAL_UNSUPPORTED", "MANUAL_UNSUPPORTED") in unsupported


class TestExecutionClassFiltering:
    def test_http_only_host_gets_only_http_runnable_candidates(self):
        decision = plan_routes(
            fingerprint=_fp(), supported_execution_classes=frozenset({"HTTP"})
        )
        assert decision.candidates
        assert all(candidate.execution_class == "HTTP" for candidate in decision.candidates)

    def test_missing_http_execution_class_is_recorded_exactly(self):
        decision = plan_routes(
            fingerprint=_fp(), supported_execution_classes=frozenset()
        )
        assert decision.candidates == ()
        assert any(
            item.strategy == "PROVIDER_NATIVE"
            and item.execution_class == "HTTP"
            and item.reason == "UNSUPPORTED_EXECUTION_CLASS"
            for item in decision.unsupported_candidates
        )

    def test_browser_support_does_not_invent_browser_implementation(self):
        decision = plan_routes(
            fingerprint=_fp(),
            supported_execution_classes=frozenset({"HTTP", "BROWSER", "BROWSER_INTERACTIVE"}),
        )
        assert all(candidate.strategy != "PLAYWRIGHT_PUBLIC" for candidate in decision.candidates)
        assert any(
            item.strategy == "PLAYWRIGHT_PUBLIC" and item.reason == "STRATEGY_NOT_IMPLEMENTED"
            for item in decision.unsupported_candidates
        )


class TestLowConfidenceFallback:
    def test_low_confidence_yields_generic_discovery_status(self):
        decision = plan_routes(
            fingerprint=_fp(None, 0.1, None),
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK
        assert "confidence" in (decision.fallback_reason or "").lower()

    def test_borderline_below_threshold_yields_generic_fallback(self):
        decision = plan_routes(
            fingerprint=_fp("GREENHOUSE", 0.5, "greenhouse"),
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK

    def test_no_family_yields_generic_fallback_even_with_high_confidence(self):
        decision = plan_routes(
            fingerprint=_fp(None, 0.9, None),
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK

    def test_generic_fallback_does_not_invent_an_adapter(self):
        decision = plan_routes(
            fingerprint=_fp(None, 0.1, None),
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert decision.candidates == ()
        assert any(
            item.strategy == "HTTP_HTML"
            and item.reason == "GENERIC_DISCOVERY_NOT_IMPLEMENTED"
            for item in decision.unsupported_candidates
        )


class TestUnsupportedExecutionClass:
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
        )
        with pytest.raises(AttributeError):
            unsupported.strategy = "HTTP"

    def test_route_candidate_is_frozen(self):
        candidate = RouteCandidate("PROVIDER_NATIVE", "HTTP", "greenhouse")
        with pytest.raises(AttributeError):
            candidate.strategy = "HTTP_HTML"
