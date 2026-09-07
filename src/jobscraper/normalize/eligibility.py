"""Profile-relative eligibility evaluation.

Authority: module 01 section 36. Eligibility is a profile-relative
evidence-derived verdict, not a workflow status.

Hard invariant: absence of restriction text does NOT mean worldwide
eligibility. Unknown stays UNCLEAR.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EVALUATOR_VERSION = "eligibility-v1"

VERDICTS = ("ELIGIBLE", "LIKELY", "UNCLEAR", "UNLIKELY", "INELIGIBLE")


@dataclass
class EligibilityResult:
    verdict: str
    confidence: float
    reason_codes: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def as_row(self, *, job_id: str, profile_id: str, profile_revision_id: str | None,
               job_content_revision: int, normalization_version: str, evaluated_at: str) -> dict:
        return {
            "job_id": job_id,
            "profile_id": profile_id,
            "profile_revision_id": profile_revision_id,
            "rules_revision_id": "rules-v1",
            "job_content_revision": job_content_revision,
            "normalization_version": normalization_version,
            "evaluator_version": EVALUATOR_VERSION,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reason_codes_json": str(__import__("json").dumps(self.reason_codes)),
            "evidence_json": str(__import__("json").dumps(self.evidence, sort_keys=True)),
            "rule_version": "eligibility-rules-v1",
            "evaluated_at": evaluated_at,
        }


def _country_allowed(country: str | None, profile: dict) -> bool | None:
    eligible = [c.upper() for c in (profile.get("eligible_countries") or [])]
    home = (profile.get("home_country") or "").upper()
    if country is None:
        return None
    country = country.upper()
    if home and country == home:
        return True
    if eligible and country in eligible:
        return True
    if eligible and country not in eligible:
        return False
    if home and country != home and not eligible:
        return None  # unknown whether this country works for the user
    return None


def evaluate_eligibility(
    *,
    title: str,
    locations: list[dict],
    facts: list[dict] | None = None,
    profile: dict,
    remote_worldwide: bool = False,
) -> EligibilityResult:
    """Evaluate eligibility from normalized locations, facts and profile."""
    facts = facts or []
    reasons: list[str] = []
    evidence: dict = {"locations": locations, "facts_considered": [f.get("fact_type") for f in facts]}

    auth_fact = next((f for f in facts if f.get("fact_type") == "work_authorization"), None)
    if auth_fact:
        value = auth_fact.get("value")
        auth_str = value if isinstance(value, str) else str(value)
        if auth_str.startswith("NO_SPONSORSHIP") and profile.get("needs_sponsorship"):
            return EligibilityResult("INELIGIBLE", 0.9, ["NO_SPONSORSHIP"], evidence)
        if auth_str.startswith("AUTHORIZED_IN:"):
            region = auth_str.split(":", 1)[1].strip().upper()
            allowed = _country_allowed(region, profile)
            if allowed is False:
                return EligibilityResult("INELIGIBLE", 0.85, [f"AUTHORIZED_IN_{region}"], evidence)

    remote_locations = [loc for loc in locations if loc.get("remote")]
    onsite_locations = [loc for loc in locations if not loc.get("remote")]
    remote_rules = profile.get("remote_rules") or {"allow_remote": True}

    remote_excluded = False
    onsite_reasons: list[str] = []

    # Explicit country-restricted remote ("Remote — US only").
    for loc in remote_locations:
        allowed = _country_allowed(loc.get("country"), profile)
        if allowed is True:
            return EligibilityResult("ELIGIBLE", 0.9, ["REMOTE_ALLOWED_COUNTRY"], evidence)
        if allowed is False:
            remote_excluded = True
            reasons.append("REMOTE_COUNTRY_EXCLUDED")

    if remote_worldwide and remote_rules.get("allow_remote") and remote_rules.get("remote_worldwide"):
        return EligibilityResult("ELIGIBLE", 0.85, ["REMOTE_WORLDWIDE"], evidence)

    for loc in onsite_locations:
        allowed = _country_allowed(loc.get("country"), profile)
        if allowed is True:
            return EligibilityResult("ELIGIBLE", 0.85, [f"LOCATION_{(loc.get('country') or '').upper()}"], evidence)
        if allowed is False:
            onsite_reasons.append(f"LOCATION_EXCLUDED_{(loc.get('country') or '').upper()}")
    reasons.extend(onsite_reasons)

    if remote_excluded and not onsite_locations:
        # Remote explicitly restricted to an excluded country: no fallback.
        return EligibilityResult("INELIGIBLE", 0.85, reasons, evidence)

    if reasons:
        # All known onsite locations excluded; unrestricted remote fallback?
        if remote_locations and remote_rules.get("allow_remote") and not remote_excluded:
            return EligibilityResult("LIKELY", 0.6, reasons + ["REMOTE_FALLBACK"], evidence)
        return EligibilityResult("INELIGIBLE", 0.8, reasons, evidence)

    # Unknown country data.
    if remote_locations and remote_rules.get("allow_remote"):
        return EligibilityResult("LIKELY", 0.6, ["REMOTE_UNSPECIFIED_COUNTRY"], evidence)
    # Hard invariant: absence of restriction evidence is not worldwide eligibility.
    return EligibilityResult("UNCLEAR", 0.4, ["LOCATION_UNPARSEABLE"], evidence)
