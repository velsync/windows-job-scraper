"""Outbound network destination policy (SSRF controls).

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md section 5.1,
SEC-02; VER-05.

Rules:
  * only ``http``/``https`` schemes;
  * no credentials embedded in URLs;
  * hostname is resolved *before* connection and every candidate address must
    be public (loopback/private/link-local/metadata/reserved are denied);
  * validation is bound to the actual connection (the executor connects to a
    pre-validated address, preventing DNS-rebinding TOCTOU);
  * every redirect hop is re-validated;
  * a narrowly defined internal feature (e.g. the local test fixture server)
    may authorize specific private destinations ONLY through explicit host
    policy constructed in trusted code — never from imported source/recipe
    configuration.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = {"http", "https"}

# Explicitly denied networks (in addition to non-global addresses).
DENIED_V4_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
]
DENIED_V6_NETS = [
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("::ffff:0:0/96"),
]


class DestinationPolicyError(Exception):
    """Raised when a destination violates the outbound network policy."""

    def __init__(self, reason: str, *, host: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 4:
        if any(ip in net for net in DENIED_V4_NETS):
            return False
    else:
        if any(ip in net for net in DENIED_V6_NETS):
            return False
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_link_local:
            return False
    if not ip.is_global:
        return False
    return True


def normalize_host_for_lookup(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.lower()


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(normalize_host_for_lookup(host))
        return True
    except ValueError:
        return False


_DNS_CACHE: dict[tuple[str, int], tuple[float, list[str]]] = {}
_DNS_TTL_S = 30.0


def resolve_host(host: str, port: int, *, family: int = 0) -> list[str]:
    """Resolve a hostname to address strings (cached briefly)."""
    key = (normalize_host_for_lookup(host), int(port), family)
    now = time.monotonic()
    cached = _DNS_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        infos = socket.getaddrinfo(
            normalize_host_for_lookup(host), int(port), type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise DestinationPolicyError(f"DNS resolution failed for {host!r}: {exc}", host=host) from exc
    addresses = list({info[4][0] for info in infos})
    _DNS_CACHE[key] = (now + _DNS_TTL_S, addresses)
    return addresses


def clear_dns_cache() -> None:
    _DNS_CACHE.clear()


def parse_url(url: str) -> tuple[str, str, int, str]:
    """Parse and sanity-check a URL; returns (scheme, host, port, path)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise DestinationPolicyError(f"unparseable URL: {url!r}") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise DestinationPolicyError(f"scheme {parts.scheme!r} not allowed", host=parts.hostname or None)
    if not parts.hostname:
        raise DestinationPolicyError("URL has no host")
    if parts.username or parts.password:
        raise DestinationPolicyError("credentials embedded in URL are not allowed")
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme.lower(), parts.hostname, port, path


@dataclass
class DestinationDecision:
    allowed: bool
    host: str
    port: int
    addresses: list[str] = field(default_factory=list)
    reason: str = ""


class NetworkPolicy:
    """Host-owned outbound destination policy.

    ``allow_private`` may be enabled ONLY by trusted host code for narrowly
    defined internal features (e.g. the packaged local test fixtures); it is
    never settable from imported source/recipe configuration.
    """

    def __init__(
        self,
        *,
        allow_hosts: set[str] | None = None,
        allow_private: bool = False,
        allow_paths: list[str] | None = None,
        deny_paths: list[str] | None = None,
    ) -> None:
        self.allow_hosts = {normalize_host_for_lookup(h) for h in (allow_hosts or set())}
        self.allow_private = allow_private
        self.allow_paths = allow_paths
        self.deny_paths = deny_paths or []

    def check_url(self, url: str) -> DestinationDecision:
        scheme, host, port, path = parse_url(url)
        norm_host = normalize_host_for_lookup(host)
        if self.allow_hosts and norm_host not in self.allow_hosts:
            raise DestinationPolicyError(
                f"host {norm_host!r} is outside the approved host scope", host=norm_host
            )
        self._check_path(path)
        addresses = resolve_host(norm_host, port)
        if not addresses:
            raise DestinationPolicyError(f"no addresses resolved for {norm_host!r}", host=norm_host)
        for addr in addresses:
            self._check_address(addr, host=norm_host)
        return DestinationDecision(
            allowed=True, host=norm_host, port=port, addresses=list(addresses)
        )

    def _check_path(self, path: str) -> None:
        for denied in self.deny_paths:
            if denied and path.startswith(denied):
                raise DestinationPolicyError(f"path {path!r} matches deny pattern {denied!r}")

    def _check_address(self, addr: str, *, host: str) -> None:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise DestinationPolicyError(f"invalid address {addr!r}", host=host) from exc
        if ip.is_loopback and host in {"localhost", normalize_host_for_lookup(host)} and self.allow_private:
            return
        if self.allow_private:
            # Explicitly authorized internal scope (trusted host code only).
            if not (ip.is_multicast or ip.is_unspecified or ip.is_reserved):
                return
            raise DestinationPolicyError(f"address {addr} is reserved/multicast", host=host)
        if not ip_is_public(ip):
            raise DestinationPolicyError(
                f"address {addr} for host {host!r} is not a public destination", host=host
            )

    def check_redirect(self, location: str, base_url: str) -> DestinationDecision:
        """Validate a redirect Location header against the same policy."""
        try:
            parts = urlsplit(location)
        except ValueError as exc:
            raise DestinationPolicyError(f"unparseable redirect location {location!r}") from exc
        if not parts.scheme and not parts.netloc:
            # Relative redirect: resolve against the current URL.
            base = urlsplit(base_url)
            resolved = urlunsplit((base.scheme, base.netloc, location or "/", "", ""))
            return self.check_url(resolved)
        return self.check_url(location)


def source_network_policy(allowed_hosts: set[str]) -> NetworkPolicy:
    """The production policy for source fetches: public destinations only."""
    return NetworkPolicy(allow_hosts=allowed_hosts)


def make_test_fixture_policy(host: str) -> NetworkPolicy:
    """Trusted-host-only policy permitting a local test fixture server.

    This is the 'narrowly defined internal feature' exception: constructed
    exclusively by test/host code, never influenced by source configuration.
    """
    return NetworkPolicy(allow_hosts={host}, allow_private=True)
