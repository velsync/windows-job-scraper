"""Integration tests for the HTTP executor (SSRF + redirects + caps + 304)."""

import pytest

from jobscraper.acquisition.executors.http import HttpExecutor
from jobscraper.security.netpolicy import make_test_fixture_policy, source_network_policy
from tests.fixtures.fixture_server import FixtureServer

ENVELOPE_INPUTS = {
    "execution_plan_id": "plan-1",
    "request_id": "req-1",
    "attempt_id": "att-1",
    "run_source_plan_id": "rsp-1",
    "source_id": "src-1",
    "binding_id": "b-1",
    "binding_revision_id": "br-1",
    "adapter_id": "greenhouse",
    "adapter_version": "1.0.0",
    "strategy": "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
    "execution_class": "HTTP",
}


def make_plan(url, **kw):
    return {"method": "GET", "url": url, **kw}


@pytest.fixture()
def server():
    with FixtureServer() as srv:
        srv.add_json("/jobs", {"jobs": [{"id": 1, "title": "Eng"}]})
        yield srv


def test_fetch_ok(server):
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    result = executor.execute(ENVELOPE_INPUTS, make_plan(f"{server.base_url}/jobs"))
    assert result.ok
    assert result.envelope.status_code == 200
    assert b"Eng" in result.envelope.body
    assert result.envelope.body_hash
    assert result.envelope.transport == "HTTP"
    assert result.envelope.duration_ms >= 0


def test_loopback_denied_under_source_policy(server):
    executor = HttpExecutor(source_network_policy({server.host}))
    result = executor.execute(ENVELOPE_INPUTS, make_plan(f"{server.base_url}/jobs"))
    assert not result.ok
    assert result.envelope.failure_kind == "POLICY_REJECTED"
    assert "not a public destination" in (result.envelope.failure_detail or "")


def test_redirect_followed_and_validated(server):
    server.add(
        "GET",
        "/redirect-1",
        lambda req, body: (302, "text/plain", "", {"Location": "/jobs"}),
    )
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    result = executor.execute(ENVELOPE_INPUTS, make_plan(f"{server.base_url}/redirect-1"))
    assert result.ok
    assert result.envelope.status_code == 200
    assert len(result.envelope.redirect_chain) == 1


def test_redirect_to_private_blocked(server):
    server.add(
        "GET",
        "/redirect-evil",
        lambda req, body: (302, "text/plain", "", {"Location": "http://192.168.13.37/steal"}),
    )
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    result = executor.execute(ENVELOPE_INPUTS, make_plan(f"{server.base_url}/redirect-evil"))
    assert not result.ok
    assert result.envelope.failure_kind == "POLICY_REJECTED"


def test_redirect_budget_capped(server):
    server.add(
        "GET",
        "/loop",
        lambda req, body: (302, "text/plain", "", {"Location": "/loop"}),
    )
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    result = executor.execute(
        ENVELOPE_INPUTS, make_plan(f"{server.base_url}/loop", max_redirects=3)
    )
    assert not result.ok
    assert result.envelope.failure_kind == "UNSUPPORTED"
    assert len(result.envelope.redirect_chain) >= 3


def test_body_size_capped(server):
    big = "x" * 100_000
    server.add_json("/big", {"data": big})
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    result = executor.execute(
        ENVELOPE_INPUTS, make_plan(f"{server.base_url}/big", max_bytes=1000)
    )
    # Fetch succeeded but truncated at the cap.
    assert result.envelope.bytes_downloaded <= 1000


def test_timeout_enforced(server):
    import time

    def slow(req, body):
        time.sleep(3)
        return (200, "application/json", "{}", {})

    server.add("GET", "/slow", slow)
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    result = executor.execute(
        ENVELOPE_INPUTS, make_plan(f"{server.base_url}/slow", timeout_s=0.5)
    )
    assert not result.ok
    assert result.envelope.failure_kind == "TIMEOUT"


def test_304_surfaced(server):
    server.add(
        "GET",
        "/cached",
        lambda req, body: (
            304,
            "application/json",
            "",
            {"ETag": '"abc"'},
        ),
    )
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    result = executor.execute(
        ENVELOPE_INPUTS,
        make_plan(f"{server.base_url}/cached", validators={"If-None-Match": '"abc"'}),
    )
    assert result.envelope.was_304 is True
    assert result.envelope.body == b""
    assert result.envelope.validators_sent == {"If-None-Match": '"abc"'}


def test_sensitive_request_headers_never_sent(server):
    captured = {}

    def spy(req, body):
        captured.update({k.lower(): v for k, v in req.headers.items()})
        return (200, "application/json", "{}", {})

    server.add("GET", "/spy", spy)
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    executor.execute(
        ENVELOPE_INPUTS,
        make_plan(
            f"{server.base_url}/spy",
            headers={"Authorization": "Bearer secret", "Cookie": "a=b", "X-Trace": "1"},
        ),
    )
    assert "authorization" not in captured
    assert "cookie" not in captured
    assert captured.get("x-trace") == "1"


def test_response_headers_redacted(server):
    server.add(
        "GET",
        "/setcookie",
        lambda req, body: (200, "application/json", "{}", {"Set-Cookie": "session=abc"}),
    )
    executor = HttpExecutor(make_test_fixture_policy(server.host))
    result = executor.execute(ENVELOPE_INPUTS, make_plan(f"{server.base_url}/setcookie"))
    assert result.envelope.headers_redacted.get("set-cookie") == "[REDACTED]"
    assert "session=abc" not in str(result.envelope.headers_redacted)


def test_post_write_denied_by_plan_validation(server):
    from jobscraper.acquisition.contracts import RequestPlan

    with pytest.raises(ValueError):
        RequestPlan(method="POST", url=f"{server.base_url}/submit", body_json="{}").validate_read_only()

def test_readonly_search_post_allowed_with_capability(server):
    from jobscraper.acquisition.contracts import RequestPlan

    plan = RequestPlan(
        method="POST",
        url=f"{server.base_url}/search",
        body_json="{}",
        expected_operation_class="READ_ONLY_SEARCH_POST",
        operation_capability_ref="cap:readonly-search",
    )
    plan.validate_read_only()  # must not raise
