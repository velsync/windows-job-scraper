"""Browser session registry and CSRF tokens.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md SEC-01;
plan S0.7 (choices 8-9).

Sessions are server-side, bound to the current service-instance epoch, with
30-minute idle and 12-hour absolute lifetimes. Service restart or secret
rotation invalidates all sessions. The CSRF token is bound to the session and
mirrored in a separate non-HttpOnly cookie for same-origin JS.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Mapping

from jobscraper.timeutil import FakeClock, utc_now_s

SESSION_COOKIE = "wjs_session"
CSRF_COOKIE = "wjs_csrf"
CSRF_HEADER = "X-CSRF-Token"

IDLE_LIFETIME_S = 30 * 60
ABSOLUTE_LIFETIME_S = 12 * 60 * 60


@dataclass
class BrowserSession:
    session_id: str
    csrf_token: str
    instance_id: str
    created_at: float
    last_seen_at: float
    revoked: bool = False

    @property
    def expired_by_idle(self) -> bool:
        return False  # evaluated by the registry with its clock

    def idle_expired(self, now: float) -> bool:
        return (now - self.last_seen_at) > IDLE_LIFETIME_S

    def absolute_expired(self, now: float) -> bool:
        return (now - self.created_at) > ABSOLUTE_LIFETIME_S


class SessionRegistry:
    """In-memory session registry (service-instance scoped)."""

    def __init__(self, instance_id: str, *, now_fn=None) -> None:
        self.instance_id = instance_id
        self._sessions: dict[str, BrowserSession] = {}
        import time

        self._now_fn = now_fn or (lambda: float(time.time()))

    def _now(self) -> float:
        return self._now_fn()

    def create(self, instance_id: str) -> tuple[str, str]:
        """Create a session bound to the given service instance.

        Returns ``(session_id, csrf_token)``.
        """
        if instance_id != self.instance_id:
            raise ValueError("session registry bound to a different service instance")
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = self._now()
        self._sessions[session_id] = BrowserSession(
            session_id=session_id,
            csrf_token=csrf_token,
            instance_id=instance_id,
            created_at=now,
            last_seen_at=now,
        )
        return session_id, csrf_token

    def validate(self, session_id: str, instance_id: str) -> BrowserSession | None:
        """Validate and touch a session; None when invalid/expired/revoked."""
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None or session.revoked:
            return None
        if session.instance_id != instance_id or instance_id != self.instance_id:
            return None
        now = self._now()
        if session.idle_expired(now) or session.absolute_expired(now):
            del self._sessions[session_id]
            return None
        session.last_seen_at = now
        return session

    def revoke(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        return session is not None

    def revoke_all(self) -> int:
        count = len(self._sessions)
        self._sessions.clear()
        return count

    def validate_csrf(self, session: BrowserSession, header_token: str | None, cookie_token: str | None) -> bool:
        """CSRF validation: header token must match the session-bound token.

        The mirrored cookie token must also match; this proves same-origin
        context (a cross-site attacker can set neither consistently).
        """
        if not header_token or not cookie_token:
            return False
        return secrets.compare_digest(header_token, session.csrf_token) and secrets.compare_digest(
            cookie_token, session.csrf_token
        )

    def session_count(self) -> int:
        return len(self._sessions)
