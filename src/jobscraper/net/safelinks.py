"""Safe displayed links (01 PROD-05).

Only approved URL schemes (http, https) may be rendered as clickable
external links. ``javascript:``, ``data:`` and other active schemes MUST
NOT be emitted from scraped source fields as clickable job/application
links. The check never raises: untrustworthy input yields ``None``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from jobscraper.net.urlnorm import (
    UrlNormalizationError,
    has_embedded_credentials,
    normalize_url,
)

_APPROVED_SCHEMES = frozenset({"http", "https"})


def safe_external_url(value: str | None) -> str | None:
    """Return the URL if it is safe to render as a clickable external link."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "\t" in candidate or "\n" in candidate or "\r" in candidate or "\x00" in candidate:
        # A tab/newline anywhere can smuggle headers or split schemes; a
        # stripped variant is not trusted either (fail closed).
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return None
    if has_embedded_credentials(candidate):
        return None
    try:
        normalized = normalize_url(candidate)
    except UrlNormalizationError:
        return None
    if normalized.scheme not in _APPROVED_SCHEMES:
        return None
    if not normalized.host:
        return None
    parts = urlsplit(normalized.normalized)
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        return None
    if normalized.normalized != normalized.normalized.strip():
        return None
    return normalized.normalized


__all__ = ["safe_external_url"]
