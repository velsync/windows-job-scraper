"""Integration tests for the Slice 0 service shell (S0.6) and the
authenticated bootstrap/session contract (S0.7).

Runs against the ASGI app with an isolated temporary data root. These tests
prove the plan's S0.6/S0.7 requirements:

* /health/live is public, minimal and non-sensitive;
* / is a public bootstrap shell with local assets only (no CDN);
* private reads (/app, /api/events, /api/events/stream) fail without session;
* Host validation applies before route handling;
* security headers/CSP are applied;
* launcher proof issues a one-time ticket; bootstrap exchange issues
  HttpOnly session + CSRF cookies; mutations require session+CSRF;
* the request-body bound is enforced on the actual received stream — a
  request without Content-Length cannot bypass it.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.diagnostics.events import append_event, event
from jobscraper.paths import build_app_paths
from jobscraper.service.app import create_service_app
from jobscraper.web.bootstrap import make_launcher_proof

PORT = 8431
SECRET = b"\x01" * 32
HOST = "127.0.0.1"
ORIGIN = f"http://{HOST}:{PORT}"


@pytest.fixture()
def app_state(data_root):
    config = AppConfig(data_root=data_root)
    db = Database(build_app_paths(data_root).database_file)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    app, state = create_service_app(
        config, db, port=PORT, secret=SECRET, now_fn=lambda: 1000.0
    )
    yield app, state
    db.close()


@pytest.fixture()
def client(app_state):
    app, _state = app_state
    with TestClient(app) as c:
        yield c


def _launcher_headers():
    return {"host": f"{HOST}:{PORT}", "content-type": "application/json"}


def _browser_headers():
    return {"host": f"{HOST}:{PORT}", "origin": ORIGIN}


# ---------------------------------------------------------------- liveness


def test_health_live_is_public_and_minimal(client):
    r = client.get("/health/live", headers={"host": f"{HOST}:{PORT}"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ok"
    assert "service_instance_id" in payload
    # No filesystem path, token, environment or event content.
    text = r.text
    assert data_root_free(text)
    assert "secret" not in text.lower()
    assert "token" not in text.lower()


def data_root_free(text: str) -> bool:
    import os

    for marker in (os.environ.get("LOCALAPPDATA", "LOCALAPPDATA"), "C:\\", "/home/", "data-root"):
        if marker and marker in text:
            return False
    return True


def test_bootstrap_shell_is_public_local_only(client):
    r = client.get("/", headers={"host": f"{HOST}:{PORT}"})
    assert r.status_code == 200
    html = r.text
    # Only local assets; no CDN URL anywhere.
    for forbidden in ("http://", "https://", "//unpkg", "//cdn", "jsdelivr"):
        assert forbidden not in html.replace(f"http://{HOST}", ""), f"unexpected remote reference: {forbidden}"
    assert "/static/app.js" in html
    # No private data in the public shell.
    assert "events" not in html.lower() or "Recent events" not in html
    assert "service_instance_id" not in html


# ------------------------------------------------------------------ privacy


def test_app_requires_session(client):
    r = client.get("/app", headers={"host": f"{HOST}:{PORT}", "origin": ORIGIN})
    assert r.status_code == 401


def test_events_require_session(client):
    r = client.get("/api/events", headers={"host": f"{HOST}:{PORT}", "origin": ORIGIN})
    assert r.status_code == 401


def test_sse_requires_session(client):
    r = client.get("/api/events/stream", headers={"host": f"{HOST}:{PORT}", "origin": ORIGIN})
    assert r.status_code == 401


# -------------------------------------------------------------------- host


def test_host_validation_before_route_handling(client):
    # Attacker domain -> rejected regardless of route.
    r = client.get("/health/live", headers={"host": "evil.example:8431"})
    assert r.status_code == 400
    # localhost alias -> rejected.
    r = client.get("/health/live", headers={"host": "localhost:8431"})
    assert r.status_code == 400
    # alternate port -> rejected.
    r = client.get("/health/live", headers={"host": "127.0.0.1:9999"})
    assert r.status_code == 400
    # embedded userinfo -> rejected.
    r = client.get("/health/live", headers={"host": "evil@127.0.0.1:8431"})
    assert r.status_code == 400


def test_security_headers_on_html(client):
    r = client.get("/", headers={"host": f"{HOST}:{PORT}"})
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------- launcher channel


def test_launcher_proof_issues_ticket_and_exchange_creates_session(client):
    app_state_fixture = client.app.state.service
    state = app_state_fixture
    proof = make_launcher_proof(SECRET, state.instance_id, "nonce-1", 1000)
    r = client.post(
        "/__launcher/bootstrap-ticket",
        headers=_launcher_headers(),
        json={
            "instance_id": state.instance_id,
            "nonce": "nonce-1",
            "issued_at": 1000,
            "proof": proof,
        },
    )
    assert r.status_code == 200
    ticket = r.json()["ticket"]
    assert f"#bootstrap={ticket}" in r.json()["dashboard_url"]
    assert "?" not in r.json()["dashboard_url"]

    # Exchange: strict Origin required.
    r = client.post(
        "/api/bootstrap",
        headers={**_launcher_headers(), "origin": ORIGIN},
        json={"ticket": ticket},
    )
    assert r.status_code == 200
    csrf = r.json()["csrf_token"]
    cookies = r.cookies
    assert "wjs_session" in cookies
    assert "wjs_csrf" in cookies

    # Session cookie flags.
    set_cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()

    # Private routes now work.
    r = client.get("/app", headers=_browser_headers())
    assert r.status_code == 200
    assert "Windows Job Scraper" in r.text

    r = client.get("/api/events", headers=_browser_headers())
    assert r.status_code == 200

    # Ticket is single-use.
    r = client.post(
        "/api/bootstrap",
        headers={**_launcher_headers(), "origin": ORIGIN},
        json={"ticket": ticket},
    )
    assert r.status_code == 403


def test_launcher_proof_rejects_wrong_instance(client):
    state = client.app.state.service
    proof = make_launcher_proof(SECRET, "svc-other", "nonce-2", 1000)
    r = client.post(
        "/__launcher/bootstrap-ticket",
        headers=_launcher_headers(),
        json={
            "instance_id": "svc-other",
            "nonce": "nonce-2",
            "issued_at": 1000,
            "proof": proof,
        },
    )
    assert r.status_code == 403


def test_bootstrap_exchange_requires_exact_origin(client):
    state = client.app.state.service
    proof = make_launcher_proof(SECRET, state.instance_id, "nonce-3", 1000)
    r = client.post(
        "/__launcher/bootstrap-ticket",
        headers=_launcher_headers(),
        json={
            "instance_id": state.instance_id,
            "nonce": "nonce-3",
            "issued_at": 1000,
            "proof": proof,
        },
    )
    ticket = r.json()["ticket"]
    # Hostile origin.
    r = client.post(
        "/api/bootstrap",
        headers={**_launcher_headers(), "origin": "http://evil.example"},
        json={"ticket": ticket},
    )
    assert r.status_code == 403
    # Missing origin (mutations/bootstrap require it).
    r = client.post(
        "/api/bootstrap",
        headers=_launcher_headers(),
        json={"ticket": ticket},
    )
    assert r.status_code == 403


def test_logout_requires_csrf_and_revokes(client):
    state = client.app.state.service
    proof = make_launcher_proof(SECRET, state.instance_id, "nonce-4", 1000)
    r = client.post(
        "/__launcher/bootstrap-ticket",
        headers=_launcher_headers(),
        json={
            "instance_id": state.instance_id,
            "nonce": "nonce-4",
            "issued_at": 1000,
            "proof": proof,
        },
    )
    ticket = r.json()["ticket"]
    r = client.post(
        "/api/bootstrap",
        headers={**_launcher_headers(), "origin": ORIGIN},
        json={"ticket": ticket},
    )
    csrf = r.json()["csrf_token"]

    # Logout without CSRF header -> 403.
    r = client.post(
        "/api/session/logout",
        headers={**_browser_headers(), "content-type": "application/json"},
    )
    assert r.status_code == 403

    # With session + CSRF -> revoked; subsequent private read fails.
    r = client.post(
        "/api/session/logout",
        headers={
            **_browser_headers(),
            "content-type": "application/json",
            "X-CSRF-Token": csrf,
        },
    )
    assert r.status_code == 200
    r = client.get("/app", headers=_browser_headers())
    assert r.status_code == 401


# --------------------------------------------------------------- body limit


def test_body_limit_enforced_on_declared_content_length(client):
    big = b"x" * (2 * 1024 * 1024)
    r = client.post(
        "/api/bootstrap",
        headers={**_launcher_headers(), "origin": ORIGIN, "content-type": "application/json"},
        content=big,
    )
    assert r.status_code == 413


def test_body_limit_enforced_on_actual_stream_without_content_length(app_state):
    """Regression (audit finding): the bound must apply to the actual received
    body/stream. A request that omits Content-Length (chunked transfer) or
    understates it must not be able to push an unbounded payload through."""
    import asyncio

    from jobscraper.web.security import MAX_BODY_BYTES

    app_obj, _state = app_state

    async def run_case(chunks, declared_length):
        index = {"i": 0}

        async def stream_receive():
            i = index["i"]
            if i >= len(chunks):
                return {"type": "http.disconnect"}
            index["i"] += 1
            return {
                "type": "http.request",
                "body": chunks[i],
                "more_body": i < len(chunks) - 1,
            }

        sent = {}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]

        headers = [(b"host", f"{HOST}:{PORT}".encode()), (b"origin", ORIGIN.encode())]
        if declared_length is not None:
            headers.append((b"content-length", str(declared_length).encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/api/bootstrap",
            "raw_path": b"/api/bootstrap",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "client": ("127.0.0.1", 12345),
            "server": (HOST, PORT),
            "headers": headers,
        }
        await app_obj(scope, stream_receive, send)
        return sent.get("status")

    # Case 1: no Content-Length at all, streamed chunks exceed the bound.
    chunk_size = 512 * 1024
    chunks = [b"x" * chunk_size] * (MAX_BODY_BYTES // chunk_size + 2)
    status = asyncio.run(run_case(chunks, declared_length=None))
    assert status == 413, f"chunked body without Content-Length must be bounded, got {status}"

    # Case 2: lying Content-Length understating the true body size.
    status = asyncio.run(run_case(chunks, declared_length=1))
    assert status == 413, f"understated Content-Length must still be bounded, got {status}"


def test_sse_stream_delivers_new_events(app_state):
    """The SSE generator delivers only events that arrive after connection,
    as redacted JSON payloads, then terminates cleanly when closed."""
    import asyncio

    from jobscraper.web.sse import event_stream

    app, state = app_state

    async def scenario():
        gen = event_stream(state.db)
        first = await gen.__anext__()
        assert first == b": connected\n\n"
        # A new event arrives after connection.
        append_event(
            state.db.conn,
            event("INFO", "TEST_KIND", "hello stream", data={"public": "ok"}),
        )
        chunk = await gen.__anext__()
        assert b"TEST_KIND" in chunk
        assert b"hello stream" in chunk
        assert b"data: " in chunk
        await gen.aclose()

    asyncio.run(scenario())

