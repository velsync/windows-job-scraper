"""S1.2 unit tests: safe displayed links (01 PROD-05).

`javascript:`, `data:` and other active schemes must never be emitted from
scraped source fields as clickable links; only http/https are approved.
"""

from __future__ import annotations

from jobscraper.net.safelinks import safe_external_url


def test_http_and_https_are_safe():
    assert safe_external_url("https://example.com/jobs/9") == "https://example.com/jobs/9"
    assert safe_external_url("http://example.com/apply") == "http://example.com/apply"


def test_active_schemes_are_never_clickable():
    for bad in (
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        " data:text/html,<script>x</script>",
        "data:text/html,hi",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "ftp://example.com/f",
        "//example.com/x",               # protocol-relative -> ambiguous
        "mailto:a@b.test",
        "tel:+1234",
    ):
        assert safe_external_url(bad) is None, bad


def test_garbage_and_empty_are_none():
    for bad in ("", "   ", "not a url", "http://", "https:///x", None):
        assert safe_external_url(bad) is None


def test_credentialled_urls_are_none():
    assert safe_external_url("http://user:pass@example.com/x") is None


def test_control_characters_and_whitespace_stripped_but_scheme_checked_first():
    # leading whitespace/nulls cannot smuggle a scheme past the check
    assert safe_external_url("  https://example.com/x  ") == "https://example.com/x"
    assert safe_external_url("java\tscript:alert(1)") is None
    assert safe_external_url("https://example.com/x\u0000") is None
