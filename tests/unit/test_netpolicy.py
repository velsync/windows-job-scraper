"""Unit tests for the outbound network policy (SSRF controls)."""

import pytest

from jobscraper.security.netpolicy import (
    DestinationPolicyError,
    NetworkPolicy,
    clear_dns_cache,
    ip_is_public,
    is_ip_literal,
    parse_url,
    source_network_policy,
    make_test_fixture_policy,
)


class TestIPLiterals:
    @pytest.mark.parametrize(
        "addr,expected",
        [
            ("127.0.0.1", False),
            ("127.0.0.0", False),
            ("10.1.2.3", False),
            ("192.168.1.1", False),
            ("172.16.0.1", False),
            ("172.31.255.255", False),
            ("169.254.169.254", False),  # cloud metadata
            ("100.64.0.1", False),
            ("0.0.0.0", False),
            ("224.0.0.1", False),
            ("240.0.0.1", False),
            ("198.18.0.1", False),
            ("::1", False),
            ("fc00::1", False),
            ("fe80::1", False),
            ("::", False),
            ("::ffff:127.0.0.1", False),
            ("8.8.8.8", True),
            ("1.1.1.1", True),
            ("2606:4700::1111", True),
        ],
    )
    def test_ip_classification(self, addr, expected):
        import ipaddress

        assert ip_is_public(ipaddress.ip_address(addr)) is expected

    def test_is_ip_literal(self):
        assert is_ip_literal("1.2.3.4") is True
        assert is_ip_literal("[::1]") is True
        assert is_ip_literal("example.com") is False


class TestURLParsing:
    def test_scheme_policy(self):
        with pytest.raises(DestinationPolicyError):
            parse_url("file:///etc/passwd")
        with pytest.raises(DestinationPolicyError):
            parse_url("ftp://example.com/x")
        with pytest.raises(DestinationPolicyError):
            parse_url("javascript:alert(1)")

    def test_credentials_rejected(self):
        with pytest.raises(DestinationPolicyError):
            parse_url("https://user:pass@example.com/jobs")

    def test_ports_and_path(self):
        scheme, host, port, path = parse_url("https://Example.com:8443/jobs?x=1")
        assert scheme == "https"
        assert host == "example.com"
        assert port == 8443
        assert path == "/jobs?x=1"
        scheme, host, port, path = parse_url("http://example.com")
        assert port == 80


class TestPolicyEnforcement:
    def test_loopback_denied_for_source_policy(self):
        clear_dns_cache()
        policy = source_network_policy({"127.0.0.1"})
        with pytest.raises(DestinationPolicyError):
            policy.check_url("http://127.0.0.1:8000/fixture")

    def test_private_denied_for_source_policy(self):
        clear_dns_cache()
        policy = source_network_policy({"10.0.0.5"})
        with pytest.raises(DestinationPolicyError):
            policy.check_url("http://10.0.0.5/api")

    def test_host_scope_enforced(self):
        clear_dns_cache()
        policy = source_network_policy({"boards-api.greenhouse.io"})
        with pytest.raises(DestinationPolicyError):
            policy.check_url("https://evil.example/jobs")

    def test_ip_literal_denied_when_private(self):
        policy = source_network_policy({"0.0.0.0"})
        with pytest.raises(DestinationPolicyError):
            policy.check_url("http://0.0.0.0/x")

    def test_fixture_policy_permits_loopback_explicitly(self):
        clear_dns_cache()
        policy = make_test_fixture_policy("127.0.0.1")
        decision = policy.check_url("http://127.0.0.1:8391/jobs")
        assert decision.allowed
        assert "127.0.0.1" in decision.addresses

    def test_redirect_validation(self):
        clear_dns_cache()
        policy = make_test_fixture_policy("127.0.0.1")
        decision = policy.check_redirect("/jobs?page=2", "http://127.0.0.1:8391/jobs")
        assert decision.allowed

    def test_redirect_to_private_blocked_from_public(self):
        clear_dns_cache()
        policy = source_network_policy({"example.com"})
        with pytest.raises(DestinationPolicyError):
            policy.check_redirect("http://127.0.0.1:8000/x", "https://example.com/jobs")

    def test_deny_paths(self):
        clear_dns_cache()
        policy = make_test_fixture_policy("127.0.0.1")
        policy.deny_paths = ["/admin"]
        with pytest.raises(DestinationPolicyError):
            policy.check_url("http://127.0.0.1:8391/admin/secret")

    def test_dns_failure_is_policy_error(self):
        clear_dns_cache()
        policy = source_network_policy({"this-host-does-not-exist.invalid"})
        with pytest.raises(DestinationPolicyError):
            policy.check_url("https://this-host-does-not-exist.invalid/jobs")
