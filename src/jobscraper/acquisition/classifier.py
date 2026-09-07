"""Page validity classifier.

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md section 21.

Critical invariant: a login page is not a parser failure and not an empty
jobs list. Extraction/repair/promotion are prohibited on invalid classes.
Ordinary redirects are metadata, not a validity class by themselves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from jobscraper.acquisition.contracts import ClassifierEvidence, PageClass, ResultEnvelope

_LOGIN_MARKERS = (
    "sign in to continue",
    "please log in",
    "login required",
    "log in to your account",
)
_AUTH_EXPIRED_MARKERS = ("session has expired", "your session expired", "please sign in again")
_CHALLENGE_MARKERS = (
    "are you a robot",
    "verify you are human",
    "checking your browser",
    "cf-challenge",
    "captcha",
    "attention required",
)
_RATE_MARKERS = ("too many requests", "rate limit exceeded", "slow down")
_CONSENT_MARKERS = ("we use cookies", "cookie preferences", "accept all cookies")
_SPA_SHELL_MARKERS = (
    '<div id="root"></div>',
    '<div id="app"></div>',
    '<noscript>enable javascript',
    "you need to enable javascript to run this app",
)
_CLOSED_MARKERS = ("this position is no longer available", "job posting closed", "this job is closed", "no longer accepting applications")


@dataclass(frozen=True)
class Classification:
    page_class: PageClass
    evidence: ClassifierEvidence
    reason: str

    @property
    def is_valid(self) -> bool:
        return self.page_class in {PageClass.VALID_LIST, PageClass.VALID_JOB}


def _extract_title(body: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()[:200]


def _visible_markers(body_lower: str) -> dict[str, bool]:
    return {
        "login": any(m in body_lower for m in _LOGIN_MARKERS),
        "auth_expired": any(m in body_lower for m in _AUTH_EXPIRED_MARKERS),
        "challenge": any(m in body_lower for m in _CHALLENGE_MARKERS),
        "rate_limited": any(m in body_lower for m in _RATE_MARKERS),
        "consent": any(m in body_lower for m in _CONSENT_MARKERS),
        "spa_shell": any(m in body_lower for m in _SPA_SHELL_MARKERS),
        "closed": any(m in body_lower for m in _CLOSED_MARKERS),
    }


def _json_shape(body: str) -> str | None:
    """Classify JSON payload shapes: list / jobs object / other."""
    stripped = body.lstrip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return "INVALID_JSON"
    if isinstance(data, list):
        return "JSON_LIST"
    if isinstance(data, dict):
        keys = set(data.keys())
        for jobs_key in ("jobs", "postings", "data", "results", "positions", "items"):
            if jobs_key in keys:
                return "JSON_JOBS"
        if "error" in keys or "message" in keys and "jobs" not in keys:
            return "JSON_ERROR"
        return "JSON_OBJECT"
    return "JSON_OTHER"


def classify(
    result: ResultEnvelope,
    *,
    expected: str = "LIST",
    source_signatures: tuple[str, ...] = (),
) -> Classification:
    """Classify a result envelope.

    ``expected``: ``LIST`` or ``JOB`` — selects the valid class on success.
    ``source_signatures``: expected source markers used to confirm expected
    content (e.g. known JSON keys or host markers).
    """
    status = result.status_code
    content_type = (result.content_type or "").lower()
    body = (result.body or b"").decode("utf-8", errors="replace")
    body_lower = body.lower()[:200_000]
    title = _extract_title(body)
    markers = _visible_markers(body_lower)
    body_size = len(result.body or b"")

    evidence = ClassifierEvidence(
        status_code=status,
        final_url=result.final_url,
        content_type=result.content_type,
        title=title,
        body_size=body_size,
        markers=markers,
    )

    def out(page_class: PageClass, reason: str) -> Classification:
        return Classification(page_class=page_class, evidence=evidence, reason=reason)

    if result.failure_kind == "CANCELLED":
        return out(PageClass.UNKNOWN, "request cancelled")
    if status is None:
        return out(PageClass.UNKNOWN, f"transport failure: {result.failure_kind}")

    if status == 304:
        return out(PageClass.UNKNOWN, "304 handled by revalidation layer, not classification")

    if status in (401, 403):
        if markers["login"] or markers["auth_expired"]:
            return out(
                PageClass.AUTH_EXPIRED if markers["auth_expired"] else PageClass.LOGIN_REQUIRED,
                "auth markers with 401/403",
            )
        if markers["challenge"]:
            return out(PageClass.CHALLENGE_PAGE, "challenge markers with 401/403")
        return out(PageClass.BLOCKED_MARK if False else PageClass.UNEXPECTED_CONTENT, "401/403 without recognized markers")

    if status == 429:
        return out(PageClass.RATE_LIMITED, "HTTP 429")

    if status == 404:
        return out(PageClass.NOT_FOUND, "HTTP 404")

    if status >= 400 and status < 500:
        return out(PageClass.UNEXPECTED_CONTENT, f"HTTP {status}")

    if status >= 500:
        return out(PageClass.UNEXPECTED_CONTENT, f"HTTP {status}")

    # 2xx family: inspect content.
    json_shape = _json_shape(body) if "json" in content_type or body.lstrip()[:1] in "{[" else None

    if json_shape == "JSON_ERROR":
        return out(PageClass.UNEXPECTED_CONTENT, "JSON error payload")

    if json_shape in ("JSON_LIST", "JSON_JOBS"):
        # Empty recognized enumeration?
        try:
            data = json.loads(body)
            jobs = data if isinstance(data, list) else next(
                (data[k] for k in ("jobs", "postings", "data", "results", "positions", "items") if k in data),
                None,
            )
            if isinstance(jobs, list) and not jobs:
                return out(PageClass.EMPTY, "recognized empty enumeration")
        except (ValueError, RecursionError):
            pass
        return out(
            PageClass.VALID_LIST if expected == "LIST" else PageClass.VALID_JOB,
            f"recognized {json_shape}",
        )

    if json_shape == "INVALID_JSON":
        return out(PageClass.UNEXPECTED_CONTENT, "invalid JSON where JSON expected")

    # HTML-ish content.
    if markers["login"] or markers["auth_expired"]:
        return out(
            PageClass.AUTH_EXPIRED if markers["auth_expired"] else PageClass.LOGIN_REQUIRED,
            "login markers in HTML",
        )
    if markers["challenge"]:
        return out(PageClass.CHALLENGE_PAGE, "challenge markers in HTML")
    if markers["rate_limited"]:
        return out(PageClass.RATE_LIMITED, "rate-limit markers in HTML")

    if "html" in content_type or body_lower.startswith("<!doctype") or "<html" in body_lower:
        if markers["closed"] and expected == "JOB":
            return out(PageClass.JOB_CLOSED, "closed markers on job page")
        if markers["spa_shell"] and body_size < 300_000:
            # A tiny JS shell with no recognizable job content.
            has_jobs = any(sig in body_lower for sig in source_signatures) if source_signatures else False
            if not has_jobs:
                return out(PageClass.JS_SHELL, "SPA shell without recognizable content")
        if source_signatures and not any(sig in body_lower for sig in source_signatures):
            return out(PageClass.UNEXPECTED_CONTENT, "expected source signatures missing")
        if expected == "JOB":
            return out(PageClass.VALID_JOB, "HTML job page")
        return out(PageClass.VALID_LIST, "HTML list page")

    if body_size == 0:
        return out(PageClass.EMPTY, "empty body")

    return out(PageClass.UNKNOWN, "unrecognized content")
