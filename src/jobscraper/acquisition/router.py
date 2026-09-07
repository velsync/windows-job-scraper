"""Strategy routing.

Authority: module 02 section 12.2 (strategy order), ACQ-07 (escalation as a
new typed task, not identity evasion).

The cheapest legitimate reliable strategy wins; HTTP->browser escalation is
a new typed logical request under a different strategy.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobscraper.acquisition.contracts import ExecutionClass, Strategy, STRATEGY_ORDER


@dataclass(frozen=True)
class RoutedBinding:
    adapter_id: str
    strategy: Strategy
    execution_class: ExecutionClass
    fallback_rank: int


def strategy_rank(strategy: Strategy) -> int:
    return STRATEGY_ORDER.index(strategy)


def route_for_fingerprint(
    *,
    family: str | None,
    confident: bool,
    adapter_for_family: str | None = None,
    needs_browser: bool = False,
) -> RoutedBinding:
    """Pick the preferred strategy for a fingerprinted source."""
    if confident and adapter_for_family:
        # Structured provider APIs are the cheapest reliable route.
        return RoutedBinding(
            adapter_id=adapter_for_family,
            strategy=Strategy.FEED_OR_PUBLIC_STRUCTURED_ENDPOINT,
            execution_class=ExecutionClass.HTTP,
            fallback_rank=1,
        )
    if needs_browser:
        return RoutedBinding(
            adapter_id="generic",
            strategy=Strategy.PLAYWRIGHT_PUBLIC,
            execution_class=ExecutionClass.BROWSER,
            fallback_rank=2,
        )
    return RoutedBinding(
        adapter_id="generic",
        strategy=Strategy.HTTP_HTML,
        execution_class=ExecutionClass.HTTP,
        fallback_rank=1,
    )


def fallback_candidates(routed: RoutedBinding) -> list[RoutedBinding]:
    """Ordered fallback ladder after the primary route."""
    ladder: list[RoutedBinding] = []
    if routed.execution_class == ExecutionClass.HTTP:
        ladder.append(
            RoutedBinding(
                adapter_id=routed.adapter_id,
                strategy=Strategy.PLAYWRIGHT_PUBLIC,
                execution_class=ExecutionClass.BROWSER,
                fallback_rank=routed.fallback_rank + 1,
            )
        )
    return ladder


def manual_unsupported() -> RoutedBinding:
    return RoutedBinding(
        adapter_id="generic",
        strategy=Strategy.MANUAL_UNSUPPORTED,
        execution_class=ExecutionClass.HOST_NATIVE,
        fallback_rank=99,
    )
