from jobscraper.acquisition.crawler.robots import (
    RobotsDecisionKind,
    RobotsPolicy,
    RobotsPolicyStatus,
    build_robots_request_plan,
    evaluate_robots_policy,
    parse_robots_result,
)
from jobscraper.acquisition.result import ResultEnvelope


def _result(status, body=b""):
    return ResultEnvelope(
        execution_plan_id="p", request_id="r", attempt_id="a", run_source_plan_id="rsp",
        source_id="src", binding_id="b", binding_revision_id="br", adapter_id="x",
        adapter_version="1", strategy="HTTP_HTML", execution_class="HTTP",
        requested_url="https://jobs.example.test/robots.txt", final_url="https://jobs.example.test/robots.txt",
        status_code=status, content_type="text/plain", body=body,
    )


def test_robots_longest_matching_rule_wins():
    policy = RobotsPolicy(RobotsPolicyStatus.AVAILABLE, "ok", (
        "User-agent: *", "Disallow: /jobs", "Allow: /jobs/public",
    ))
    assert evaluate_robots_policy(policy, "https://jobs.example.test/jobs/private").kind is RobotsDecisionKind.DENY
    assert evaluate_robots_policy(policy, "https://jobs.example.test/jobs/public/1").kind is RobotsDecisionKind.ALLOW


def test_robots_allow_wins_equal_specificity_tie():
    policy = RobotsPolicy(RobotsPolicyStatus.AVAILABLE, "ok", (
        "User-agent: *", "Disallow: /jobs", "Allow: /jobs",
    ))
    assert evaluate_robots_policy(policy, "https://jobs.example.test/jobs").kind is RobotsDecisionKind.ALLOW


def test_missing_or_malformed_robots_never_fabricates_denial_or_empty_membership():
    missing = parse_robots_result(_result(404))
    malformed = parse_robots_result(_result(200, b"Disallow: /"))
    assert missing.status is RobotsPolicyStatus.NOT_PRESENT
    assert malformed.status is RobotsPolicyStatus.UNKNOWN


def test_robots_request_plan_uses_same_source_origin_and_source_crawl_role():
    plan = build_robots_request_plan("https://jobs.example.test/careers/list?x=1")
    assert plan.url == "https://jobs.example.test/robots.txt"
    assert plan.method == "GET"
    assert plan.purpose == "SOURCE_CRAWL"
    assert plan.expected_operation_class == "READ"
