"""Slice-1 service routes (S1.10; 01 §55).

Every mutation passes the Slice-0 security shell: localhost
request-authentication, session + CSRF-equivalent checks, bounded JSON
bodies (04 §5). Private reads also require an authenticated session;
only the non-sensitive liveness endpoint stays public.

Runs execute synchronously and bounded (single-user local product; the
driver's stop policy caps pages/requests). The driver never holds a DB
transaction across network waits (03 §50).
"""

from __future__ import annotations

import json

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from jobscraper.applications.applylink import best_application_url
from jobscraper.applications.core import (
    ApplicationStatusError,
    create_application,
    get_application,
    list_applications,
    update_application,
)
from jobscraper.inbox.events import inbox_feed
from jobscraper.inbox.state import DispositionConflict, set_disposition
from jobscraper.net.safelinks import safe_external_url
from jobscraper.pipeline.driver import execute_run
from jobscraper.profiles.core import (
    create_profile,
    current_snapshot,
    edit_profile,
    list_profiles,
)
from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import aggregate_run, create_run
from jobscraper.version import APP_VERSION

_PROFILE_FIELDS = {
    "name",
    "keywords",
    "negative_terms",
    "eligible_countries",
    "remote_rules",
    "salary_floor",
    "min_score_inbox",
}


def _profile_payload(payload: dict) -> dict:
    snapshot = {k: payload[k] for k in _PROFILE_FIELDS if k in payload}
    if not snapshot.get("name"):
        raise HTTPException(status_code=400, detail="profile name is required")
    return snapshot


def install_slice1_routes(app, state) -> None:
    """Register the Slice-1 API surface on the service app."""
    from fastapi import Depends

    from jobscraper.web.sessions import CSRF_COOKIE, CSRF_HEADER

    def conn():
        return state.db.conn

    def require_session(request: Request):
        session = state.validate_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="session required")
        return session

    def require_mutation(request: Request, session=Depends(require_session)):
        header_token = request.headers.get(CSRF_HEADER)
        cookie_token = request.cookies.get(CSRF_COOKIE)
        if not state.sessions.validate_csrf(session, header_token, cookie_token):
            raise HTTPException(status_code=403, detail="csrf validation failed")
        return session

    # ---------------------------------------------------------- profiles
    @app.get("/api/profiles")
    def profiles_list(session=Depends(require_session)):
        rows = list_profiles(conn())
        return {
            "profiles": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "current_revision_id": r["current_revision_id"],
                    "updated_at": r["updated_at"],
                    "snapshot": current_snapshot(conn(), r["id"]),
                }
                for r in rows
            ]
        }

    @app.post("/api/profiles")
    async def profiles_create(request: Request, session=Depends(require_mutation)):
        payload = await _json_object(request)
        snapshot = _profile_payload(payload)
        profile_id, revision_id = create_profile(
            conn(), snapshot=snapshot, now=db_utc_now(conn())
        )
        return {"id": profile_id, "revision_id": revision_id}

    @app.patch("/api/profiles/{profile_id}")
    async def profiles_edit(profile_id: str, request: Request, session=Depends(require_mutation)):
        payload = await _json_object(request)
        existing = conn().execute(
            "SELECT id FROM search_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="profile not found")
        current = current_snapshot(conn(), profile_id) or {}
        merged = {**current, **_profile_payload(payload)}
        revision_id = edit_profile(conn(), profile_id, snapshot=merged, now=db_utc_now(conn()))
        return {"id": profile_id, "revision_id": revision_id}

    # -------------------------------------------------------------- runs
    @app.get("/api/runs")
    def runs_list(limit: int = 20, session=Depends(require_session)):
        limit = max(1, min(limit, 100))
        rows = conn().execute(
            "SELECT id, status, created_at, started_at, finished_at,"
            " jobs_discovered, jobs_saved, requests_total, requests_failed"
            " FROM scrape_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"runs": [dict(r) for r in rows]}

    @app.post("/api/runs")
    async def runs_create(request: Request, session=Depends(require_mutation)):
        payload = await _json_object(request)
        profile_id = payload.get("profile_id")
        if profile_id:
            exists = conn().execute(
                "SELECT id FROM search_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="profile not found")
        # viable plans: ENABLED/NORMAL sources with a viable current revision
        plan_sources = conn().execute(
            """
            SELECT s.id AS source_id, s.entry_url, r.id AS binding_revision_id,
                   r.adapter_id, r.adapter_version, r.strategy, r.execution_class,
                   r.permission_profile_id, r.permission_profile_revision
            FROM sources s
            JOIN source_adapter_bindings b ON b.source_id = s.id
            JOIN source_adapter_binding_revisions r ON r.id = b.current_revision_id
            WHERE s.desired_state = 'ENABLED' AND s.administrative_state = 'NORMAL'
              AND b.desired_state = 'ENABLED' AND b.administrative_state = 'NORMAL'
              AND r.superseded_at IS NULL
            """
        ).fetchall()
        if not plan_sources:
            raise HTTPException(status_code=409, detail="no viable sources/bindings")
        now = db_utc_now(conn())
        plans = []
        for row in plan_sources:
            plans.append(
                dict(
                    source_id=row["source_id"],
                    entry_url=row["entry_url"],
                    source_plan_group_id=f"grp-{row['source_id']}",
                    fallback_rank=0,
                    binding_id=None,  # filled below
                    binding_revision_id=row["binding_revision_id"],
                    adapter_id=row["adapter_id"],
                    adapter_version=row["adapter_version"],
                    adapter_api_version="1",
                    strategy=row["strategy"],
                    execution_class=row["execution_class"],
                    permission_profile_id=row["permission_profile_id"],
                    permission_profile_revision=row["permission_profile_revision"],
                )
            )
        # resolve binding ids for the chosen revisions
        for plan in plans:
            binding_row = conn().execute(
                "SELECT binding_id FROM source_adapter_binding_revisions WHERE id = ?",
                (plan["binding_revision_id"],),
            ).fetchone()
            plan["binding_id"] = binding_row["binding_id"]
        run_id, plan_ids = create_run(conn(), profile_id=profile_id, plans=plans, now=now)
        for plan, plan_id in zip(plans, plan_ids):
            enqueue_request(
                conn(),
                run_id=run_id,
                run_source_plan_id=plan_id,
                source_id=plan["source_id"],
                binding_id=plan["binding_id"],
                request_type="LIST_FETCH",
                target_identity=plan.get("entry_url") or f"source:{plan['source_id']}",
                logical_key='{"page": 1}',
            )
        status = execute_run(conn(), run_id)
        counts = conn().execute(
            "SELECT jobs_saved, jobs_updated, requests_total, requests_failed"
            " FROM scrape_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return {
            "run_id": run_id,
            "status": status,
            "jobs_saved": counts["jobs_saved"],
            "jobs_updated": counts["jobs_updated"],
            "requests_total": counts["requests_total"],
            "requests_failed": counts["requests_failed"],
        }

    @app.post("/api/runs/{run_id}/cancel")
    async def runs_cancel(run_id: str, session=Depends(require_mutation)):
        row = conn().execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        if row["status"] in ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"):
            # cancelling a finished run must not rewrite its durable outcome
            return {"status": row["status"]}
        request_run_cancellation(conn(), run_id, now=db_utc_now(conn()))
        # finalize now when every group is closed (e.g. an interrupted run
        # after restart recovery); a run with live in-flight work reports
        # honestly that it is still running.
        status = aggregate_run(conn(), run_id, now=db_utc_now(conn()))
        return {"status": status or "RUNNING"}

    # -------------------------------------------------------------- inbox
    @app.get("/api/inbox")
    def inbox(profile_id: str, limit: int = 100, session=Depends(require_session)):
        limit = max(1, min(limit, 200))
        rows = inbox_feed(conn(), profile_id, limit=limit)
        items = []
        for r in rows:
            score_row = conn().execute(
                "SELECT score, breakdown_json FROM job_scores WHERE job_id = ?"
                " AND profile_id = ?",
                (r["job_id"], profile_id),
            ).fetchone()
            items.append(
                {
                    "event_id": r["id"],
                    "job_id": r["job_id"],
                    "event_kind": r["event_kind"],
                    "created_at": r["created_at"],
                    "title": r["title"],
                    "listing_status": r["listing_status"],
                    "score": r["score"],
                    "eligibility": r["verdict"],
                    "disposition": r["disposition"] or "NONE",
                    "breakdown": json.loads(score_row["breakdown_json"]) if score_row else [],
                }
            )
        return {"inbox": items}

    @app.post("/api/profiles/{profile_id}/jobs/{job_id}/disposition")
    async def disposition_set(
        profile_id: str, job_id: str, request: Request, session=Depends(require_mutation)
    ):
        payload = await _json_object(request)
        disposition = payload.get("disposition")
        if disposition not in ("NONE", "SHORTLISTED", "DISMISSED", "SNOOZED", "ARCHIVED"):
            raise HTTPException(status_code=400, detail="invalid disposition")
        expected = payload.get("expected_row_revision")
        try:
            row = set_disposition(
                conn(),
                job_id=job_id,
                profile_id=profile_id,
                disposition=disposition,
                now=db_utc_now(conn()),
                reason=payload.get("reason"),
                snoozed_until=payload.get("snoozed_until"),
                expected_row_revision=int(expected) if expected is not None else None,
            )
        except DispositionConflict as exc:
            return JSONResponse({"error": "row_revision_conflict", "detail": str(exc)}, status_code=409)
        return {
            "job_id": job_id,
            "profile_id": profile_id,
            "disposition": row["disposition"],
            "row_revision": row["row_revision"],
        }

    # --------------------------------------------------------------- jobs
    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, session=Depends(require_session)):
        job = conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        presences = conn().execute(
            "SELECT * FROM job_sources WHERE job_id = ? ORDER BY created_at", (job_id,)
        ).fetchall()
        history = conn().execute(
            "SELECT at, change_class FROM job_history WHERE job_id = ? ORDER BY at DESC LIMIT 50",
            (job_id,),
        ).fetchall()
        apply_url = best_application_url(conn(), job_id)
        return {
            "job": {
                "id": job["id"],
                "title": job["title"],
                "description_md": job["description_md"],
                "listing_status": job["listing_status"],
                "salary_original_text": job["salary_original_text"],
                "salary_min": job["salary_min"],
                "salary_max": job["salary_max"],
                "salary_currency": job["salary_currency"],
                "salary_period": job["salary_period"],
                "first_seen_at": job["first_seen_at"],
                "last_seen_at": job["last_seen_at"],
            },
            # every source and application link stays inspectable (§39);
            # only safelink-approved URLs are emitted as clickable (PROD-05)
            "sources": [
                {
                    "source_id": p["source_id"],
                    "source_job_id": p["source_job_id"],
                    "presence_state": p["presence_state"],
                    "discovery_url": safe_external_url(p["discovery_url"]),
                    "canonical_job_url": safe_external_url(p["canonical_job_url"]),
                    "application_url": safe_external_url(p["application_url"]),
                    "first_seen_at": p["first_seen_at"],
                    "last_seen_at": p["last_seen_at"],
                }
                for p in presences
            ],
            "apply_url": apply_url,
            "history": [dict(h) for h in history],
        }

    # ------------------------------------------------------- applications
    @app.get("/api/applications")
    def applications_list(profile_id: str, session=Depends(require_session)):
        rows = list_applications(conn(), profile_id=profile_id)
        return {"applications": [dict(r) for r in rows]}

    @app.post("/api/applications")
    async def applications_create(request: Request, session=Depends(require_mutation)):
        payload = await _json_object(request)
        job_id = payload.get("job_id")
        profile_id = payload.get("profile_id")
        if not job_id or not profile_id:
            raise HTTPException(status_code=400, detail="job_id and profile_id are required")
        for table, key in (("jobs", job_id), ("search_profiles", profile_id)):
            if conn().execute(f"SELECT id FROM {table} WHERE id = ?", (key,)).fetchone() is None:
                raise HTTPException(status_code=404, detail=f"{table[:-1]} not found")
        try:
            row = create_application(
                conn(),
                job_id=job_id,
                profile_id=profile_id,
                now=db_utc_now(conn()),
                resume_doc_id=payload.get("resume_doc_id"),
                cover_letter_doc_id=payload.get("cover_letter_doc_id"),
                notes_md=payload.get("notes_md"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid application: {exc}") from exc
        return dict(row)

    @app.patch("/api/applications/{application_id}")
    async def applications_update(
        application_id: str, request: Request, session=Depends(require_mutation)
    ):
        payload = await _json_object(request)
        if get_application(conn(), application_id) is None:
            raise HTTPException(status_code=404, detail="application not found")
        allowed = {
            "status", "applied_at", "applied_via_url", "resume_doc_id",
            "cover_letter_doc_id", "salary_asked", "notes_md",
            "next_action_at", "next_action_text", "outcome", "outcome_reason",
        }
        kwargs = {k: v for k, v in payload.items() if k in allowed}
        try:
            row = update_application(
                conn(), application_id, now=db_utc_now(conn()), **kwargs
            )
        except ApplicationStatusError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(row)


async def _json_object(request: Request) -> dict:
    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object required")
    return payload


__all__ = ["install_slice1_routes"]
