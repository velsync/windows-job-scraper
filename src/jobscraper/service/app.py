"""The local service application: dashboard, API and run coordination.

Authority: docs/spec/v0.3.1.3/04 (SEC-01 bootstrap, SEC-03 content safety);
module 01 (product surface); plan S0.7 (launcher→service→dashboard contract).

Security model implemented here:
  * exact loopback Host validation on every request;
  * Origin validation: required-exact for browser mutations and the bootstrap
    exchange; missing-allowed (but exact when present) for reads, liveness and
    the launcher channel;
  * only ``GET /healthz`` is public; every other read requires a browser
    session; every mutation additionally requires the session-bound CSRF
    header + cookie pair and an exact JSON content type;
  * strict CSP / no-store / no-CORS headers on every response;
  * request bodies are bounded;
  * source-controlled content is always rendered HTML-escaped; only
    ``safe_external_url`` links are clickable (PROD-05).

The service owns a single background run executor (single-machine capacity);
collection runs use their own database connection so UI mutations never share
a transaction with the run thread (SQLite WAL + busy-timeout arbitration).
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from jobscraper.acquisition.registry import AdapterRegistry
from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.runtime.engine import RunEngine
from jobscraper.security.netpolicy import NetworkPolicy
from jobscraper.timeutil import utc_now_s
from jobscraper.web.bootstrap import (
    BootstrapTicketStore,
    build_dashboard_bootstrap_url,
    verify_launcher_proof,
)
from jobscraper.web.security import (
    MAX_BODY_BYTES,
    RoutePolicy,
    SecurityViolation,
    expected_origin,
    require_content_type,
    safe_external_url,
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
from jobscraper.workflow.applications import (
    TRANSITIONS,
    create_application,
    set_application_status,
)
from jobscraper.workflow.inbox import DISPOSITIONS, set_disposition

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# ---------------------------------------------------------------------------
# Route classification under the security model.
# ---------------------------------------------------------------------------

_PUBLIC = {("GET", "/healthz")}
_LAUNCHER_CHANNEL = {("POST", "/api/launcher/proof")}
_BOOTSTRAP_EXCHANGE = {("POST", "/api/bootstrap/exchange")}
# Path templates for session-protected mutations.
_MUTATION_TEMPLATES = (
    "api/profiles",
    "api/runs",
    "api/jobs/{job_id}/disposition",
    "api/jobs/{job_id}/applications",
    "api/applications/{application_id}/status",
)


def _match(template: str, path: str) -> bool:
    if template == path:
        return True
    t_parts = [p for p in template.strip("/").split("/") if p]
    r_parts = [p for p in path.strip("/").split("/") if p]
    if len(t_parts) != len(r_parts):
        return False
    return all(t.startswith("{") or t == r for t, r in zip(t_parts, r_parts))


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
    if any(_match(t, path) for t in _MUTATION_TEMPLATES) and method.upper() == "POST":
        return RoutePolicy(mutation=True, csrf_required=True)
    if method.upper() == "GET":
        return RoutePolicy()  # private read: session required
    return RoutePolicy(mutation=True, csrf_required=True)  # unknown: fail closed


class ServiceSecurityMiddleware(BaseHTTPMiddleware):
    """Host/Origin/body/headers enforcement for every request."""

    def __init__(self, app, *, state: "ServiceState") -> None:
        super().__init__(app)
        self.state = state

    async def dispatch(self, request, call_next):
        state = self.state
        host = request.headers.get("host")
        if not validate_host(host, state.port, bound_host=state.config.loopback_host):
            return JSONResponse({"error": "invalid host"}, status_code=400)

        path = request.url.path
        method = request.method
        origin = request.headers.get("origin")

        # Origin rules per policy class.
        if path == "/api/bootstrap/exchange" or classify_request(method, path).mutation:
            if not validate_origin(origin, expected=state.expected, allow_missing=False):
                return JSONResponse({"error": "invalid origin"}, status_code=403)
        else:
            # Reads / liveness / launcher channel: missing allowed, exact when present.
            if origin is not None and not validate_origin(origin, expected=state.expected):
                return JSONResponse({"error": "invalid origin"}, status_code=403)

        # Bounded bodies.
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > MAX_BODY_BYTES:
                    return JSONResponse({"error": "body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "invalid content-length"}, status_code=400)

        response = await call_next(request)
        for key, value in security_headers().items():
            response.headers[key] = value
        return response


class ServiceState:
    """Mutable service-scoped state (sessions, tickets, run executor)."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        port: int,
        secret: bytes,
        policy_factory=None,
    ) -> None:
        self.config = config
        self.db = db
        self.port = port
        self.secret = secret
        self.instance_id = "svc-" + uuid.uuid4().hex[:12]
        self.expected = expected_origin(config.loopback_host, port)
        self.sessions = SessionRegistry(self.instance_id)
        self.tickets = BootstrapTicketStore()
        self._used_nonces: set[str] = set()
        self._nonce_lock = threading.Lock()
        # Separate connection for collection runs: WAL + busy-timeout keep the
        # UI connection and the run connection independent.
        self.run_db = Database(db.path)
        self.registry = AdapterRegistry()
        self.engine = RunEngine(
            config,
            self.run_db,
            self.registry,
            policy_factory=policy_factory or (lambda ctx: NetworkPolicy()),
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wjs-run")
        self.templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))

    # ------------------------------------------------------------- runs
    def start_run(self, run_id: str) -> None:
        self._executor.submit(self._execute_run_safely, run_id)

    def _execute_run_safely(self, run_id: str) -> None:
        try:
            self.engine.execute_run(run_id)
        except Exception:  # pragma: no cover - defensive
            pass

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)
        self.run_db.close()

    # --------------------------------------------------------- bootstrap
    def verify_and_issue_ticket(
        self, instance_id: str, nonce: str, issued_at: int, proof: str
    ) -> str | None:
        with self._nonce_lock:
            ok = verify_launcher_proof(
                self.secret,
                instance_id,
                nonce,
                issued_at,
                proof,
                used_nonces=self._used_nonces,
                current_instance_id=self.instance_id,
            )
        if not ok:
            return None
        return self.tickets.issue()

    def exchange_ticket(self, ticket: str) -> tuple[str, str] | None:
        if not self.tickets.consume(ticket):
            return None
        return self.sessions.create(self.instance_id)


# ---------------------------------------------------------------------------
# Read-model helpers (all HTML-escaped by Jinja autoescape).
# ---------------------------------------------------------------------------

def _job_provenance(db: Database, job_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT js.*, s.display_name AS source_name FROM job_sources js"
        " LEFT JOIN sources s ON s.id = js.source_id"
        " WHERE js.job_id=? ORDER BY js.source_rank",
        (job_id,),
    )
    out = []
    for r in rows:
        out.append(
            {
                "source_name": r["source_name"] or r["source_id"],
                "source_job_id": r["source_job_id"],
                "presence_state": r["presence_state"],
                "first_seen_at": r["first_seen_at"],
                "last_seen_at": r["last_seen_at"],
                "canonical_url": safe_external_url(r["canonical_job_url"]),
                "application_url": safe_external_url(r["application_url"]),
                "discovery_url": safe_external_url(r["discovery_url"]),
            }
        )
    return out


def _job_locations(db: Database, job_id: str) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM job_locations WHERE job_id=?", (job_id,))]


def _job_facts(db: Database, job_id: str) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM job_facts WHERE job_id=?", (job_id,))]


def _job_score(db: Database, job_id: str, profile_id: str | None):
    if not profile_id:
        return None
    row = db.query_one(
        "SELECT * FROM job_scores WHERE job_id=? AND profile_id=?", (job_id, profile_id)
    )
    if row is None:
        return None
    return {"score": row["score"], "breakdown": json.loads(row["breakdown_json"] or "[]")}


def _job_eligibility(db: Database, job_id: str, profile_id: str | None):
    if not profile_id:
        return None
    row = db.query_one(
        "SELECT verdict, reason_codes_json FROM job_eligibility WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    )
    if row is None:
        return None
    return {"verdict": row["verdict"], "reasons": json.loads(row["reason_codes_json"] or "[]")}


def _profile_state(db: Database, job_id: str, profile_id: str | None):
    if not profile_id:
        return None
    row = db.query_one(
        "SELECT * FROM job_profile_state WHERE job_id=? AND profile_id=?", (job_id, profile_id)
    )
    return dict(row) if row else None


def _applications_view(db: Database, job_id: str | None = None, profile_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT a.*, j.title AS job_title FROM applications a"
        " LEFT JOIN jobs j ON j.id = a.job_id WHERE 1=1"
    )
    params: list = []
    if job_id:
        sql += " AND a.job_id=?"
        params.append(job_id)
    if profile_id:
        sql += " AND a.profile_id=?"
        params.append(profile_id)
    sql += " ORDER BY a.updated_at DESC LIMIT 200"
    return [dict(r) for r in db.query(sql, params)]


def _selected_profile(db: Database, request_profile_id: str | None):
    profiles = [dict(p) for p in _list_profiles(db)]
    if not profiles:
        return None, profiles
    for p in profiles:
        if p["id"] == request_profile_id:
            return p, profiles
    return profiles[0], profiles


def _list_profiles(db: Database):
    from jobscraper.domain.profiles import list_profiles

    return list_profiles(db)


def _recent_runs(db: Database, limit: int = 5) -> list[dict]:
    return [
        dict(r)
        for r in db.query(
            "SELECT id, status, run_kind, created_at, finished_at FROM scrape_runs"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    ]


def _all_source_ids(db: Database) -> list[str]:
    return [r["id"] for r in db.query("SELECT id FROM sources ORDER BY created_at")]


def _snooze_until(now: str, days: int = 7) -> str:
    from jobscraper.timeutil import add_seconds

    return add_seconds(now, days * 86400)


# ---------------------------------------------------------------------------
# App factory.
# ---------------------------------------------------------------------------

def create_service_app(
    config: AppConfig,
    db: Database,
    *,
    port: int,
    secret: bytes,
    policy_factory=None,
) -> tuple[FastAPI, ServiceState]:
    state = ServiceState(config, db, port=port, secret=secret, policy_factory=policy_factory)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = state
    app.add_middleware(ServiceSecurityMiddleware, state=state)
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # ------------------------------------------------------------- deps
    def require_session(request: Request):
        session_id = request.cookies.get(SESSION_COOKIE)
        session = state.sessions.validate(session_id, state.instance_id) if session_id else None
        if session is None:
            raise HTTPException(status_code=401, detail="session required")
        return session

    def require_mutation(request: Request, session=Depends(require_session)):
        header_token = request.headers.get(CSRF_HEADER)
        cookie_token = request.cookies.get(CSRF_COOKIE)
        if not state.sessions.validate_csrf(session, header_token, cookie_token):
            raise HTTPException(status_code=403, detail="csrf validation failed")
        try:
            require_content_type(request.headers.get("content-type"), {"application/json"})
        except SecurityViolation as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return session

    async def json_body(request: Request) -> dict:
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="json object required")
        return payload

    # ---------------------------------------------------------- liveness
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # ---------------------------------------------------- launcher channel
    @app.post("/api/launcher/proof")
    async def launcher_proof(request: Request):
        payload = await json_body(request)
        ticket = state.verify_and_issue_ticket(
            payload.get("instance_id") or "",
            payload.get("nonce") or "",
            int(payload.get("issued_at") or 0),
            payload.get("proof") or "",
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
    @app.post("/api/bootstrap/exchange")
    async def bootstrap_exchange(request: Request):
        payload = await json_body(request)
        exchanged = state.exchange_ticket(payload.get("ticket") or "")
        if exchanged is None:
            return JSONResponse({"error": "invalid ticket"}, status_code=403)
        session_id, csrf_token = exchanged
        response = JSONResponse({"csrf_token": csrf_token, "instance_id": state.instance_id})
        response.set_cookie(
            SESSION_COOKIE, session_id, httponly=True, samesite="strict", path="/"
        )
        # Mirrored CSRF cookie: readable by same-origin JS, validated against
        # the session-bound token on every mutation.
        response.set_cookie(
            CSRF_COOKIE, csrf_token, httponly=False, samesite="strict", path="/"
        )
        return response

    # ------------------------------------------------------------ pages
    def _base_context(request: Request, profile_id: str | None):
        profile, profiles = _selected_profile(db, profile_id)
        return {
            "request": request,
            "profiles": profiles,
            "profile": profile,
            "profile_id": profile["id"] if profile else "",
            "recent_runs": _recent_runs(db),
            "dispositions": DISPOSITIONS,
        }

    @app.get("/", response_class=None)
    async def dashboard(request: Request, profile_id: str | None = None, session=Depends(require_session)):
        from jobscraper.workflow.inbox import inbox_queue

        context = _base_context(request, profile_id)
        rows = (
            [dict(r) for r in inbox_queue(db, context["profile_id"], limit=100)]
            if context["profile_id"]
            else []
        )
        for row in rows:
            state_row = _profile_state(db, row["id"], context["profile_id"])
            row["row_revision"] = state_row["row_revision"] if state_row else 1
        context["inbox"] = rows
        return state.templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/jobs/{job_id}")
    async def job_detail(
        request: Request, job_id: str, profile_id: str | None = None,
        session=Depends(require_session),
    ):
        job = db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        company = db.query_one("SELECT * FROM companies WHERE id=?", (job["company_id"],))
        context = _base_context(request, profile_id)
        applications = _applications_view(db, job_id=job_id)
        for app_row in applications:
            app_row["allowed_next"] = sorted(TRANSITIONS.get(app_row["status"], frozenset()))
        context.update(
            {
                "job": dict(job),
                "company": dict(company) if company else None,
                "locations": _job_locations(db, job_id),
                "facts": _job_facts(db, job_id),
                "provenance": _job_provenance(db, job_id),
                "score": _job_score(db, job_id, context["profile_id"]),
                "eligibility": _job_eligibility(db, job_id, context["profile_id"]),
                "job_state": _profile_state(db, job_id, context["profile_id"]),
                "applications": applications,
            }
        )
        return state.templates.TemplateResponse(request, "job_detail.html", context)

    @app.get("/applications")
    async def applications_page(
        request: Request, profile_id: str | None = None, session=Depends(require_session),
    ):
        context = _base_context(request, profile_id)
        rows = _applications_view(db, profile_id=context["profile_id"])
        for row in rows:
            row["allowed_next"] = sorted(TRANSITIONS.get(row["status"], frozenset()))
        context["applications"] = rows
        return state.templates.TemplateResponse(request, "applications.html", context)

    @app.get("/profiles")
    async def profiles_page(request: Request, session=Depends(require_session)):
        context = _base_context(request, None)
        return state.templates.TemplateResponse(request, "profiles.html", context)

    # ------------------------------------------------------- JSON reads
    @app.get("/api/inbox")
    async def api_inbox(profile_id: str, session=Depends(require_session)):
        from jobscraper.workflow.inbox import inbox_queue

        return {"items": [dict(r) for r in inbox_queue(db, profile_id, limit=100)]}

    @app.get("/api/runs/{run_id}")
    async def api_run(run_id: str, session=Depends(require_session)):
        row = db.query_one(
            "SELECT id, status, run_kind, profile_id, created_at, started_at, finished_at,"
            " requests_total, requests_failed FROM scrape_runs WHERE id=?",
            (run_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return dict(row)

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: str, profile_id: str | None = None, session=Depends(require_session)):
        job = db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {
            "job": dict(job),
            "locations": _job_locations(db, job_id),
            "provenance": _job_provenance(db, job_id),
            "score": _job_score(db, job_id, profile_id),
            "eligibility": _job_eligibility(db, job_id, profile_id),
        }

    # -------------------------------------------------------- mutations
    @app.post("/api/profiles")
    async def api_create_profile(request: Request, session=Depends(require_mutation)):
        from jobscraper.domain.profiles import create_profile

        payload = await json_body(request)
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        overrides: dict[str, Any] = {}
        if payload.get("home_country"):
            overrides["home_country"] = str(payload["home_country"]).strip().upper()
        if payload.get("eligible_countries"):
            raw = payload["eligible_countries"]
            if isinstance(raw, str):
                raw = raw.split(",")
            overrides["eligible_countries"] = [str(c).strip().upper() for c in raw if str(c).strip()]
        for field in ("keywords", "must_keywords", "should_keywords", "must_not_keywords"):
            raw = payload.get(field)
            if raw:
                if isinstance(raw, str):
                    raw = raw.split(",")
                overrides[field] = [str(k).strip().lower() for k in raw if str(k).strip()]
        if payload.get("min_score_inbox") is not None:
            try:
                overrides["min_score_inbox"] = float(payload["min_score_inbox"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="min_score_inbox must be a number")
        profile_id = create_profile(db, name=name, snapshot_overrides=overrides)
        return {"ok": True, "profile_id": profile_id}

    @app.post("/api/runs")
    async def api_start_run(request: Request, session=Depends(require_mutation)):
        payload = await json_body(request)
        profile_id = payload.get("profile_id") or None
        source_ids = _all_source_ids(db)
        if not source_ids:
            raise HTTPException(status_code=400, detail="no sources configured")
        run_id = state.engine.create_run(profile_id=profile_id, source_ids=source_ids)
        state.start_run(run_id)
        return {"ok": True, "run_id": run_id}

    @app.post("/api/jobs/{job_id}/disposition")
    async def api_disposition(job_id: str, request: Request, session=Depends(require_mutation)):
        payload = await json_body(request)
        profile_id = payload.get("profile_id") or ""
        disposition = payload.get("disposition") or ""
        if disposition not in DISPOSITIONS:
            raise HTTPException(status_code=400, detail=f"invalid disposition {disposition}")
        if not profile_id:
            raise HTTPException(status_code=400, detail="profile_id required")
        snoozed_until = payload.get("snoozed_until")
        if disposition == "SNOOZED" and not snoozed_until:
            snoozed_until = _snooze_until(utc_now_s())
        try:
            disposition_out, revision = set_disposition(
                db,
                job_id=job_id,
                profile_id=profile_id,
                disposition=disposition,
                snoozed_until=snoozed_until,
                expected_row_revision=payload.get("expected_row_revision"),
            )
        except Exception as exc:
            if type(exc).__name__ == "StaleRowRevision":
                raise HTTPException(status_code=409, detail="stale row revision") from exc
            raise
        return {"ok": True, "disposition": disposition_out, "row_revision": revision}

    @app.post("/api/jobs/{job_id}/applications")
    async def api_create_application(job_id: str, request: Request, session=Depends(require_mutation)):
        payload = await json_body(request)
        if db.query_one("SELECT id FROM jobs WHERE id=?", (job_id,)) is None:
            raise HTTPException(status_code=404, detail="job not found")
        try:
            application_id = create_application(
                db,
                job_id=job_id,
                profile_id=payload.get("profile_id") or None,
                applied_via_url=payload.get("applied_via_url") or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "application_id": application_id}

    @app.post("/api/applications/{application_id}/status")
    async def api_application_status(
        application_id: str, request: Request, session=Depends(require_mutation),
    ):
        payload = await json_body(request)
        status = payload.get("status") or ""
        try:
            new_status, revision = set_application_status(
                db,
                application_id=application_id,
                status=status,
                expected_row_revision=payload.get("expected_row_revision"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="application not found") from exc
        except AppStaleRevision as exc:
            raise HTTPException(status_code=409, detail="stale row revision") from exc
        return {"ok": True, "status": new_status, "row_revision": revision}

    return app, state
