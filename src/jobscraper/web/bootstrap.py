"""Launcher proof and one-time browser bootstrap tickets.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md SEC-01
(launcher → service → dashboard bootstrap); plan S0.7 (choices 6-7).

The launcher proves itself to the service with an HMAC over
(instance_id, nonce, issued_at) keyed by the protected install secret; the
secret itself is never sent. The service returns a short-lived single-use
bootstrap ticket; only its hash is retained while pending. The launcher opens
the dashboard with the ticket in the URL *fragment*; same-origin bootstrap JS
exchanges it once via POST for a session cookie + CSRF token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

PROOF_SKEW_S = 30
TICKET_TTL_S = 60
MAX_PENDING_TICKETS = 64


class BootstrapError(Exception):
    pass


def make_launcher_proof(secret: bytes, instance_id: str, nonce: str, issued_at: int) -> str:
    """HMAC proof over the launcher channel material."""
    message = f"{instance_id}|{nonce}|{issued_at}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_launcher_proof(
    secret: bytes,
    instance_id: str,
    nonce: str,
    issued_at: int,
    proof: str,
    *,
    now: float | None = None,
    used_nonces: set[str] | None = None,
    current_instance_id: str | None = None,
) -> bool:
    """Verify a launcher proof with bounded skew and nonce replay rejection."""
    if not secret or not proof or not nonce:
        return False
    now = time.time() if now is None else now
    if abs(now - issued_at) > PROOF_SKEW_S:
        return False
    if current_instance_id is not None and instance_id != current_instance_id:
        return False
    if used_nonces is not None and nonce in used_nonces:
        return False
    expected = make_launcher_proof(secret, instance_id, nonce, issued_at)
    if not hmac.compare_digest(expected, proof):
        return False
    if used_nonces is not None:
        used_nonces.add(nonce)
        # Bounded replay cache.
        while len(used_nonces) > 4096:
            used_nonces.pop()
    return True


def hash_ticket(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


class BootstrapTicketStore:
    """Server-side store of pending one-time bootstrap tickets."""

    def __init__(self, *, now_fn=None) -> None:
        self._pending: dict[str, float] = {}
        self._now_fn = now_fn or time.monotonic

    def issue(self, *, ttl_s: int = TICKET_TTL_S) -> str:
        """Issue a fresh single-use ticket (returns the raw ticket value)."""
        ticket = secrets.token_urlsafe(32)
        now = self._now_fn()
        self._pending[hash_ticket(ticket)] = now + ttl_s
        # Prune expired + bound the store.
        for h in [h for h, exp in self._pending.items() if exp <= now]:
            del self._pending[h]
        while len(self._pending) > MAX_PENDING_TICKETS:
            oldest = min(self._pending.items(), key=lambda kv: kv[1])[0]
            del self._pending[oldest]
        return ticket

    def consume(self, ticket: str) -> bool:
        """Consume a ticket exactly once; False when missing/expired/used."""
        if not ticket:
            return False
        h = hash_ticket(ticket)
        expiry = self._pending.pop(h, None)
        if expiry is None:
            return False
        return self._now_fn() <= expiry

    def pending_count(self) -> int:
        now = self._now_fn()
        for h in [h for h, exp in self._pending.items() if exp <= now]:
            del self._pending[h]
        return len(self._pending)


def build_dashboard_bootstrap_url(host: str, port: int, ticket: str) -> str:
    """Dashboard URL with the ticket in the fragment (never query/path)."""
    return f"http://{host}:{port}/#bootstrap={ticket}"
