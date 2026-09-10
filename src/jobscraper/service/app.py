"""The local service application shell (Slice 0).

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.6/S0.7;
docs/spec/v0.3.1.3/04_security_and_authentication.md sections 5/SEC-01;
module 01 section 55 (private routes require an authenticated session).

Security model implemented here:
  * exact loopback Host validation on every request;
  * Origin validation: required-exact for browser mutations and the bootstrap
    exchange; missing-allowed (but exact when present) for reads, liveness and
    the launcher channel;
  * only ``GET /health/live`` and the public bootstrap shell ``GET /`` are
    public; every other read requires a browser session; every mutation
    additionally requires the session-bound CSRF header + cookie pair and an
    exact JSON content type;
  * strict CSP / no-store / no-CORS headers on every response;
  * request bodies are bounded on the actual received stream (not merely the
    declared Content-Length);
  * no product/job data routes exist in Slice 0.

The route set is exactly the Slice 0 plan set:

    GET  /health/live                   public; minimal {status, service_instance_id}
    GET  /                              public bootstrap shell only; no private data
    POST /__launcher/bootstrap-ticket    launcher proof -> one-time ticket
    POST /api/bootstrap                 ticket + strict Host/Origin -> session cookies
    POST /api/session/logout            session + CSRF -> revoke session
    GET  /app                           protected dashboard shell
    GET  /api/events                    protected recent events
    GET  /api/events/stream             protected SSE event stream
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles

from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.diagnostics.events import list_recent_events
from jobscraper.version import APP_VERSION, SCHEMA_VERSION
from jobscraper.web.bootstrap import (
    BootstrapTicketStore,
    build_dashboard_bootstrap_url,
    verify_launcher_proof,
)
from jobscraper.web.security import (
    MAX_BODY_BYTES,
    RoutePolicy,
    SecurityViolation,
    BoundedBodyMiddleware,
    expected_origin,
    require_content_type,
    security_headers,
    validate_host,
    validate_origin,
)
from jobscraper.web.sessions import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    SessionRegistry,
)
from jobscraper.web.sse import sse_response

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# ---------------------------------------------------------------------------
# Route classification under the security model (fail closed).
# ---------------------------------------------------------------------------

_PUBLIC = {("GET", "/health/live"), ("GET", "/")}
_LAUNCHER_CHANNEL = {("POST", "/__launcher/bootstrap-ticket")}
_BOOTSTRAP_EXCHANGE = {("POST", "/api/bootstrap")}
_MUTATIONS = {("POST", "/api/session/logout")}


def classify_request(method: str, path: str) -> RoutePolicy:
    """Classify a request under the security model (fail closed)."""
    key = (method.upper(), path)
    if key in _PUBLIC:
        return RoutePolicy(public=True)
    if key in _LAUNCHER_CHANNEL:
        return RoutePolicy(launcher_channel=True)
    if key in _BOOTSTRAP_EXCHANGE:
        # Browser-initiated but pre-session: exact Origin still required.
        return RoutePolicy(launcher_channel=True, csrf_required=False)
    if key in _MUTATIONS:
        return RoutePolicy(mutation=True, csrf_required=True)
    if method.upper() == "GET":
        return RoutePolicy()  # private read: session required
    return RoutePolicy(mutation=True, csrf_required=True)  # unknown: fail closed


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class ServiceSecurityMiddleware:
    """Pure-ASGI Host/Origin/security-header enforcement for every request."""

    def __init__(self, app, *, state: "ServiceState") -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = self.state
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        host = headers.get("host")
        if not validate_host(host, state.port, bound_host=state.config.loopback_host):
            await _send_json(send, 400, {"error": "invalid host"})
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        origin = headers.get("origin")
        policy = classify_request(method, path)

        if path == "/api/bootstrap" or policy.mutation:
            if not validate_origin(origin, expected=state.expected, allow_missing=False):
                await _send_json(send, 403, {"error": "invalid origin"})
                return
        else:
            # Reads / liveness / launcher channel: missing allowed, exact when
            # present.
            if origin is not None and not validate_origin(origin, expected=state.expected):
                await _send_json(send, 403, {"error": "invalid origin"})
                return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                raw = message.setdefault("headers", [])
                for key, value in security_headers().items():
                    raw.append((key.encode("latin-1"), value.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class ServiceState:
    """Mutable service-scoped state (sessions, tickets, instance identity)."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        port: int,
        secret: bytes,
        sessions: SessionRegistry | None = None,
        tickets: BootstrapTicketStore | None = None,
        now_fn=None,
    ) -> None:
        import threading

        self.config = config
        self.db = db
        self.port = port
        self.secret = secret
        self.instance_id = "svc-" + uuid.uuid4().hex[:12]
        self.expected = expected_origin(config.loopback_host, port)
        # A session registry always exists (S0.6 shells simply have no way to
        # create a session before the S0.7 bootstrap endpoints exist).
        self.sessions = sessions or SessionRegistry(self.instance_id, now_fn=now_fn)
        self.tickets = tickets or BootstrapTicketStore()
        # Service-lifetime wall-clock anomaly guard (§50): created by
        # create_service_app over the active service epoch; every production
        # claim runs through it (see install_slice1_routes / the driver).
        self.clock_guard = None
        self._used_nonces: set[str] = set()
        self._nonce_lock = threading.Lock()
        self.templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
        self._now_fn = now_fn

    # ------------------------------------------------------------- bootstrap
    def verify_and_issue_ticket(self, instance_id: str, nonce: str, issued_at: int, proof: str) -> str | None:
        with self._nonce_lock:
            ok = verify_launcher_proof(
                self.secret,
                instance_id,
                nonce,
                issued_at,
                proof,
                used_nonces=self._used_nonces,
                current_instance_id=self.instance_id,
                now_fn=self._now_fn,
            )
        if not ok:
            return None
        return self.tickets.issue()

    def exchange_ticket(self, ticket: str) -> tuple[str, str] | None:
        if self.sessions is None:  # pragma: no cover - defensive
            return None
        if not self.tickets.consume(ticket):
            return None
        return self.sessions.create(self.instance_id)

    # ------------------------------------------------------------- sessions
    def validate_session(self, request: Request):
        if self.sessions is None:
            return None
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        return self.sessions.validate(session_id, self.instance_id)


def create_service_app(
    config: AppConfig,
    db: Database,
    *,
    port: int,
    secret: bytes,
    sessions: SessionRegistry | None = None,
    tickets: BootstrapTicketStore | None = None,
    now_fn=None,
) -> tuple[FastAPI, ServiceState]:
    state = ServiceState(
        config,
        db,
        port=port,
        secret=secret,
        sessions=sessions,
        tickets=tickets,
        now_fn=now_fn,
    )
    # §50: the service app lives inside one service epoch. Fail closed if the
    # epoch was never opened — run_service opens it before restart recovery,
    # and any in-process service harness must model the same lifetime.
    from jobscraper.runtime.clock import (
        NoActiveServiceEpoch,
        ServiceClockGuard,
        current_service_epoch,
    )

    epoch = current_service_epoch(db.conn)
    if epoch is None:
        raise NoActiveServiceEpoch(
            "service app requires an active service epoch (§50): open it"
            " before creating the app (run_service does this before restart"
            " recovery)"
        )
    state.clock_guard = ServiceClockGuard(epoch)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = state
    # add_middleware wraps in reverse: the LAST added is OUTERMOST. The
    # bounded-body guard sits outside so every byte received is bounded even
    # before Host/Origin classification; both apply to all route handling.
    app.add_middleware(ServiceSecurityMiddleware, state=state)
    app.add_middleware(BoundedBodyMiddleware, max_bytes=MAX_BODY_BYTES)
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # ------------------------------------------------------------- deps
    def require_session(request: Request):
        session = state.validate_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="session required")
        return session

    def require_mutation(request: Request, session=Depends(require_session)):
        if state.sessions is None:  # pragma: no cover - defensive
            raise HTTPException(status_code=403, detail="sessions unavailable")
        header_token = request.headers.get(CSRF_HEADER)
        cookie_token = request.cookies.get(CSRF_COOKIE)
        if not state.sessions.validate_csrf(session, header_token, cookie_token):
            raise HTTPException(status_code=403, detail="csrf validation failed")
        try:
            require_content_type(request.headers.get("content-type"), {"application/json"})
        except SecurityViolation as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return session

    async def json_object(request: Request) -> dict:
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="json object required")
        return payload

    # ---------------------------------------------------------- liveness
    @app.get("/health/live")
    def health_live():
        # Minimal, non-sensitive liveness only (plan S0.6).
        return {"status": "ok", "service_instance_id": state.instance_id}

    # ---------------------------------------------------- launcher channel
    @app.post("/__launcher/bootstrap-ticket")
    async def launcher_bootstrap_ticket(payload: dict = Depends(json_object)):
        ticket = state.verify_and_issue_ticket(
            str(payload.get("instance_id") or ""),
            str(payload.get("nonce") or ""),
            _int_or(payload.get("issued_at")),
            str(payload.get("proof") or ""),
        )
        if ticket is None:
            return JSONResponse({"error": "proof rejected"}, status_code=403)
        return {
            "ticket": ticket,
            "dashboard_url": build_dashboard_bootstrap_url(
                config.loopback_host, port, ticket
            ),
        }

    # -------------------------------------------------- bootstrap exchange
    @app.post("/api/bootstrap")
    async def bootstrap_exchange(payload: dict = Depends(json_object)):
        exchanged = state.exchange_ticket(str(payload.get("ticket") or ""))
        if exchanged is None:
            return JSONResponse({"error": "invalid ticket"}, status_code=403)
        session_id, csrf_token = exchanged
        response = JSONResponse(
            {"csrf_token": csrf_token, "instance_id": state.instance_id}
        )
        response.set_cookie(
            SESSION_COOKIE, session_id, httponly=True, samesite="strict", path="/"
        )
        # Mirrored CSRF cookie: readable by same-origin JS, validated against
        # the session-bound token on every mutation.
        response.set_cookie(
            CSRF_COOKIE, csrf_token, httponly=False, samesite="strict", path="/"
        )
        return response

    # ------------------------------------------------------------ logout
    @app.post("/api/session/logout")
    async def session_logout(session=Depends(require_mutation)):
        state.sessions.revoke(session.session_id)
        response = JSONResponse({"status": "revoked"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return response

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def bootstrap_shell(request: Request):
        # Public bootstrap shell only: local assets + fragment exchange logic.
        # No private data, no CDN, no session state.
        return state.templates.TemplateResponse(request, "bootstrap.html", {"request": request})

    @app.get("/app", response_class=HTMLResponse)
    async def dashboard(request: Request, session=Depends(require_session)):
        context = {
            "request": request,
            "app_name": "Windows Job Scraper",
            "app_version": APP_VERSION,
            "schema_version": SCHEMA_VERSION,
            "service_instance_id": state.instance_id,
        }
        return state.templates.TemplateResponse(request, "dashboard.html", context)

    # ------------------------------------------------------------ events
    @app.get("/api/events")
    def events(limit: int = 50, session=Depends(require_session)):
        limit = max(1, min(limit, 200))
        rows = list_recent_events(state.db.conn, limit=limit)
        return {
            "events": [
                {
                    "at": r.at,
                    "level": r.level,
                    "kind": r.kind,
                    "message": r.message,
                    "data": r.data,
                }
                for r in reversed(rows)
            ]
        }

    @app.get("/api/events/stream")
    async def events_stream(session=Depends(require_session)):
        return sse_response(state.db)

    # ------------------------------------------------- Slice 1 surface (S1.10)
    from jobscraper.net.safelinks import safe_external_url

    state.templates.env.filters["safelink"] = lambda value: safe_external_url(value) or ""
    from jobscraper.service.s1_routes import install_slice1_routes

    install_slice1_routes(app, state)

    # ------------------------------------------------- Slice 2 surface (S2.3)
    from jobscraper.service.s2_routes import install_slice2_routes

    install_slice2_routes(app, state)

    return app, state


def _int_or(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
