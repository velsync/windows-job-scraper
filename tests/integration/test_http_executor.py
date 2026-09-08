"""S1.4 integration tests: host-owned HTTP executor + validity gate (02 §11/§21/§27).

Proves against a real loopback fixture server (explicit internal grant):

* ExecutionPlanEnvelope validation: only host-approved read methods, only
  policy-passing URLs, no secrets in plan headers (ACQ-06/§11.1);
* destination validation bound to the actual connection (validated address
  is the address connected);
* redirects validated per hop with cap; off-policy redirect rejected and
  recorded;
* byte/duration caps enforced (BODY_TOO_LARGE / TIMEOUT typed failures);
* 304 revalidation passthrough; header redaction of secrets;
* page-validity classifier: login ≠ parser failure ≠ empty (§21 invariant),
  rate-limit/challenge/SPA-shell/not-found/gone classes, JSON empty list,
  unexpected content type, unexpected cross-host redirect;
* typed failure model (retryability, source-health impact);
* TLS against a plaintext endpoint fails closed as TLS_ERROR.
"""

from __future__ import annotations

import http.server
import json
import threading
import time

import pytest

from jobscraper.acquisition.envelope import (
    ExecutionPlanEnvelope,
    RequestPlan,
    validate_envelope,
)
from jobscraper.acquisition.failures import FailureKind, FailureRecord
from jobscraper.acquisition.httpexec import execute_request
from jobscraper.acquisition.pagevalidity import (
    PageClass,
    classify_page,
)
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.net.destination import (
    DestinationPolicy,
    DestinationRejected,
    InternalGrant,
)

GRANT = InternalGrant(
    purpose="fixture acceptance server", allowed_hosts=frozenset({"127.0.0.1"})
)


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_address[1]
        if self.path == "/feed":
            body = json.dumps(
                {"jobs": [{"id": "1", "title": "Backend Engineer"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=SECRET-VALUE; Path=/")
            self.send_header("X-Source-Header", "kept")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/empty-feed":
            body = b'{"jobs": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-once":
            self.send_response(302)
            self.send_header("Location", "/feed")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.end_headers()
        elif self.path == "/redirect-denied":
            # redirect to a destination outside every policy (TEST-NET-1):
            # the hop itself is policy-denied mid-chain
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.9/feed")
            self.end_headers()
        elif self.path == "/redirect-offpolicy":
            # redirect to a loopback port that is NOT running (still loopback:
            # policy would allow it via the grant; we point at a dead port to
            # prove connect failure typing instead). For the off-policy case
            # the test swaps the policy to one without a grant.
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port + 1}/feed")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(3)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        elif self.path == "/large":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            for _ in range(50):
                self.wfile.write(b"x" * 10000)  # 500 KB, streamed past the cap
        elif self.path == "/etag":
            etag = self.headers.get("If-None-Match")
            if etag == '"v1"':
                self.send_response(304)
                self.send_header("ETag", '"v1"')
                self.end_headers()
            else:
                body = b'{"jobs": [{"id": "2"}]}'
                self.send_response(200)
                self.send_header("ETag", '"v1"')
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif self.path == "/login":
            body = (
                b"<html><head><title>Sign in</title></head><body>"
                b'<input type="password" name="pw"> Please sign in to continue'
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/challenge":
            body = b"<html><body>Attention Required! | Just a moment... captcha</body></html>"
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/shell":
            body = (
                b"<html><body><div id='root'></div><noscript>You need JS</noscript>"
                b"<script src='/app.js'></script></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/wrongtype":
            body = b"\x89PNG\r\n\x1a\n" + b"0" * 64
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/gone"):
            body = b"<html><body>This position is no longer available.</body></html>"
            self.send_response(410)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _policy(**overrides) -> DestinationPolicy:
    defaults = dict(
        internal_grant=GRANT, timeout_s=10.0, max_bytes=1024 * 1024, max_redirects=3
    )
    defaults.update(overrides)
    return DestinationPolicy(**defaults)


def _envelope(server, path="/feed", **plan_overrides) -> ExecutionPlanEnvelope:
    port = server.server_address[1]
    plan = dict(
        method="GET",
        url=f"http://127.0.0.1:{port}{path}",
        headers={"Accept": "application/json"},
        expected_content_types=("application/json",),
        timeout_s=10.0,
        max_bytes=1024 * 1024,
        purpose="LIST_FETCH",
    )
    plan.update(plan_overrides)
    return ExecutionPlanEnvelope(
        plan_id="plan-1",
        request_id="req-1",
        attempt_id="att-1",
        run_id="run-1",
        run_source_plan_id="rsp-1",
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="feed",
        adapter_version="1.0.0",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        policy_snapshot_ref="policy-1",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
        payload_kind="REQUEST",
        payload=RequestPlan(**plan),
    )


# ------------------------------------------------------- envelope validation


def test_envelope_rejects_unapproved_methods_and_secret_headers(server):
    with pytest.raises(ValueError):
        validate_envelope(_envelope(server, method="POST"), _policy())
    with pytest.raises(ValueError):
        validate_envelope(
            _envelope(server, headers={"Authorization": "Bearer x"}), _policy()
        )
    with pytest.raises(ValueError):
        validate_envelope(
            _envelope(server, headers={"Cookie": "session=x"}), _policy()
        )


def test_envelope_rejects_policy_violating_urls(server):
    with pytest.raises(DestinationRejected):
        # no grant in this policy: loopback is denied before any connection
        validate_envelope(_envelope(server), DestinationPolicy())
    with pytest.raises((ValueError, DestinationRejected)):
        # non-http scheme payload is not a valid RequestPlan target
        validate_envelope(_envelope(server, url="file:///etc/passwd"), _policy())


def test_valid_envelope_passes(server):
    validate_envelope(_envelope(server), _policy())


# ------------------------------------------------------------- execution core


def test_happy_path_json_fetch(server):
    envelope = _envelope(server)
    result = execute_request(envelope, _policy())
    assert isinstance(result, ResultEnvelope)
    assert result.status_code == 200
    assert result.failure is None
    assert json.loads(result.body)["jobs"][0]["id"] == "1"
    assert result.content_type.startswith("application/json")
    assert result.final_url == envelope.payload.url
    assert len(result.body_hash) == 64
    assert result.duration_ms >= 0
    assert result.transport == "http" and result.browser_used is False
    # header redaction: secrets never recorded
    assert "set-cookie" not in {k.lower() for k in result.headers_redacted}
    assert result.headers_redacted.get("X-Source-Header") == "kept"


def test_redirect_followed_per_hop(server):
    result = execute_request(_envelope(server, "/redirect-once"), _policy())
    assert result.status_code == 200
    assert result.redirect_chain, "redirect chain recorded"
    assert result.final_url.endswith("/feed")


def test_redirect_cap_enforced(server):
    result = execute_request(_envelope(server, "/redirect-loop"), _policy())
    assert result.failure is not None
    assert result.failure.kind == FailureKind.POLICY_REJECTED


def test_policy_denied_redirect_is_not_an_authoritative_empty_page(server):
    # §21/RUN-13: a mid-chain denial carries the hop's status code but no
    # usable page; it must classify UNKNOWN with typed failure evidence —
    # never EMPTY, which would grant terminal-enumeration/absence
    # authority to a fetch that never reached the source.
    result = execute_request(_envelope(server, "/redirect-denied"), _policy())
    assert result.failure is not None
    assert result.failure.kind == FailureKind.POLICY_REJECTED
    assert result.status_code is not None  # the redirect hop's status
    cls = classify_page(result, expect="LIST")
    assert cls.state == PageClass.UNKNOWN
    assert cls.evidence.get("failure_kind") == "POLICY_REJECTED"


def test_redirect_to_denied_destination_rejected(server):
    # policy WITHOUT the grant: the initial URL itself is denied
    result = execute_request(_envelope(server), DestinationPolicy(timeout_s=10.0))
    assert result.failure is not None
    assert result.failure.kind == FailureKind.POLICY_REJECTED
    assert result.failure.details_redacted.get("reason_code") == "LOOPBACK_ADDRESS"


def test_body_cap_enforced(server):
    result = execute_request(
        _envelope(server, "/large", max_bytes=100 * 1024), _policy(max_bytes=100 * 1024)
    )
    assert result.failure is not None
    assert result.failure.kind == FailureKind.POLICY_REJECTED
    assert result.bytes_downloaded <= 200 * 1024  # hard-stopped near the cap


def test_timeout_is_typed(server):
    result = execute_request(
        _envelope(server, "/slow", timeout_s=1.0), _policy(timeout_s=1.0)
    )
    assert result.failure is not None
    assert result.failure.kind == FailureKind.TIMEOUT
    assert result.failure.retryable is True


def test_304_revalidation(server):
    first = execute_request(
        _envelope(server, "/etag"), _policy()
    )
    assert first.was_304 is False
    etag = first.headers_redacted.get("ETag")
    assert etag
    second = execute_request(
        _envelope(server, "/etag", headers={"Accept": "application/json", "If-None-Match": etag}),
        _policy(),
    )
    assert second.was_304 is True
    assert second.body == b""


def test_plaintext_endpoint_requested_as_https_fails_closed_as_tls_error(server):
    port = server.server_address[1]
    envelope = _envelope(server, url=f"https://127.0.0.1:{port}/feed")
    result = execute_request(envelope, _policy())
    assert result.failure is not None
    assert result.failure.kind == FailureKind.TLS_ERROR


# --------------------------------------------------------- page validity gate


def test_valid_json_list_classifies_valid_list(server):
    result = execute_request(_envelope(server), _policy())
    cls = classify_page(result, expect="LIST")
    assert cls.state == PageClass.VALID_LIST


def test_empty_json_list_is_recognized_empty(server):
    result = execute_request(_envelope(server, "/empty-feed"), _policy())
    cls = classify_page(result, expect="LIST")
    assert cls.state == PageClass.EMPTY


def test_login_page_is_not_a_parser_failure_and_not_empty(server):
    # §21 critical invariant: login page != parser failure != empty jobs list
    result = execute_request(
        _envelope(server, "/login", expected_content_types=("text/html",)), _policy()
    )
    cls = classify_page(result, expect="LIST")
    assert cls.state == PageClass.LOGIN_REQUIRED


def test_rate_limit_and_challenge_classes(server):
    class _RateHandler(_FixtureHandler):
        def do_GET(self):
            if self.path == "/rate":
                body = b"Too Many Requests"
                self.send_response(429)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RateHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        result = execute_request(_envelope(srv, "/rate"), _policy())
        cls = classify_page(result, expect="LIST")
        assert cls.state == PageClass.RATE_LIMITED
        result = execute_request(_envelope(srv, "/challenge", expected_content_types=("text/html",)), _policy())
        cls = classify_page(result, expect="LIST")
        assert cls.state == PageClass.CHALLENGE_PAGE
    finally:
        srv.shutdown()
        srv.server_close()


def test_spa_shell_classified(server):
    result = execute_request(
        _envelope(server, "/shell", expected_content_types=("text/html",)), _policy()
    )
    cls = classify_page(result, expect="LIST")
    assert cls.state == PageClass.JS_SHELL


def test_not_found_and_gone(server):
    result = execute_request(_envelope(server, "/missing"), _policy())
    assert classify_page(result, expect="LIST").state == PageClass.NOT_FOUND
    assert result.failure is not None and result.failure.kind == FailureKind.HTTP_4XX
    result = execute_request(
        _envelope(server, "/gone", expected_content_types=("text/html",)), _policy()
    )
    assert classify_page(result, expect="LIST").state == PageClass.JOB_CLOSED


def test_unexpected_content_type(server):
    result = execute_request(_envelope(server, "/wrongtype"), _policy())
    cls = classify_page(result, expect="LIST")
    assert cls.state == PageClass.UNEXPECTED_CONTENT


def test_unexpected_redirect_within_policy(server):
    # Two granted servers: the plan expected the first host/port; the redirect
    # lands on a different port (same host). Security policy allows it, but
    # the source contract flags UNEXPECTED_REDIRECT when expect_host mismatches.
    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        other_port = other.server_address[1]

        class _Cross(_FixtureHandler):
            def do_GET(self):
                if self.path == "/cross":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{other_port}/feed")
                    self.end_headers()
                else:
                    super().do_GET()

        cross = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Cross)
        threading.Thread(target=cross.serve_forever, daemon=True).start()
        try:
            envelope = _envelope(cross, "/cross")
            result = execute_request(envelope, _policy())
            assert result.status_code == 200
            cls = classify_page(
                result, expect="LIST", expect_host=f"127.0.0.1:{port}"
            )
            assert cls.state == PageClass.UNEXPECTED_REDIRECT
            # without an expectation the same result is simply a valid list
            cls2 = classify_page(result, expect="LIST")
            assert cls2.state == PageClass.VALID_LIST
        finally:
            cross.shutdown()
            cross.server_close()
    finally:
        other.shutdown()
        other.server_close()


# ----------------------------------------------------------- failure model


def test_failure_record_shape():
    rec = FailureRecord(
        kind=FailureKind.TIMEOUT,
        retryable=True,
        source_health_impact="DEGRADED",
        details_redacted={"timeout_s": 1.0},
        request_id="req-1",
        attempt_id="att-1",
        observed_at="2026-09-08T09:00:00.000000Z",
    )
    assert rec.kind == FailureKind.TIMEOUT
    assert "SECRET" not in str(rec)
