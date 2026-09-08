"""S1.2 unit tests: URL normalization (02 §31).

Proves the normalization equivalence classes used for URL-based identity
comparison: HTML/JSON escape decoding, relative resolution, scheme/host
lowercasing, default-port trimming, fragment removal, conservative tracking-
parameter removal, raw preservation.
"""

from __future__ import annotations

import pytest

from jobscraper.net.urlnorm import (
    TRACKING_PARAMS_VERSION,
    NormalizedUrl,
    normalize_url,
    url_identity,
)


def test_raw_is_preserved_exactly():
    n = normalize_url("HTTPS://Example.COM:443/Jobs?a=1&utm_source=x#frag")
    assert n.raw == "HTTPS://Example.COM:443/Jobs?a=1&utm_source=x#frag"


def test_scheme_and_host_lowercase_default_ports_trimmed():
    assert url_identity("HTTPS://Example.COM:443/jobs") == "https://example.com/jobs"
    assert url_identity("HTTP://Example.COM:80/jobs") == "http://example.com/jobs"
    assert url_identity("https://example.com:8443/jobs") == "https://example.com:8443/jobs"


def test_equivalence_classes_collide():
    a = url_identity("HTTPS://Example.com:443/jobs#apply")
    b = url_identity("https://example.com/jobs")
    assert a == b


def test_fragment_removed_tracking_params_removed_semantic_kept():
    a = url_identity("https://example.com/jobs?utm_source=feed&id=42&fbclid=abc#x")
    b = url_identity("https://example.com/jobs?id=42")
    assert a == b
    # semantic params are never removed
    assert url_identity("https://example.com/jobs?id=1") != url_identity(
        "https://example.com/jobs?id=2"
    )


def test_tracking_param_removal_is_versioned():
    assert isinstance(TRACKING_PARAMS_VERSION, int)
    assert TRACKING_PARAMS_VERSION >= 1


def test_html_and_json_escapes_decoded():
    a = url_identity("https://example.com/jobs?x=1&amp;y=2")
    b = url_identity("https://example.com/jobs?x=1&y=2")
    assert a == b
    c = url_identity(r"https:\/\/example.com\/jobs?x=1")
    assert c == "https://example.com/jobs?x=1"


def test_relative_resolution_against_base():
    n = normalize_url("../job/9", base="https://boards.example.com/list/2026/page=2")
    assert n.scheme == "https"
    assert n.host == "boards.example.com"
    # base directory is /list/2026/; ".." collapses to /list/
    assert n.path == "/list/job/9"


def test_unreserved_percent_escapes_normalized_and_reencoded_consistently():
    a = url_identity("https://example.com/a%7Eb%2Fc?q=%7e")
    b = url_identity("https://example.com/a~b%2Fc?q=~")
    assert a == b  # unreserved decoded; reserved (%2F) preserved uppercased
    n = normalize_url("https://example.com/a%2fb")
    assert n.path == "/a%2Fb"


def test_empty_path_becomes_slash():
    assert url_identity("https://example.com") == "https://example.com/"


def test_port_and_ipv6_host_handling():
    n = normalize_url("http://[2001:DB8::1]:8080/x")
    assert n.host == "[2001:db8::1]"
    assert n.port == 8080


def test_malformed_urls_raise_typed_error():
    from jobscraper.net.urlnorm import UrlNormalizationError

    with pytest.raises(UrlNormalizationError):
        normalize_url("not a url at all")
    with pytest.raises(UrlNormalizationError):
        normalize_url("https://")  # no host


def test_normalized_url_dataclass_fields():
    n: NormalizedUrl = normalize_url(
        "HTTPS://Example.com:443/jobs?utm_campaign=c&id=7#sec"
    )
    assert n.scheme == "https"
    assert n.host == "example.com"
    assert n.port is None
    assert n.path == "/jobs"
    assert n.query == "id=7"
    assert n.fragment == ""  # dropped as non-semantic by default
