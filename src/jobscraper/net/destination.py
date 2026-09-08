"""Fail-closed outbound destination policy (04 §5.1, SEC-02).

Every outbound acquisition destination is validated before and during the
connection:

* only supported schemes (``http``/``https``);
* no credentials embedded in URLs;
* hostname resolved before connection and *every* resolved address checked
  against denied ranges (loopback/private/link-local/reserved/multicast/
  unspecified/IPv4-mapped equivalents), so DNS responses containing a public
  and a private record fail closed;
* redirect targets validated per hop against the same policy, with a cap;
* body size and duration caps are policy data consumed by the executor;
* source allow-host/path policy;
* a narrowly defined internal feature may authorize a *loopback* destination
  only through an explicit ``InternalGrant`` unavailable to imported
  source/recipe configuration;
* fail closed when the backend cannot prove an allowed peer.

The executor (S1.4) binds validation to the actual connection by connecting
to the validated address itself, so a DNS change between check and connect
cannot redirect the connection.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from jobscraper.net.urlnorm import (
    UrlNormalizationError,
    has_embedded_credentials,
    normalize_url,
)

_DEFAULT_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class InternalGrant:
    """Explicit authorization for one internal (loopback) feature.

    Grants are constructed by host-owned code (tests, internal features) —
    never from imported source/recipe configuration (04 §5.1). A grant
    authorizes only loopback destinations for the exact granted host names.
    """

    purpose: str
    allowed_hosts: frozenset[str]


@dataclass(frozen=True)
class DestinationPolicy:
    """Outbound destination policy (04 §5.1)."""

    schemes: frozenset[str] = _DEFAULT_SCHEMES
    allowed_hosts: frozenset[str] | None = None
    allowed_path_prefixes: tuple[str, ...] = ()
    denied_path_patterns: tuple[str, ...] = ()
    max_bytes: int = 2_000_000
    timeout_s: float = 30.0
    max_redirects: int = 5
    internal_grant: InternalGrant | None = None
    purpose: str | None = None

    def for_purpose(self, purpose: str) -> "DestinationPolicy":
        return DestinationPolicy(
            schemes=self.schemes,
            allowed_hosts=self.allowed_hosts,
            allowed_path_prefixes=self.allowed_path_prefixes,
            denied_path_patterns=self.denied_path_patterns,
            max_bytes=self.max_bytes,
            timeout_s=self.timeout_s,
            max_redirects=self.max_redirects,
            internal_grant=self.internal_grant,
            purpose=purpose,
        )


@dataclass(frozen=True)
class ResolvedDestination:
    scheme: str
    host: str
    port: int
    path: str
    query: str
    addresses: tuple
    granted_purpose: str | None
    normalized_url: str

    @property
    def grant(self) -> str | None:
        return self.granted_purpose


class DestinationRejected(Exception):
    """A destination violates the outbound policy (fail-closed)."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _default_resolver(host: str) -> list:
    """Resolve ``host`` via the system resolver (real DNS)."""
    addresses: list = []
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)
        except (socket.gaierror, OSError):
            continue
        for info in infos:
            addr = info[4][0]
            try:
                parsed = ipaddress.ip_address(addr.split("%")[0])
            except ValueError:
                continue
            if parsed not in addresses:
                addresses.append(parsed)
    if not addresses:
        raise OSError(f"could not resolve {host!r}")
    return addresses


def _classify_address(addr) -> str | None:
    """Return a denial reason code for a denied address, else None.

    The IPv4-mapped form is classified first (it carries the real intent),
    and the most specific security reason wins: loopback before the coarse
    private/reserved flags, documentation ranges (TEST-NET-1, 2001:db8::/32)
    before ``is_private`` (which subsumes them on some Python versions).
    """
    mapped = None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        mapped = addr.ipv4_mapped
    for candidate in (mapped, addr):
        if candidate is None:
            continue
        if candidate.is_loopback:
            return "LOOPBACK_ADDRESS"
        if candidate.is_link_local:
            return "LINK_LOCAL_ADDRESS"
        if candidate.is_unspecified:
            return "UNSPECIFIED_ADDRESS"
        if candidate.is_multicast:
            return "MULTICAST_ADDRESS"
        if candidate.version == 4 and candidate in ipaddress.ip_network("192.0.2.0/24"):
            return "RESERVED_ADDRESS"  # TEST-NET-1 documentation range
        if candidate.version == 6 and candidate in ipaddress.ip_network("2001:db8::/32"):
            return "RESERVED_ADDRESS"  # documentation range
        if candidate.is_reserved:
            return "RESERVED_ADDRESS"
        if candidate.is_private:
            return "PRIVATE_ADDRESS"
        if candidate.version == 4 and candidate in ipaddress.ip_network("100.64.0.0/10"):
            return "PRIVATE_ADDRESS"  # shared address space (CGNAT)
    return None


def _is_loopback(addr) -> bool:
    if addr.is_loopback:
        return True
    return (
        isinstance(addr, ipaddress.IPv6Address)
        and addr.ipv4_mapped is not None
        and addr.ipv4_mapped.is_loopback
    )


def check_url(
    url: str,
    policy: DestinationPolicy,
    *,
    resolver=None,
    base_url: str | None = None,
) -> ResolvedDestination:
    """Validate one destination URL against ``policy`` (fail-closed).

    ``resolver`` is injectable for deterministic tests; production callers
    use the real system resolver.
    """
    # Scheme gate first: non-web schemes (file:, javascript:, data:, ...) are
    # forbidden before any host normalization can misreport them.
    try:
        from urllib.parse import urlsplit as _split

        peeked_scheme = (_split(url.strip()).scheme or "").lower()
    except ValueError:
        peeked_scheme = ""
    if peeked_scheme and peeked_scheme not in policy.schemes:
        raise DestinationRejected(
            "SCHEME_FORBIDDEN",
            f"scheme {peeked_scheme!r} not in {sorted(policy.schemes)}",
        )

    if has_embedded_credentials(url):
        raise DestinationRejected(
            "EMBEDDED_CREDENTIALS", "credentials embedded in URL are not permitted"
        )

    try:
        normalized = normalize_url(url, base=base_url)
    except UrlNormalizationError as exc:
        raise DestinationRejected("MALFORMED_URL", str(exc)) from exc

    if normalized.scheme not in policy.schemes:
        raise DestinationRejected(
            "SCHEME_FORBIDDEN", f"scheme {normalized.scheme!r} not in {sorted(policy.schemes)}"
        )

    host = normalized.host or ""
    if host.startswith("["):
        host_key = host[1:-1]
    else:
        host_key = host
    if not host_key:
        raise DestinationRejected("MALFORMED_URL", "URL has no host")

    # Host allowlist (exact, lowercase).
    if policy.allowed_hosts is not None and host not in policy.allowed_hosts:
        raise DestinationRejected(
            "HOST_NOT_ALLOWED", f"host {host!r} is not in the source allow-host policy"
        )

    # Path policy.
    path = normalized.path or "/"
    if policy.allowed_path_prefixes and not path.startswith(tuple(policy.allowed_path_prefixes)):
        raise DestinationRejected(
            "PATH_NOT_ALLOWED", f"path {path!r} matches no allowed prefix"
        )
    for pattern in policy.denied_path_patterns:
        if re.search(pattern, path):
            raise DestinationRejected(
                "PATH_NOT_ALLOWED", f"path {path!r} matches denied pattern {pattern!r}"
            )

    # Resolve (literal IP hosts skip DNS).
    resolve = resolver or _default_resolver
    is_literal = False
    try:
        literal = ipaddress.ip_address(host_key)
        is_literal = True
    except ValueError:
        literal = None
    try:
        addresses = [literal] if is_literal else list(resolve(host_key))
    except OSError as exc:
        raise DestinationRejected("RESOLUTION_FAILED", f"{host!r}: {exc}") from exc
    if not addresses:
        raise DestinationRejected("RESOLUTION_FAILED", f"{host!r} resolved to no addresses")

    granted_purpose: str | None = None
    grant = policy.internal_grant
    denials = [(addr, reason) for addr in addresses if (reason := _classify_address(addr))]
    if grant is not None and host in grant.allowed_hosts:
        # A grant authorizes loopback destinations only, for the exact
        # granted host. Anything else on a granted host fails closed.
        if denials and all(_is_loopback(addr) for addr, _reason in denials) and len(denials) == len(addresses):
            granted_purpose = grant.purpose
        else:
            addr, reason = denials[0] if denials else (addresses[0], "GRANT_SCOPE_EXCEEDED")
            raise DestinationRejected(
                reason if denials else "GRANT_SCOPE_EXCEEDED",
                f"granted host {host!r} resolves outside the loopback-only grant scope "
                f"({addr})",
            )
    elif denials:
        addr, reason = denials[0]
        raise DestinationRejected(
            reason,
            f"{host!r} resolves to denied address {addr} ({reason})",
        )

    port = normalized.port or (443 if normalized.scheme == "https" else 80)
    return ResolvedDestination(
        scheme=normalized.scheme,
        host=host,
        port=port,
        path=path,
        query=normalized.query,
        addresses=tuple(addresses),
        granted_purpose=granted_purpose,
        normalized_url=normalized.normalized,
    )


def check_redirect_hop(
    url: str,
    policy: DestinationPolicy,
    hop_index: int,
    *,
    resolver=None,
    base_url: str | None = None,
) -> ResolvedDestination:
    """Validate one redirect hop (each hop gets the full policy check)."""
    if hop_index >= policy.max_redirects:
        raise DestinationRejected(
            "REDIRECT_TOO_MANY",
            f"redirect hop {hop_index} exceeds cap {policy.max_redirects}",
        )
    return check_url(url, policy, resolver=resolver, base_url=base_url)


__all__ = [
    "DestinationPolicy",
    "DestinationRejected",
    "InternalGrant",
    "ResolvedDestination",
    "check_redirect_hop",
    "check_url",
]
