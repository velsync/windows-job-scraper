"""Unit tests: eligibility verdicts and deterministic scoring (§36/§41)."""

from jobscraper.normalize.eligibility import evaluate_eligibility
from jobscraper.normalize.scoring import score_job

PROFILE = {
    "home_country": "DE",
    "eligible_countries": ["DE", "NL", "FR"],
    "needs_sponsorship": True,
    "remote_rules": {"allow_remote": True, "remote_worldwide": True},
    "keywords": ["python"],
    "must_keywords": ["python"],
    "min_score_inbox": 10,
}


def test_home_country_location_eligible():
    res = evaluate_eligibility(
        title="Engineer",
        locations=[{"country": "DE", "city": "Berlin", "remote": False}],
        profile=PROFILE,
    )
    assert res.verdict == "ELIGIBLE"
    assert res.reason_codes == ["LOCATION_DE"]


def test_excluded_country_ineligible():
    res = evaluate_eligibility(
        title="Engineer",
        locations=[{"country": "US", "city": "NYC", "remote": False}],
        profile=PROFILE,
    )
    assert res.verdict == "INELIGIBLE"
    assert "LOCATION_EXCLUDED_US" in res.reason_codes


def test_no_sponsorship_with_needs_sponsorship_ineligible():
    res = evaluate_eligibility(
        title="Engineer",
        locations=[{"country": "DE", "remote": False}],
        facts=[{"fact_type": "work_authorization", "value": "NO_SPONSORSHIP"}],
        profile=PROFILE,
    )
    assert res.verdict == "INELIGIBLE"


def test_absent_restriction_is_not_worldwide():
    # No location info, no remote: unknown stays UNCLEAR.
    res = evaluate_eligibility(title="Engineer", locations=[], profile=PROFILE)
    assert res.verdict == "UNCLEAR"


def test_remote_worldwide_eligible_when_allowed():
    res = evaluate_eligibility(
        title="Engineer", locations=[], profile=PROFILE, remote_worldwide=True
    )
    assert res.verdict == "ELIGIBLE"


def test_remote_country_restricted():
    res = evaluate_eligibility(
        title="Engineer",
        locations=[{"country": "US", "remote": True}],
        profile=PROFILE,
    )
    assert res.verdict == "INELIGIBLE"
    res_de = evaluate_eligibility(
        title="Engineer",
        locations=[{"country": "DE", "remote": True}],
        profile=PROFILE,
    )
    assert res_de.verdict == "ELIGIBLE"


def test_authorized_in_excluded_region():
    res = evaluate_eligibility(
        title="Engineer",
        locations=[],
        facts=[{"fact_type": "work_authorization", "value": "AUTHORIZED_IN: US"}],
        profile=PROFILE,
    )
    assert res.verdict == "INELIGIBLE"


# ------------------------------------------------------------------ scoring
def test_score_deterministic_with_breakdown():
    args = dict(
        title="Senior Python Engineer",
        description_text="Python FastAPI PostgreSQL role in Berlin.",
        company=None,
        locations=[{"country": "DE", "remote": False}],
        salary=None,
        facts=[{"fact_type": "seniority", "value": "senior"}],
        eligibility_verdict="ELIGIBLE",
        profile=PROFILE,
    )
    s1, b1 = score_job(**args)
    s2, b2 = score_job(**args)
    assert s1 == s2
    assert b1 == b2
    rules = {c["rule"]: c["points"] for c in b1}
    assert "title_must_keywords" in rules
    assert rules["eligible_location"] > 0
    assert rules["salary_unknown"] < 0  # unknown salary: negative, not zero
    for c in b1:
        assert c["rule_version"]


def test_ineligible_scores_deeply_negative():
    s, b = score_job(
        title="Python Developer",
        description_text="python",
        company=None,
        locations=[{"country": "US"}],
        salary=None,
        facts=[],
        eligibility_verdict="INELIGIBLE",
        profile=PROFILE,
    )
    assert s < -50


def test_known_salary_beats_unknown():
    base = dict(
        title="Python Developer",
        description_text="python",
        company=None,
        locations=[{"country": "DE"}],
        facts=[],
        eligibility_verdict="ELIGIBLE",
        profile=PROFILE,
    )
    s_unknown, _ = score_job(**base, salary=None)
    s_known, b = score_job(**base, salary={"annual_min_usd": 90000})
    assert s_known > s_unknown
    assert any(c["rule"] == "salary_known" for c in b)
