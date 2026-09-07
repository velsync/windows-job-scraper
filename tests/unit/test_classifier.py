"""Unit tests for the page validity classifier (module 02 section 21)."""

from jobscraper.acquisition.classifier import classify
from jobscraper.acquisition.contracts import PageClass, ResultEnvelope


def envelope(
    *,
    status=200,
    content_type="application/json",
    body=b"{}",
    url="https://boards.example/jobs",
    failure=None,
):
    return ResultEnvelope(
        execution_plan_id="p",
        request_id="r",
        attempt_id="a",
        run_source_plan_id="rsp",
        source_id="s",
        binding_id="b",
        binding_revision_id="br",
        adapter_id="ad",
        adapter_version="1",
        strategy="HTTP_HTML",
        execution_class="HTTP",
        requested_url=url,
        final_url=url,
        status_code=status,
        headers_redacted={},
        content_type=content_type,
        body=body,
        body_hash=None,
        normalized_content_hash=None,
        fetched_at="2026-01-01T00:00:00.000000Z",
        duration_ms=10,
        bytes_downloaded=len(body),
        redirect_chain=[],
        transport="HTTP",
        browser_used=False,
        robots_decision=None,
        validators_sent={},
        was_304=False,
        resource_blocking_applied=None,
        failure_kind=failure,
        failure_detail=None,
    )


def test_valid_json_list():
    c = classify(envelope(body=b'{"jobs": [{"id": 1}]}'))
    assert c.page_class == PageClass.VALID_LIST
    assert c.is_valid


def test_valid_json_empty_enumeration():
    c = classify(envelope(body=b'{"jobs": []}'))
    assert c.page_class == PageClass.EMPTY
    assert not c.is_valid


def test_valid_json_array():
    c = classify(envelope(body=b'[{"id": 1}]'))
    assert c.page_class == PageClass.VALID_LIST


def test_login_page_not_parser_failure():
    html = b"<html><title>Sign In</title><body>Please log in to continue</body></html>"
    c = classify(envelope(content_type="text/html", body=html, status=200))
    assert c.page_class == PageClass.LOGIN_REQUIRED
    assert not c.is_valid


def test_auth_expired_distinct():
    html = b"<html><body>Your session has expired. Please sign in again.</body></html>"
    c = classify(envelope(content_type="text/html", body=html, status=200))
    assert c.page_class == PageClass.AUTH_EXPIRED


def test_rate_limited_429():
    c = classify(envelope(status=429, body=b"{}"))
    assert c.page_class == PageClass.RATE_LIMITED


def test_challenge_page():
    c = classify(envelope(status=403, body=b"Verify you are human to continue"))
    assert c.page_class == PageClass.CHALLENGE_PAGE


def test_not_found():
    c = classify(envelope(status=404))
    assert c.page_class == PageClass.NOT_FOUND


def test_js_shell_detected():
    html = b'<html><body><div id="root"></div><noscript>Enable JavaScript</noscript></body></html>'
    c = classify(envelope(content_type="text/html", body=html))
    assert c.page_class == PageClass.JS_SHELL


def test_js_shell_with_source_signature_is_valid():
    html = b'<html><body><div id="root"></div><script src="x"></script>' \
           b'<div class="job-card" data-testid="job-card">Engineer</div></body></html>'
    c = classify(
        envelope(content_type="text/html", body=html),
        source_signatures=("job-card",),
    )
    assert c.page_class == PageClass.VALID_LIST


def test_closed_job_page():
    html = b"<html><body><h1>Software Engineer</h1><p>This position is no longer available.</p></body></html>"
    c = classify(envelope(content_type="text/html", body=html), expected="JOB")
    assert c.page_class == PageClass.JOB_CLOSED


def test_invalid_json_unexpected_content():
    c = classify(envelope(body=b"{invalid json!!", content_type="application/json"))
    assert c.page_class == PageClass.UNEXPECTED_CONTENT


def test_transport_failure_unknown():
    c = classify(envelope(status=None, failure="CONNECT_ERROR"))
    assert c.page_class == PageClass.UNKNOWN


def test_json_error_payload():
    c = classify(envelope(body=b'{"error": "not found"}'))
    assert c.page_class == PageClass.UNEXPECTED_CONTENT


def test_html_list_page_valid():
    html = b"<html><body><ul><li><a href='/job/1'>Engineer</a></li></ul></body></html>"
    c = classify(envelope(content_type="text/html", body=html))
    assert c.page_class == PageClass.VALID_LIST


def test_html_job_page_valid():
    html = b"<html><body><h1>Senior Engineer</h1><div>description</div></body></html>"
    c = classify(envelope(content_type="text/html", body=html), expected="JOB")
    assert c.page_class == PageClass.VALID_JOB


def test_missing_source_signature_unexpected():
    html = b"<html><body><div class='card'>x</div></body></html>"
    c = classify(envelope(content_type="text/html", body=html), source_signatures=("greenhouse",))
    assert c.page_class == PageClass.UNEXPECTED_CONTENT


def test_304_not_classified():
    c = classify(envelope(status=304, body=b""))
    assert c.page_class == PageClass.UNKNOWN
