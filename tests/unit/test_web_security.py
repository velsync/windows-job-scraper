"""Unit tests for localhost security primitives (S0.5)."""

from jobscraper.web.security import (
    CSP,
    MAX_BODY_BYTES,
    RoutePolicy,
    SecurityViolation,
    expected_origin,
    require_content_type,
    safe_external_url,
    security_headers,
    validate_host,
    validate_origin,
)


class TestHostValidation:
    def test_exact_loopback_accepted(self):
        assert validate_host("127.0.0.1:8431", 8431) is True

    def test_wrong_port_rejected(self):
        assert validate_host("127.0.0.1:9999", 8431) is False

    def test_localhost_alias_rejected(self):
        assert validate_host("localhost:8431", 8431) is False

    def test_attacker_domain_rejected(self):
        assert validate_host("evil.example:8431", 8431) is False

    def test_embedded_userinfo_rejected(self):
        assert validate_host("evil@127.0.0.1:8431", 8431) is False

    def test_malformed_host_rejected(self):
        for bad in [None, "", "127.0.0.1", ":8431", "127.0.0.1:abc", " 127.0.0.1:8431 "]:
            assert validate_host(bad, 8431) is False

    def test_ipv6_loopback_exact(self):
        assert validate_host("[::1]:8431", 8431, bound_host="::1") is True
        assert validate_host("[::1]:9999", 8431, bound_host="::1") is False


class TestOriginValidation:
    def test_exact_origin_accepted(self):
        assert validate_origin("http://127.0.0.1:8431", expected="http://127.0.0.1:8431") is True

    def test_hostile_origin_rejected(self):
        assert validate_origin("http://evil.example", expected="http://127.0.0.1:8431") is False
        assert validate_origin("https://127.0.0.1:8431", expected="http://127.0.0.1:8431") is False

    def test_null_origin_rejected(self):
        assert validate_origin("null", expected="http://127.0.0.1:8431") is False

    def test_missing_origin_policy(self):
        assert validate_origin(None, expected="http://127.0.0.1:8431", allow_missing=True) is True
        assert validate_origin(None, expected="http://127.0.0.1:8431", allow_missing=False) is False

    def test_other_port_rejected(self):
        assert validate_origin("http://127.0.0.1:9999", expected="http://127.0.0.1:8431") is False


class TestHeaders:
    def test_csp_forbids_remote_scripts(self):
        assert "default-src 'self'" in CSP
        assert "script-src 'self'" in CSP
        # No unsafe-inline/remote origins for scripts.
        script_csp = CSP.split("script-src 'self';")[0] + "script-src 'self';"
        assert "http" not in CSP.split("script-src")[1].split(";")[0]
        assert "unsafe-inline" not in CSP.split("script-src")[1].split(";")[0]

    def test_no_cors_headers_emitted(self):
        headers = security_headers()
        assert not any(k.lower().startswith("access-control-") for k in headers)
        assert "X-Frame-Options" in headers

    def test_body_size_bounded(self):
        assert 0 < MAX_BODY_BYTES <= 2 * 1024 * 1024


class TestContentType:
    def test_allowed(self):
        require_content_type("application/json", {"application/json"})
        require_content_type("application/json; charset=utf-8", {"application/json"})

    def test_rejected(self):
        import pytest

        with pytest.raises(SecurityViolation):
            require_content_type("text/plain", {"application/json"})
        with pytest.raises(SecurityViolation):
            require_content_type(None, {"application/json"})


class TestSafeExternalUrl:
    def test_http_https_allowed(self):
        assert safe_external_url("https://boards.example/job/1") == "https://boards.example/job/1"
        assert safe_external_url("http://example.com/j") == "http://example.com/j"

    def test_active_schemes_rejected(self):
        for bad in [
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:x",
            "file:///C:/x",
            "",
            None,
            "https://user:pass@example.com/j",
        ]:
            assert safe_external_url(bad) is None

    def test_relative_rejected(self):
        assert safe_external_url("/jobs/1") is None


def test_expected_origin_string():
    assert expected_origin("127.0.0.1", 8000) == "http://127.0.0.1:8000"


def test_route_policy_defaults():
    policy = RoutePolicy()
    assert policy.public is False
    assert policy.mutation is False
    assert policy.csrf_required is False
