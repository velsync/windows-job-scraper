"""S1.2 unit tests: outbound destination / SSRF policy (04 §5.1).

Proves fail-closed destination validation with an injectable resolver so the
DNS-rebinding and mixed-record cases are deterministic:

* scheme allowlist (http/https only; file:// and javascript: never);
* embedded credentials rejected;
* every resolved address checked (loopback/private/link-local/reserved/
  multicast/unspecified/IPv4-mappedspecial ranges);
* host allowlist + path policy;
* per-hop redirect validation with cap;
* explicit narrow internal grant (loopback only) for internal features;
* unresolvable host fails closed.
"""

from __future__ import annotations

import ipaddress
import pytest

from jobscraper.net.destination import (
    DestinationPolicy,
    DestinationRejected,
    InternalGrant,
    check_url,
)


def resolver(mapping: dict[str, list[str]]):
    def resolve(host: str) -> list:
        if host in mapping:
            return [ipaddress.ip_address(a) for a in mapping[host]]
        raise OSError(f"no such host {host}")

    return resolve


PUBLIC = {"jobs.example.test": ["93.184.216.34"]}
LOCALHOST = {"localhost": ["127.0.0.1"], "loop6.test": ["::1"]}


def test_public_https_destination_passes():
    d = check_url(
        "https://jobs.example.test/api/jobs", DestinationPolicy(), resolver=resolver(PUBLIC)
    )
    assert d.scheme == "https"
    assert d.host == "jobs.example.test"
    assert d.port == 443
    assert list(d.addresses) == [ipaddress.ip_address("93.184.216.34")]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",                      # never through a source import
        "javascript:alert(1)",
        "data:text/html,hi",
        "ftp://jobs.example.test/x",
        "gopher://jobs.example.test/x",
    ],
)
def test_non_http_schemes_rejected(url):
    with pytest.raises(DestinationRejected) as exc:
        check_url(url, DestinationPolicy())
    assert exc.value.reason_code == "SCHEME_FORBIDDEN"


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@jobs.example.test/x",
        "http://user@jobs.example.test/x",
        "https://jobs.example.test:x/x",  # malformed port
        "https:///path",                  # no host
        "https://exa mple.test/x",        # space in host
    ],
)
def test_malformed_or_credentialed_urls_rejected(url):
    with pytest.raises(DestinationRejected) as exc:
        check_url(url, DestinationPolicy())
    assert exc.value.reason_code in ("EMBEDDED_CREDENTIALS", "MALFORMED_URL")


@pytest.mark.parametrize(
    ("host_records", "url", "reason"),
    [
        ({"h.test": ["127.0.0.1"]}, "http://h.test/x", "LOOPBACK_ADDRESS"),
        ({"h.test": ["::1"]}, "http://h.test/x", "LOOPBACK_ADDRESS"),
        ({"h.test": ["127.0.0.1"]}, "http://127.0.0.1/x", "LOOPBACK_ADDRESS"),
        ({"h.test": ["10.1.2.3"]}, "http://h.test/x", "PRIVATE_ADDRESS"),
        ({"h.test": ["192.168.0.9"]}, "http://h.test/x", "PRIVATE_ADDRESS"),
        ({"h.test": ["172.16.5.4"]}, "http://h.test/x", "PRIVATE_ADDRESS"),
        ({"h.test": ["169.254.1.1"]}, "http://h.test/x", "LINK_LOCAL_ADDRESS"),
        ({"h.test": ["0.0.0.0"]}, "http://h.test/x", "UNSPECIFIED_ADDRESS"),
        ({"h.test": ["224.0.0.1"]}, "http://h.test/x", "MULTICAST_ADDRESS"),
        ({"h.test": ["240.0.0.1"]}, "http://h.test/x", "RESERVED_ADDRESS"),
        ({"h.test": ["192.0.2.1"]}, "http://h.test/x", "RESERVED_ADDRESS"),
        ({"h.test": ["100.64.1.1"]}, "http://h.test/x", "PRIVATE_ADDRESS"),
        # IPv4-mapped IPv6 must be treated as its IPv4 target
        ({"h.test": ["::ffff:127.0.0.1"]}, "http://h.test/x", "LOOPBACK_ADDRESS"),
        ({"h.test": ["::ffff:10.0.0.1"]}, "http://h.test/x", "PRIVATE_ADDRESS"),
        # DNS-rebinding shape: one public + one private A record -> reject
        ({"h.test": ["93.184.216.34", "10.0.0.5"]}, "http://h.test/x", "PRIVATE_ADDRESS"),
        ({"h.test": ["93.184.216.34", "127.0.0.2"]}, "http://h.test/x", "LOOPBACK_ADDRESS"),
        # IPv6 link-local / unique-local
        ({"h.test": ["fe80::1"]}, "http://h.test/x", "LINK_LOCAL_ADDRESS"),
        ({"h.test": ["fd00::1"]}, "http://h.test/x", "PRIVATE_ADDRESS"),
    ],
)
def test_denied_address_ranges_fail_closed(host_records, url, reason):
    with pytest.raises(DestinationRejected) as exc:
        check_url(url, DestinationPolicy(), resolver=resolver(host_records))
    assert exc.value.reason_code == reason


def test_unresolvable_host_fails_closed():
    with pytest.raises(DestinationRejected) as exc:
        check_url("https://nonexistent.invalid/x", DestinationPolicy())
    assert exc.value.reason_code == "RESOLUTION_FAILED"


def test_host_allowlist_enforced():
    policy = DestinationPolicy(allowed_hosts=frozenset({"jobs.example.test"}))
    with pytest.raises(DestinationRejected) as exc:
        check_url("https://other.example.test/x", policy, resolver=resolver(PUBLIC))
    assert exc.value.reason_code == "HOST_NOT_ALLOWED"


def test_path_policy_enforced():
    policy = DestinationPolicy(
        allowed_path_prefixes=("/api/",),
        denied_path_patterns=(r"/admin",),
    )
    check_url("https://jobs.example.test/api/jobs", policy, resolver=resolver(PUBLIC))
    with pytest.raises(DestinationRejected) as exc:
        check_url("https://jobs.example.test/jobs", policy, resolver=resolver(PUBLIC))
    assert exc.value.reason_code == "PATH_NOT_ALLOWED"
    with pytest.raises(DestinationRejected) as exc:
        check_url(
            "https://jobs.example.test/api/admin/panel", policy, resolver=resolver(PUBLIC)
        )
    assert exc.value.reason_code == "PATH_NOT_ALLOWED"


def test_internal_grant_authorizes_only_loopback_and_only_granted_hosts():
    grant = InternalGrant(
        purpose="fixture acceptance server", allowed_hosts=frozenset({"localhost"})
    )
    policy = DestinationPolicy(internal_grant=grant)
    # granted host on loopback: allowed
    d = check_url("http://localhost:8901/feed", policy, resolver=resolver(LOCALHOST))
    assert d.grant == "fixture acceptance server"
    # non-granted loopback host: still denied
    with pytest.raises(DestinationRejected):
        check_url("http://loop6.test:8901/feed", policy, resolver=resolver(LOCALHOST))
    # granted NAME but resolving to a public address: denied (grant is loopback-only)
    with pytest.raises(DestinationRejected):
        check_url(
            "http://localhost:8901/feed",
            DestinationPolicy(internal_grant=grant),
            resolver=resolver({"localhost": ["93.184.216.34"]}),
        )


def test_redirect_hop_validation_and_cap():
    from jobscraper.net.destination import check_redirect_hop

    policy = DestinationPolicy(max_redirects=3)
    check_redirect_hop("https://jobs.example.test/next", policy, 0, resolver=resolver(PUBLIC))
    check_redirect_hop("https://jobs.example.test/next", policy, 2, resolver=resolver(PUBLIC))
    with pytest.raises(DestinationRejected) as exc:
        check_redirect_hop("https://jobs.example.test/next", policy, 3, resolver=resolver(PUBLIC))
    assert exc.value.reason_code == "REDIRECT_TOO_MANY"
    # a redirect to a private destination is denied per hop
    with pytest.raises(DestinationRejected) as exc:
        check_redirect_hop(
            "http://10.0.0.7/x", policy, 1, resolver=resolver({"10.0.0.7": ["10.0.0.7"]})
        )
    assert exc.value.reason_code == "PRIVATE_ADDRESS"


def test_relative_redirect_target_resolved_against_current_url():
    from jobscraper.net.destination import check_redirect_hop

    policy = DestinationPolicy()
    # relative Location header value resolved against the hop's current URL
    d = check_redirect_hop(
        "/api/v2/jobs", policy, 1, resolver=resolver(PUBLIC), base_url="https://jobs.example.test/api/v1"
    )
    assert d.host == "jobs.example.test"
    assert d.path == "/api/v2/jobs"
