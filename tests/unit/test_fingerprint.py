"""S2.4 — ATS fingerprint classifier tests (02 §12.1).

The fingerprint module is deterministic, evidence-first, and operates only on
already-acquired content (bytes + URL + headers). It performs no I/O and has
no database access. Every match carries versioned evidence so a historical
classification stays reproducible (ARC-10).
"""

from __future__ import annotations

import json

import pytest

from jobscraper.adapters.fingerprint import (
    FINGERPRINT_VERSION,
    AtsFingerprint,
    classify_content,
)


# ---------------------------------------------------------------------------
# Contract basics
# ---------------------------------------------------------------------------

class TestFingerprintContract:
    """The fingerprint dataclass is versioned and immutable."""

    def test_fingerprint_version_is_positive(self):
        assert FINGERPRINT_VERSION >= 1

    def test_fingerprint_has_required_fields(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.9,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        assert fp.family == "GREENHOUSE"
        assert fp.confidence == 0.9
        assert fp.recommended_adapter_id == "greenhouse"
        assert fp.fingerprint_version == FINGERPRINT_VERSION

    def test_fingerprint_is_frozen(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.9,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        with pytest.raises(AttributeError):
            fp.family = "LEVER"

    def test_as_dict_round_trips_version_and_family(self):
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=({"kind": "HOST", "value": "boards.greenhouse.io", "strength": "STRONG"},),
            recommended_adapter_id="greenhouse",
        )
        d = fp.as_dict()
        assert d["family"] == "GREENHOUSE"
        assert d["confidence"] == 0.95
        assert d["fingerprint_version"] == FINGERPRINT_VERSION
        assert d["recommended_adapter_id"] == "greenhouse"
        assert isinstance(d["evidence"], list)


# ---------------------------------------------------------------------------
# Host/domain pattern signals
# ---------------------------------------------------------------------------

class TestHostDomainSignals:
    """Host patterns that unambiguously identify known ATS providers."""

    def test_greenhouse_hosted_board_host(self):
        fp = classify_content(
            url="https://boards.greenhouse.io/acme",
            body=b"<html><body><h1>Jobs at Acme</h1></body></html>",
            content_type="text/html",
        )
        assert fp.family == "GREENHOUSE"
        assert fp.confidence >= 0.9
        assert fp.recommended_adapter_id == "greenhouse"
        kinds = {e["kind"] for e in fp.evidence}
        assert "HOST" in kinds

    def test_greenhouse_api_host(self):
        fp = classify_content(
            url="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            body=b'{"jobs": []}',
            content_type="application/json",
        )
        assert fp.family == "GREENHOUSE"
        assert fp.confidence >= 0.9

    def test_lever_jobs_host(self):
        fp = classify_content(
            url="https://jobs.lever.co/acme",
            body=b"<html><body>Open roles</body></html>",
            content_type="text/html",
        )
        assert fp.family == "LEVER"
        assert fp.confidence >= 0.9

    def test_ashby_jobs_host(self):
        fp = classify_content(
            url="https://jobs.ashbyhq.com/acme",
            body=b"<html><body>Positions</body></html>",
            content_type="text/html",
        )
        assert fp.family == "ASHBY"
        assert fp.confidence >= 0.9

    def test_lookalike_host_is_not_trusted(self):
        """Label-boundary suffix match: greenhouse.io.evil.test is not greenhouse."""
        fp = classify_content(
            url="https://boards.greenhouse.io.evil.test/acme",
            body=b"<html><body>Fake</body></html>",
            content_type="text/html",
        )
        assert fp.family is None


# ---------------------------------------------------------------------------
# HTML marker signals
# ---------------------------------------------------------------------------

class TestHtmlMarkerSignals:
    """Known HTML markers embedded by ATS platforms in their pages."""

    def test_greenhouse_embedded_script_marker(self):
        body = (
            b'<html><head>'
            b'<script src="https://boards.greenhouse.io/embed/job_board?for=acme"></script>'
            b'</head><body></body></html>'
        )
        fp = classify_content(
            url="https://acme.com/careers",
            body=body,
            content_type="text/html",
        )
        assert fp.family == "GREENHOUSE"
        assert fp.confidence >= 0.7
        kinds = {e["kind"] for e in fp.evidence}
        assert "SCRIPT_URL" in kinds

    def test_lever_embedded_script_marker(self):
        body = (
            b'<html><head>'
            b'<script src="https://jobs.lever.co/acme.js"></script>'
            b'</head><body></body></html>'
        )
        fp = classify_content(
            url="https://acme.com/careers",
            body=body,
            content_type="text/html",
        )
        assert fp.family == "LEVER"
        assert fp.confidence >= 0.7

    def test_greenhouse_iframe_marker(self):
        body = (
            b'<html><body>'
            b'<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>'
            b'</body></html>'
        )
        fp = classify_content(
            url="https://acme.com/careers",
            body=body,
            content_type="text/html",
        )
        assert fp.family == "GREENHOUSE"
        kinds = {e["kind"] for e in fp.evidence}
        assert "IFRAME_HOST" in kinds

    def test_greenhouse_data_attribute_marker(self):
        """Greenhouse boards embed data-board-token or class attributes."""
        body = (
            b'<html><body>'
            b'<div id="grnhse_app" data-board-api="https://boards-api.greenhouse.io">'
            b'</div></body></html>'
        )
        fp = classify_content(
            url="https://acme.com/careers",
            body=body,
            content_type="text/html",
        )
        assert fp.family == "GREENHOUSE"
        assert fp.confidence >= 0.6

    def test_lever_posting_class_marker(self):
        body = (
            b'<html><body>'
            b'<div class="postings-group">'
            b'<div class="posting" data-qa="posting"></div>'
            b'</div></body></html>'
        )
        fp = classify_content(
            url="https://acme.com/careers",
            body=body,
            content_type="text/html",
        )
        assert fp.family == "LEVER"
        assert fp.confidence >= 0.6


# ---------------------------------------------------------------------------
# Canonical link / JSON-LD signals
# ---------------------------------------------------------------------------

class TestCanonicalAndJsonLdSignals:
    """Canonical links and JSON-LD containing ATS provider domains."""

    def test_canonical_link_greenhouse(self):
        body = (
            b'<html><head>'
            b'<link rel="canonical" href="https://boards.greenhouse.io/acme">'
            b'</head><body></body></html>'
        )
        fp = classify_content(
            url="https://acme.com/careers",
            body=body,
            content_type="text/html",
        )
        assert fp.family == "GREENHOUSE"
        kinds = {e["kind"] for e in fp.evidence}
        assert "CANONICAL_LINK" in kinds

    def test_json_ld_with_ats_context(self):
        payload = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "hiringOrganization": {"name": "Acme"},
            "url": "https://boards.greenhouse.io/acme/jobs/12345",
        }
        body = json.dumps(payload).encode()
        fp = classify_content(
            url="https://acme.com/careers/jobs/12345",
            body=body,
            content_type="application/json",
        )
        assert fp.family == "GREENHOUSE"
        kinds = {e["kind"] for e in fp.evidence}
        assert "JSON_LD_URL" in kinds

    def test_json_ld_lever_url(self):
        payload = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "url": "https://jobs.lever.co/acme/abc123",
        }
        body = json.dumps(payload).encode()
        fp = classify_content(
            url="https://acme.com/careers/abc123",
            body=body,
            content_type="application/ld+json",
        )
        assert fp.family == "LEVER"


# ---------------------------------------------------------------------------
# JSON API keys
# ---------------------------------------------------------------------------

class TestJsonApiSignals:
    """Known JSON key shapes that indicate specific ATS APIs."""

    def test_greenhouse_api_with_departments_key(self):
        payload = {
            "jobs": [{"id": 1, "title": "Engineer"}],
            "meta": {"total": 1},
        }
        body = json.dumps(payload).encode()
        fp = classify_content(
            url="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            body=body,
            content_type="application/json",
        )
        assert fp.family == "GREENHOUSE"
        assert fp.confidence >= 0.9

    def test_lever_postings_array_response(self):
        """Lever's postings API returns a bare array, not wrapped in a key."""
        payload = [{"id": "abc123", "text": "Engineer"}]
        body = json.dumps(payload).encode()
        fp = classify_content(
            url="https://api.lever.co/v0/postings/acme",
            body=body,
            content_type="application/json",
        )
        assert fp.family == "LEVER"
        assert fp.confidence >= 0.9

    def test_ashby_api_response(self):
        payload = {
            "status": "success",
            "data": {"jobs": [{"id": "j1", "title": "Engineer"}]},
        }
        body = json.dumps(payload).encode()
        fp = classify_content(
            url="https://api.ashbyhq.com/posting-api/job-board/acme",
            body=body,
            content_type="application/json",
        )
        assert fp.family == "ASHBY"
        assert fp.confidence >= 0.9


# ---------------------------------------------------------------------------
# Combined / corroborated signals
# ---------------------------------------------------------------------------

class TestCorroboratedSignals:
    """Multiple independent signals combine into higher confidence."""

    def test_host_plus_script_corroboration(self):
        body = (
            b'<html><head>'
            b'<script src="https://boards.greenhouse.io/embed/job_board?for=acme"></script>'
            b'</head><body></body></html>'
        )
        fp = classify_content(
            url="https://boards.greenhouse.io/acme",
            body=body,
            content_type="text/html",
        )
        assert fp.family == "GREENHOUSE"
        assert fp.confidence >= 0.95
        assert len(fp.evidence) >= 2

    def test_unknown_page_yields_no_fingerprint(self):
        fp = classify_content(
            url="https://acme.com/careers",
            body=b"<html><body>Welcome to Acme</body></html>",
            content_type="text/html",
        )
        assert fp.family is None
        assert fp.confidence < 0.5
        assert fp.recommended_adapter_id is None


# ---------------------------------------------------------------------------
# Confidence tiers
# ---------------------------------------------------------------------------

class TestConfidenceTiers:
    """Confidence must reflect signal strength, not just signal count."""

    def test_strong_host_alone_yields_high_confidence(self):
        fp = classify_content(
            url="https://boards.greenhouse.io/acme",
            body=b"<html><body>Jobs</body></html>",
            content_type="text/html",
        )
        assert fp.confidence >= 0.9

    def test_weak_script_only_yields_moderate_confidence(self):
        body = (
            b'<html><body>'
            b'<script src="https://boards.greenhouse.io/embed/job_board?for=acme"></script>'
            b'</body></html>'
        )
        fp = classify_content(
            url="https://unknown-host.example/careers",
            body=body,
            content_type="text/html",
        )
        assert 0.6 <= fp.confidence <= 0.95

    def test_empty_body_with_ats_host_still_classifies(self):
        fp = classify_content(
            url="https://boards.greenhouse.io/acme",
            body=b"",
            content_type="text/html",
        )
        assert fp.family == "GREENHOUSE"


# ---------------------------------------------------------------------------
# Edge cases and safety
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Safety and edge-case handling."""

    def test_none_body_does_not_crash(self):
        fp = classify_content(
            url="https://acme.com/careers",
            body=None,
            content_type="text/html",
        )
        assert isinstance(fp, AtsFingerprint)
        assert fp.family is None

    def test_binary_body_does_not_crash(self):
        fp = classify_content(
            url="https://acme.com/careers",
            body=b"\x00\x01\x02\xff",
            content_type="application/octet-stream",
        )
        assert isinstance(fp, AtsFingerprint)

    def test_malformed_html_does_not_crash(self):
        body = b"<html><<div><script <broken></html>"
        fp = classify_content(
            url="https://acme.com/careers",
            body=body,
            content_type="text/html",
        )
        assert isinstance(fp, AtsFingerprint)

    def test_classify_content_is_deterministic(self):
        """Same inputs always produce the same fingerprint."""
        url = "https://boards.greenhouse.io/acme"
        body = b"<html><body><h1>Jobs</h1></body></html>"
        fp1 = classify_content(url=url, body=body, content_type="text/html")
        fp2 = classify_content(url=url, body=body, content_type="text/html")
        assert fp1.family == fp2.family
        assert fp1.confidence == fp2.confidence
        assert fp1.recommended_adapter_id == fp2.recommended_adapter_id
        assert len(fp1.evidence) == len(fp2.evidence)

    def test_malformed_url_does_not_crash(self):
        fp = classify_content(
            url="not a url",
            body=b"<html></html>",
            content_type="text/html",
        )
        assert isinstance(fp, AtsFingerprint)
        assert fp.family is None

    def test_embedded_credentials_url_refused(self):
        fp = classify_content(
            url="https://user:pass@boards.greenhouse.io/acme",
            body=b"<html></html>",
            content_type="text/html",
        )
        assert fp.family is None
