"""Localhost request security primitives.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md section 5,
SEC-01; plan S0.5.

Loopback binding is necessary but insufficient: Host validation, Origin
validation for browser requests, strict CSP, no permissive CORS, bounded
request bodies, and CSRF-equivalent mutation protection are all required.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from jobscraper.timeutil import utc_now_s

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB default request body cap

# Strict CSP: everything is local; no remote script/style/frame origins.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)


class SecurityViolation(Exception):
    """Raised when a request violates the localhost security policy."""


@dataclass(frozen=True)
class RoutePolicy:
    """Classification of one route under the security model."""

    public: bool = False
    mutation: bool = False
    csrf_required: bool = False
    # The launcher bootstrap-ticket endpoint is authenticated by launcher
    # proof rather than a browser session (plan S0.7).
    launcher_channel: bool = False


def expected_origin(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def validate_host(host_header: str | None, bound_port: int, *, bound_host: str = "127.0.0.1") -> bool:
    """Validate the Host header against the exact loopback endpoint.

    Only the exact ``host:port`` the service actually bound is accepted.
    ``localhost`` aliases, other ports, embedded userinfo, and attacker
    domains are rejected.
    """
    if not host_header:
        return False
    # No tolerance for surrounding whitespace or case games: the Host header
    # must be the exact loopback endpoint the service bound.
    if host_header != host_header.strip():
        return False
    if host_header.startswith("[") and "]" in host_header:
        # IPv6 literal form [::1]:port
        host_part, _, port_part = host_header.partition("]")
        host_part = host_part + "]"
        port_part = port_part.lstrip(":")
        compare_host = host_part.strip("[]")
    else:
        host_part, sep, port_part = host_header.rpartition(":")
        if not sep:
            return False
        compare_host = host_part
    if "@" in host_part:
        return False  # embedded userinfo
    if compare_host != bound_host:
        return False
    try:
        return int(port_part) == int(bound_port)
    except ValueError:
        return False


def validate_origin(
    origin: str | None,
    *,
    expected: str,
    allow_missing: bool = False,
) -> bool:
    """Validate the Origin header for browser-initiated requests.

    Protected browser requests require an exact Origin match. The launcher
    channel (no browser involved) may allow a missing Origin.
    """
    if origin is None or origin == "":
        return allow_missing
    if origin.strip().lower() == "null":
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    # Exact string match against the expected same-origin value.
    return origin.rstrip("/") == expected.rstrip("/")


def security_headers(*, csp: str = CSP) -> dict[str, str]:
    """Response headers applied to every dashboard response."""
    return {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        # No CORS headers are emitted at all; this space intentionally has no
        # Access-Control-Allow-Origin.
    }


def require_content_type(content_type: str | None, allowed: set[str]) -> None:
    """Require an exact Content-Type from an allowed set for mutations."""
    if not content_type:
        raise SecurityViolation("content-type required")
    base = content_type.split(";", 1)[0].strip().lower()
    if base not in allowed:
        raise SecurityViolation(f"unsupported content-type: {base}")


def safe_external_url(url: str | None) -> str | None:
    """PROD-05: only approved schemes may be rendered as clickable links."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme in {"http", "https"} and parts.netloc:
        # Reject embedded credentials in displayed links.
        if "@" in parts.netloc:
            return None
        return url.strip()
    return None


def cookie_header_flags(*, host_only: bool = True, http_only: bool = True, same_site: str = "Strict") -> dict[str, str]:
    flags = {"Path": "/", "SameSite": same_site}
    if host_only:
        pass  # no Domain attribute == host-only cookie
    if http_only:
        flags["HttpOnly"] = ""
    return flags
