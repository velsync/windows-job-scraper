"""Page Validity Classifier (02 §21).

Acquisition success is not equivalent to content validity. Critical
invariant:

    login page != parser failure != empty jobs list

Extraction, adaptive repair and recipe auto-promotion are prohibited on
invalid page classes (enforced downstream: the parser only ever receives
results classified valid, or handles invalid classes as typed outcomes).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum

from jobscraper.acquisition.result import ResultEnvelope


class PageClass(Enum):
    VALID_LIST = "VALID_LIST"
    VALID_JOB = "VALID_JOB"
    JS_SHELL = "JS_SHELL"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    CHALLENGE_PAGE = "CHALLENGE_PAGE"
    EMPTY = "EMPTY"
    NOT_FOUND = "NOT_FOUND"
    JOB_CLOSED = "JOB_CLOSED"
    UNEXPECTED_REDIRECT = "UNEXPECTED_REDIRECT"
    UNEXPECTED_CONTENT = "UNEXPECTED_CONTENT"
    UNKNOWN = "UNKNOWN"


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LOGIN_MARKERS = (
    b"sign in",
    b"log in",
    b"login",
    b'type="password"',
    b"forgot your password",
)
_CHALLENGE_MARKERS = (
    b"captcha",
    b"attention required",
    b"just a moment",
    b"checking your browser",
    b"verify you are human",
    b"cf-challenge",
)
_SPA_MARKERS = (b"id=\"root\"", b"id='root'", b"id=\"app\"", b"id='app'", b"<noscript")


@dataclass(frozen=True)
class PageClassification:
    state: PageClass
    evidence: dict = field(default_factory=dict)


def _visible_text_size(body: bytes) -> int:
    text = re.sub(rb"<script.*?</script>", b"", body, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(rb"<style.*?</style>", b"", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(rb"<[^>]+>", b" ", text)
    return len(text.split())


def _title(body: bytes) -> str:
    match = _TITLE_RE.search(body)
    if not match:
        return ""
    return match.group(1).decode("utf-8", "replace").strip()[:200]


def classify_page(
    result: ResultEnvelope,
    *,
    expect: str = "LIST",
    expect_host: str | None = None,
) -> PageClassification:
    """Classify a fetched result into the canonical page states."""
    evidence: dict = {
        "status_code": result.status_code,
        "content_type": result.content_type,
        "final_url": result.final_url,
        "body_bytes": len(result.body or b""),
    }
    if result.failure is not None and result.status_code is None:
        # transport-level failure: no page to classify
        evidence["failure_kind"] = result.failure.kind.value
        return PageClassification(PageClass.UNKNOWN, evidence)

    status = result.status_code or 0
    body = result.body or b""
    content_type = (result.content_type or "").lower()
    title = _title(body)
    evidence["title"] = title

    # HTTP-status-driven states first.
    if status == 429:
        return PageClassification(PageClass.RATE_LIMITED, evidence)
    if status == 401:
        return PageClassification(PageClass.LOGIN_REQUIRED, evidence)
    if status == 403:
        lowered = body.lower()
        if any(marker in lowered for marker in _CHALLENGE_MARKERS):
            return PageClassification(PageClass.CHALLENGE_PAGE, evidence)
        return PageClassification(PageClass.CHALLENGE_PAGE if "captcha" in lowered else PageClass.UNKNOWN, evidence)
    if status == 404:
        return PageClassification(PageClass.NOT_FOUND, evidence)
    if status == 410:
        return PageClassification(PageClass.JOB_CLOSED, evidence)

    if result.failure is not None:
        # A failed fetch never classifies as valid content or as an
        # authoritative EMPTY page.  A mid-chain redirect denial (or any
        # other typed failure that recorded a hop status) must not grant
        # terminal-enumeration/absence authority (§21, RUN-13): only the
        # explicit status-driven states above may classify a failed
        # envelope; everything else is UNKNOWN with typed evidence.
        evidence["failure_kind"] = result.failure.kind.value
        return PageClassification(PageClass.UNKNOWN, evidence)

    if expect_host:
        from jobscraper.net.urlnorm import normalize_url

        try:
            final = normalize_url(result.final_url)
            wanted = normalize_url(f"http://{expect_host}" if "://" not in expect_host else expect_host)
            if final.host != wanted.host or final.port != wanted.port:
                evidence["expected_host"] = expect_host
                # The destination remained within host security policy but
                # violated the expected source/content contract (§21).
                return PageClassification(PageClass.UNEXPECTED_REDIRECT, evidence)
        except Exception:  # pragma: no cover - normalization already succeeded
            pass

    if "html" in content_type:
        lowered = body.lower()
        if any(marker in lowered for marker in _LOGIN_MARKERS) and (
            "sign" in title.lower() or "log" in title.lower() or b'type="password"' in lowered
        ):
            return PageClassification(PageClass.LOGIN_REQUIRED, evidence)
        if any(marker in lowered for marker in _CHALLENGE_MARKERS):
            return PageClassification(PageClass.CHALLENGE_PAGE, evidence)
        if _visible_text_size(body) < 200 and any(
            marker in lowered for marker in _SPA_MARKERS
        ):
            return PageClassification(PageClass.JS_SHELL, evidence)
        # Valid HTML for the expected page kind.
        state = PageClass.VALID_JOB if expect == "JOB" else PageClass.VALID_LIST
        return PageClassification(state, evidence)

    if "json" in content_type or content_type == "":
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            return PageClassification(PageClass.UNEXPECTED_CONTENT, evidence)
        if isinstance(payload, dict):
            for key in ("jobs", "results", "items", "data", "positions", "postings"):
                value = payload.get(key)
                if isinstance(value, list):
                    if not value:
                        return PageClassification(PageClass.EMPTY, evidence)
                    state = PageClass.VALID_JOB if expect == "JOB" else PageClass.VALID_LIST
                    return PageClassification(state, evidence)
        if payload is None:
            return PageClassification(PageClass.EMPTY, evidence)
        state = PageClass.VALID_JOB if expect == "JOB" else PageClass.VALID_LIST
        return PageClassification(state, evidence)

    return PageClassification(PageClass.UNEXPECTED_CONTENT, evidence)


__all__ = ["PageClass", "PageClassification", "classify_page"]
