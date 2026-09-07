"""Deterministic explainable profile-relative scoring.

Authority: module 01 section 41. Each contribution includes rule, points,
evidence and rule version. Optional AI/embeddings may only ever provide
secondary suggestions, never the authoritative primary score.
"""

from __future__ import annotations

from dataclasses import dataclass

SCORER_VERSION = "scoring-v1"
SCORING_RULE_VERSION = "scoring-rules-v1"


@dataclass
class Contribution:
    rule: str
    points: float
    evidence: str
    rule_version: str = SCORING_RULE_VERSION

    def as_dict(self) -> dict:
        return {"rule": self.rule, "points": self.points, "evidence": self.evidence,
                "rule_version": self.rule_version}


def _token_overlap(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    return [k for k in keywords if k and k.lower() in low]


def score_job(
    *,
    title: str,
    description_text: str,
    company: str | None,
    locations: list[dict],
    salary: dict | None,
    facts: list[dict] | None,
    eligibility_verdict: str | None,
    profile: dict,
) -> tuple[float, list[dict]]:
    """Score a job for a profile. Deterministic; profile-relative."""
    weights = profile.get("weights") or {}
    facts = facts or []
    fact_types = {f.get("fact_type"): f.get("value") for f in facts}
    contributions: list[Contribution] = []
    title_low = (title or "").lower()
    desc_low = (description_text or "").lower()[:20000]
    corpus = f"{title_low} {desc_low}"

    # Title keyword fit.
    must = _token_overlap(title_low, profile.get("must_keywords") or [])
    should = _token_overlap(corpus, profile.get("should_keywords") or [])
    keywords = _token_overlap(corpus, profile.get("keywords") or [])
    negative = _token_overlap(corpus, profile.get("must_not_keywords") or profile.get("negative_terms") or [])

    if must:
        contributions.append(Contribution("title_must_keywords", 25.0 * (len(must) / max(1, len(profile["must_keywords"]))), f"must: {','.join(must[:5])}"))
    elif should:
        contributions.append(Contribution("title_fit", float(weights.get("title_fit", 15)), f"should: {','.join(should[:5])}"))
    if keywords:
        contributions.append(Contribution("keyword_hits", float(weights.get("keyword", 10)) * min(1.0, len(keywords) / 4.0), f"keywords: {','.join(keywords[:6])}"))
    if negative:
        contributions.append(Contribution("negative_terms", -10.0 * min(1.0, len(negative) / 2.0), f"negative: {','.join(negative[:4])}"))

    # Eligibility contribution.
    if eligibility_verdict == "ELIGIBLE":
        contributions.append(Contribution("eligible_location", float(weights.get("eligible_location", 20)), "eligibility: ELIGIBLE"))
    elif eligibility_verdict in ("LIKELY",):
        contributions.append(Contribution("eligible_location_likely", 10.0, "eligibility: LIKELY"))
    elif eligibility_verdict == "INELIGIBLE":
        contributions.append(Contribution("ineligible_location", -100.0, "eligibility: INELIGIBLE"))
    elif eligibility_verdict == "UNCLEAR":
        contributions.append(Contribution("location_unclear", -3.0, "eligibility: UNCLEAR"))

    # Salary.
    salary_floor = profile.get("salary_floor")
    if salary and salary.get("annual_min_usd") is not None:
        contributions.append(Contribution("salary_known", float(weights.get("salary_known", 8)), "salary stated"))
        if salary_floor is not None and salary["annual_min_usd"] >= float(salary_floor):
            contributions.append(Contribution("salary_above_floor", float(weights.get("salary_above_floor", 8)), f"annual_min_usd={salary['annual_min_usd']:.0f}"))
        elif salary_floor is not None:
            contributions.append(Contribution("salary_below_floor", -6.0, f"annual_min_usd={salary['annual_min_usd']:.0f}"))
    elif salary is None:
        contributions.append(Contribution("salary_unknown", float(weights.get("unknown_salary", -8)), "salary unknown (not zero)"))

    # Seniority vs preference.
    seniority = fact_types.get("seniority")
    allow = profile.get("seniority_allow") or []
    if seniority and allow and seniority not in allow:
        contributions.append(Contribution("seniority_above_preference", float(weights.get("seniority_above_preference", -12)), f"seniority={seniority}"))

    # Remote fit.
    remote_scope = fact_types.get("remote_scope")
    if remote_scope in ("REMOTE_FULL", "REMOTE_FIRST") and (profile.get("remote_rules") or {}).get("allow_remote"):
        contributions.append(Contribution("remote_fit", float(weights.get("remote_fit", 6)), f"remote_scope={remote_scope}"))

    # Company watch/blocklist handled at inbox layer (hard filters), not score.

    total = sum(c.points for c in contributions)
    return round(total, 2), [c.as_dict() for c in contributions]
