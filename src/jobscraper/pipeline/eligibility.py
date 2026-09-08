"""Minimal eligibility engine (01 §36).

Eligibility is downstream of collection and normalization; adapters never
decide suitability. Verdicts: ELIGIBLE / LIKELY / UNCLEAR / UNLIKELY /
INELIGIBLE.

Hard invariant: absence of restriction text does NOT mean worldwide
eligibility — unknown stays UNCLEAR.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EVALUATOR_VERSION = "eligibility-s1-1"

_COUNTRY_TOKENS = {
    "RO": ("romania", "romanian", "bucharest", "cluj", "timisoara", "oradea", "iasi"),
    "DE": ("germany", "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne"),
    "NL": ("netherlands", "amsterdam", "rotterdam", "utrecht", "hague"),
    "UK": ("united kingdom", "london", "manchester", "england", "scotland"),
    "US": ("united states", "usa", "new york", "san francisco", "seattle", "texas"),
    "JP": ("japan", "tokyo", "osaka"),
}
_REMOTE_TOKENS = ("remote", "distributed", "work from home", "anywhere")


@dataclass(frozen=True)
class EligibilityVerdict:
    verdict: str
    confidence: float
    reason_codes: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def _infer_countries(locations) -> set[str]:
    found: set[str] = set()
    for raw in locations or ():
        text = str(raw).lower()
        for country, tokens in _COUNTRY_TOKENS.items():
            if any(token in text for token in tokens):
                found.add(country)
    return found


def _mentions_remote(locations) -> bool:
    return any(
        any(token in str(raw).lower() for token in _REMOTE_TOKENS)
        for raw in locations or ()
    )


def evaluate_eligibility(
    *,
    locations,
    remote_worldwide: bool,
    profile: dict,
) -> EligibilityVerdict:
    """Deterministic, evidence-derived verdict (never fabricated)."""
    eligible_countries = set(profile.get("eligible_countries") or ())
    remote_rules = profile.get("remote_rules") or {}

    if remote_worldwide and remote_rules.get("remote_ok", True):
        return EligibilityVerdict(
            "ELIGIBLE", 0.9, ["remote_worldwide"], {"remote_worldwide": True}
        )

    inferred = _infer_countries(locations)
    matched = inferred & eligible_countries
    if matched:
        return EligibilityVerdict(
            "ELIGIBLE", 0.8, ["country_match"], {"matched_countries": sorted(matched)}
        )
    if _mentions_remote(locations) and remote_rules.get("remote_ok"):
        # Remote scope is not proven worldwide; treat as likely, not certain.
        return EligibilityVerdict(
            "LIKELY", 0.5, ["remote_allowed"], {"remote_scope": "unknown"}
        )
    if inferred and eligible_countries and not matched:
        return EligibilityVerdict(
            "UNLIKELY",
            0.6,
            ["country_not_eligible"],
            {"inferred_countries": sorted(inferred)},
        )
    # Hard invariant: unknown stays UNCLEAR (never worldwide eligibility).
    return EligibilityVerdict("UNCLEAR", 0.3, ["no_location_evidence"], {})


__all__ = ["EVALUATOR_VERSION", "EligibilityVerdict", "evaluate_eligibility"]
