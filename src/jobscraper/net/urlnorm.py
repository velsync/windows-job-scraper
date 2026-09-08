"""URL normalization for identity comparison (02 §31).

Normalize before URL-based identity comparison:

* decode HTML/JSON escapes;
* resolve relative URLs;
* lowercase scheme/host where appropriate;
* normalize safe default ports;
* remove fragments where non-semantic;
* remove only proven tracking parameters;
* preserve the raw URL.

The normalized form is for *comparison only*; callers must keep ``raw``
for storage/display (§31 "Never overwrite the discovery path just because a
better origin is found").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlsplit,
    urlunsplit,
)

# Conservative, versioned tracking-parameter removal. Only parameters whose
# sole purpose is click tracking are removed; anything semantic stays.
TRACKING_PARAMS_VERSION = 1
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "msclkid",
        "dclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "yclid",
        "_ga",
    }
)

_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_DEFAULT_PORTS = {"http": 80, "https": 443}

_HTML_ENTITY_RE = re.compile(r"&(amp|lt|gt|quot|#38|#x26);", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class UrlNormalizationError(ValueError):
    """Raised when a URL cannot be normalized into a comparable form."""


@dataclass(frozen=True)
class NormalizedUrl:
    raw: str
    normalized: str
    scheme: str
    host: str | None
    port: int | None
    path: str
    query: str
    fragment: str


def _unescape_input(value: str) -> str:
    """Decode HTML entities and JSON escapes that appear in scraped URLs."""
    out = value.strip()
    # JSON: \/ -> /
    out = out.replace("\\/", "/")
    # HTML entities that change URL structure (&amp; is the common one).
    out = _HTML_ENTITY_RE.sub(
        lambda m: {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "#38": "&", "#x26": "&"}[
            m.group(1).lower()
        ],
        out,
    )
    return out


def _normalize_path(path: str) -> str:
    """Decode unreserved percent-escapes, uppercase remaining escapes."""
    if not path:
        return "/"
    # Split into safe segments and percent-escapes.
    out = []
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == "%" and i + 2 < len(path) + 1 and re.fullmatch(r"%[0-9a-fA-F]{2}", path[i : i + 3]):
            byte = int(path[i + 1 : i + 3], 16)
            decoded = chr(byte)
            if decoded in _UNRESERVED:
                out.append(decoded)
            else:
                out.append(f"%{byte:02X}")
            i += 3
        else:
            out.append(ch)
            i += 1
    normalized = "".join(out)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _normalize_query(query: str) -> str:
    """Re-encode the query without tracking parameters, order-preserving."""
    if not query:
        return ""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        # Malformed query: keep as-is rather than inventing structure.
        return query
    kept = [
        (quote(str(k), safe=""), quote(str(v), safe=""))
        for k, v in pairs
        if str(k).lower() not in _TRACKING_PARAMS
    ]
    return "&".join(f"{k}={v}" if v else k for k, v in kept)


def _normalize_host_in_split(scheme: str, netloc: str) -> tuple[str, int | None]:
    """Return (host-for-display, port) with defaults applied/trimmed."""
    host = netloc
    port: int | None = None
    # netloc shape: [userinfo@]host[:port] where host may be bracketed IPv6.
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            raise UrlNormalizationError("unterminated IPv6 literal")
        host_part = host[: end + 1].lower()
        rest = host[end + 1 :]
    else:
        if ":" in host:
            host_part, _, maybe_port = host.rpartition(":")
            if maybe_port:
                if not maybe_port.isdigit():
                    raise UrlNormalizationError(f"invalid port {maybe_port!r}")
                port = int(maybe_port)
            host = host_part
        host_part = host.lower()
        rest = ""
    if rest.startswith(":"):
        spec = rest[1:]
        if spec:
            if not spec.isdigit():
                raise UrlNormalizationError(f"invalid port {spec!r}")
            port = int(spec)
    if not host_part or host_part in ("[]",):
        raise UrlNormalizationError("URL has no host")
    if re.search(r"[\s<>\"{}|\\^`]", host_part):
        raise UrlNormalizationError(f"invalid characters in host {host_part!r}")
    # Strip dot-segments is *not* applied: path semantics for identity are
    # compared after server-side resolution; relative resolution already
    # handled dot-segments when a base was provided.
    return host_part, port


def normalize_url(raw: str, *, base: str | None = None, drop_fragment: bool = True) -> NormalizedUrl:
    """Normalize ``raw`` (optionally relative to ``base``) per 02 §31."""
    if not isinstance(raw, str) or not raw.strip():
        raise UrlNormalizationError("empty URL")
    if _CONTROL_RE.search(raw):
        raise UrlNormalizationError("control characters in URL")
    candidate = _unescape_input(raw)
    if base:
        candidate = _resolve_relative(candidate, urlsplit(_unescape_input(base)))

    parts = urlsplit(candidate)
    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise UrlNormalizationError(f"URL has no scheme: {raw!r}")
    if scheme not in ("http", "https"):
        # Normalization is scheme-agnostic, but identity comparison in this
        # product only concerns fetchable web URLs; still produce a result
        # for non-web schemes (callers decide policy).
        pass
    host, port = _normalize_host_in_split(scheme, parts.netloc)
    default = _DEFAULT_PORTS.get(scheme)
    if port is not None and port == default:
        port = None
    path = _normalize_path(parts.path)
    query = _normalize_query(parts.query)
    fragment = "" if drop_fragment else (parts.fragment or "")

    netloc = host if port is None else f"{host}:{port}"
    normalized = urlunsplit((scheme, netloc, path, query, fragment))
    return NormalizedUrl(
        raw=raw,
        normalized=normalized,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        query=query,
        fragment=fragment,
    )


def _resolve_relative(candidate: str, base_split) -> str:
    """Resolve ``candidate`` against an already-split base URL."""
    from urllib.parse import urljoin

    base_url = urlunsplit(base_split)
    return urljoin(base_url, candidate)


def has_embedded_credentials(value: str) -> bool:
    """True when ``value`` (pre-normalization) carries userinfo."""
    try:
        parts = urlsplit(_unescape_input(value))
    except ValueError:
        return True  # unparseable -> fail closed for policy purposes
    return bool(parts.username or parts.password or "@" in (parts.netloc or ""))


def url_identity(raw: str, *, base: str | None = None) -> str:
    """The normalized URL string used for identity comparison."""
    return normalize_url(raw, base=base).normalized


__all__ = [
    "TRACKING_PARAMS_VERSION",
    "NormalizedUrl",
    "UrlNormalizationError",
    "normalize_url",
    "url_identity",
    "unquote",
]
