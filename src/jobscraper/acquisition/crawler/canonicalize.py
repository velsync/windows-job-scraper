"""Crawler URL identity wrapper around the canonical URL normalizer.

S3.5 deliberately does not introduce a second URL-normalization algorithm.
This module narrows :mod:`jobscraper.net.urlnorm` to fetchable HTTP(S) crawl
identity, preserving the raw reference while reusing the existing conservative
tracking-parameter normalization for duplicate/trap detection.
"""
from __future__ import annotations

from dataclasses import dataclass

from jobscraper.net.urlnorm import (
    TRACKING_PARAMS_VERSION,
    UrlNormalizationError,
    has_embedded_credentials,
    normalize_url,
)


@dataclass(frozen=True)
class CrawlUrl:
    raw: str
    normalized: str
    identity: str
    scheme: str
    host: str
    port: int | None
    path: str
    query: str
    tracking_params_version: int = TRACKING_PARAMS_VERSION


def crawl_url(raw: str, *, base: str | None = None) -> CrawlUrl:
    """Return the existing normalized URL as a bounded crawl identity.

    The crawler supports public HTTP(S) only. Embedded credentials are refused
    before normalization so userinfo can never be erased and accidentally
    treated as an ordinary crawl identity. Destination/DNS/redirect authority
    remains owned by ``net.destination`` at execution time.
    """
    if has_embedded_credentials(raw):
        raise ValueError("crawler URL may not contain embedded credentials")
    try:
        normalized = normalize_url(raw, base=base)
    except UrlNormalizationError as exc:
        raise ValueError(str(exc)) from exc
    if normalized.scheme not in {"http", "https"} or not normalized.host:
        raise ValueError("crawler URL must be absolute HTTP(S)")
    return CrawlUrl(
        raw=raw,
        normalized=normalized.normalized,
        identity=normalized.normalized,
        scheme=normalized.scheme,
        host=normalized.host,
        port=normalized.port,
        path=normalized.path,
        query=normalized.query,
    )


def crawl_identity(raw: str, *, base: str | None = None) -> str:
    """Normalized comparison identity for crawler frontier/trap detection."""
    return crawl_url(raw, base=base).identity


__all__ = ["CrawlUrl", "crawl_identity", "crawl_url"]
