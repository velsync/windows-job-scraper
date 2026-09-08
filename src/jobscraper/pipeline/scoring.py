"""Explainable deterministic scoring (01 §41).

Scoring is deterministic and profile-relative. Every contribution carries
rule, points, evidence and rule_version. Optional AI may later provide
secondary suggestions — never the authoritative primary score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SCORER_VERSION = "scorer-s1-1"
RULES_VERSION = "s1-rules-1"

TITLE_KEYWORD_POINTS = 15
ELIGIBILITY_POINTS = {"ELIGIBLE": 20, "LIKELY": 10, "UNCLEAR": 0, "UNLIKELY": -15, "INELIGIBLE": -30}
SALARY_ABOVE_FLOOR_POINTS = 10
SALARY_BELOW_FLOOR_POINTS = -10
SALARY_UNKNOWN_POINTS = -5


@dataclass(frozen=True)
class ScoreResult:
    score: float
    breakdown: list = field(default_factory=list)
    scorer_version: str = SCORER_VERSION
    rule_version: str = RULES_VERSION


def _contribution(rule: str, points: float, evidence) -> dict:
    return {
        "rule": rule,
        "points": points,
        "evidence": evidence,
        "rule_version": RULES_VERSION,
    }


def score_job(*, job_data: dict, profile: dict) -> ScoreResult:
    """Deterministic profile-relative score with a full contribution
    breakdown. Unknown salary penalizes but never fabricates a value."""
    breakdown: list[dict] = []

    normalized_title = str(job_data.get("normalized_title") or job_data.get("title") or "").lower()
    keywords = [str(k).lower() for k in profile.get("keywords") or []]
    matched = [k for k in keywords if k in normalized_title]
    if matched:
        breakdown.append(
            _contribution("title_keyword_fit", TITLE_KEYWORD_POINTS * len(matched),
                          {"matched_keywords": matched})
        )

    verdict = job_data.get("eligibility_verdict") or "UNCLEAR"
    points = ELIGIBILITY_POINTS.get(verdict, 0)
    if points:
        breakdown.append(_contribution("eligibility", points, {"verdict": verdict}))

    floor = profile.get("salary_floor") or {}
    salary_min = job_data.get("salary_min")
    if salary_min is None:
        breakdown.append(_contribution("salary_unknown", SALARY_UNKNOWN_POINTS,
                                       {"salary_known": False}))
    elif floor.get("amount") is not None and (job_data.get("salary_currency") or "") == (floor.get("currency") or "EUR"):
        if float(salary_min) >= float(floor["amount"]):
            breakdown.append(_contribution("salary_above_floor", SALARY_ABOVE_FLOOR_POINTS,
                                           {"salary_min": salary_min, "floor": floor["amount"]}))
        else:
            breakdown.append(_contribution("salary_below_floor", SALARY_BELOW_FLOOR_POINTS,
                                           {"salary_min": salary_min, "floor": floor["amount"]}))

    score = sum(c["points"] for c in breakdown)
    return ScoreResult(score=float(score), breakdown=breakdown)


__all__ = ["RULES_VERSION", "SCORER_VERSION", "ScoreResult", "score_job"]
