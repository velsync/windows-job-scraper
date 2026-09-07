"""Unit tests for sessions and bootstrap tickets (S0.7)."""

import pytest

from jobscraper.web.bootstrap import (
    BootstrapTicketStore,
    build_dashboard_bootstrap_url,
    make_launcher_proof,
    verify_launcher_proof,
)
from jobscraper.web.sessions import (
    ABSOLUTE_LIFETIME_S,
    IDLE_LIFETIME_S,
    SessionRegistry,
)


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class TestLauncherProof:
    def test_valid_proof_roundtrip(self):
        secret = b"x" * 32
        proof = make_launcher_proof(secret, "inst-1", "nonce-1", 1000)
        assert verify_launcher_proof(secret, "inst-1", "nonce-1", 1000, proof, now=1000) is True

    def test_modified_instance_rejected(self):
        secret = b"x" * 32
        proof = make_launcher_proof(secret, "inst-1", "nonce-1", 1000)
        assert verify_launcher_proof(secret, "inst-2", "nonce-1", 1000, proof, now=1000) is False

    def test_modified_nonce_rejected(self):
        secret = b"x" * 32
        proof = make_launcher_proof(secret, "inst-1", "nonce-1", 1000)
        assert verify_launcher_proof(secret, "inst-1", "nonce-2", 1000, proof, now=1000) is False

    def test_modified_timestamp_rejected(self):
        secret = b"x" * 32
        proof = make_launcher_proof(secret, "inst-1", "nonce-1", 1000)
        assert verify_launcher_proof(secret, "inst-1", "nonce-1", 1001, proof, now=1000) is False

    def test_skew_bounded(self):
        secret = b"x" * 32
        proof = make_launcher_proof(secret, "inst-1", "nonce-1", 1000)
        assert verify_launcher_proof(secret, "inst-1", "nonce-1", 1000, proof, now=1000 + 31) is False

    def test_nonce_replay_rejected(self):
        secret = b"x" * 32
        proof = make_launcher_proof(secret, "inst-1", "nonce-1", 1000)
        used = set()
        assert verify_launcher_proof(secret, "inst-1", "nonce-1", 1000, proof, now=1000, used_nonces=used) is True
        assert verify_launcher_proof(secret, "inst-1", "nonce-1", 1000, proof, now=1000, used_nonces=used) is False

    def test_wrong_secret_rejected(self):
        proof = make_launcher_proof(b"a" * 32, "inst-1", "n", 1000)
        assert verify_launcher_proof(b"b" * 32, "inst-1", "n", 1000, proof, now=1000) is False

    def test_instance_binding(self):
        secret = b"x" * 32
        proof = make_launcher_proof(secret, "inst-1", "n", 1000)
        assert (
            verify_launcher_proof(secret, "inst-1", "n", 1000, proof, now=1000, current_instance_id="inst-9")
            is False
        )


class TestBootstrapTickets:
    def test_single_use(self):
        store = BootstrapTicketStore(now_fn=FakeTime())
        ticket = store.issue()
        assert store.consume(ticket) is True
        assert store.consume(ticket) is False

    def test_expiry(self):
        clock = FakeTime()
        store = BootstrapTicketStore(now_fn=clock)
        ticket = store.issue(ttl_s=60)
        clock.advance(61)
        assert store.consume(ticket) is False

    def test_invalid_ticket_rejected(self):
        store = BootstrapTicketStore(now_fn=FakeTime())
        assert store.consume("garbage") is False
        assert store.consume("") is False

    def test_url_fragment_not_query(self):
        url = build_dashboard_bootstrap_url("127.0.0.1", 8431, "TICKET123")
        assert "#bootstrap=TICKET123" in url
        assert "?" not in url


class TestSessions:
    def test_create_and_validate(self):
        reg = SessionRegistry("inst-1", now_fn=FakeTime())
        sid, csrf = reg.create("inst-1")
        session = reg.validate(sid, "inst-1")
        assert session is not None
        assert session.csrf_token == csrf

    def test_wrong_instance_rejected(self):
        reg = SessionRegistry("inst-1", now_fn=FakeTime())
        sid, _ = reg.create("inst-1")
        assert reg.validate(sid, "inst-2") is None

    def test_unknown_session_rejected(self):
        reg = SessionRegistry("inst-1", now_fn=FakeTime())
        assert reg.validate("nope", "inst-1") is None

    def test_idle_expiry(self):
        clock = FakeTime()
        reg = SessionRegistry("inst-1", now_fn=clock)
        sid, _ = reg.create("inst-1")
        clock.advance(IDLE_LIFETIME_S + 1)
        assert reg.validate(sid, "inst-1") is None

    def test_activity_extends_idle(self):
        clock = FakeTime()
        reg = SessionRegistry("inst-1", now_fn=clock)
        sid, _ = reg.create("inst-1")
        clock.advance(IDLE_LIFETIME_S - 10)
        assert reg.validate(sid, "inst-1") is not None
        clock.advance(IDLE_LIFETIME_S - 10)
        assert reg.validate(sid, "inst-1") is not None

    def test_absolute_expiry(self):
        clock = FakeTime()
        reg = SessionRegistry("inst-1", now_fn=clock)
        sid, _ = reg.create("inst-1")
        # Keep active but exceed absolute lifetime.
        for _ in range(int(ABSOLUTE_LIFETIME_S / (IDLE_LIFETIME_S / 2)) + 2):
            clock.advance(IDLE_LIFETIME_S / 2)
            reg.validate(sid, "inst-1")
        clock.advance(10)
        assert reg.validate(sid, "inst-1") is None

    def test_revoke_all(self):
        reg = SessionRegistry("inst-1", now_fn=FakeTime())
        reg.create("inst-1")
        reg.create("inst-1")
        assert reg.session_count() == 2
        assert reg.revoke_all() == 2
        assert reg.session_count() == 0

    def test_csrf_validation(self):
        reg = SessionRegistry("inst-1", now_fn=FakeTime())
        sid, csrf = reg.create("inst-1")
        session = reg.validate(sid, "inst-1")
        assert reg.validate_csrf(session, csrf, csrf) is True
        assert reg.validate_csrf(session, "wrong", csrf) is False
        assert reg.validate_csrf(session, csrf, "wrong") is False
        assert reg.validate_csrf(session, None, None) is False
        assert reg.validate_csrf(session, csrf, "") is False
