"""Slice-2 service routes (S2.3; 01 §45/§55).

Slice-1's route contract stays exact: this module owns the search surface
added by Slice 2 and gates it with the same session dependencies as
Slice-1's private reads.  ``_PUBLIC`` in the app shell is unchanged —
``GET /api/search`` is a private read like every other product read.
"""

from __future__ import annotations

from fastapi import HTTPException, Query, Request


def install_slice2_routes(app, state) -> None:
    """Register the Slice-2 API surface on the service app (read-only)."""
    from fastapi import Depends

    def conn():
        return state.db.conn

    def require_session(request: Request):
        session = state.validate_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="session required")
        return session

    # ---------------------------------------------------------- search
    @app.get("/api/search")
    def search(
        q: str = Query(..., min_length=1, max_length=200),
        listing_status: str = Query("ACTIVE", max_length=24),
        company_id: str | None = Query(None, max_length=64),
        source_id: str | None = Query(None, max_length=64),
        remote_only: bool = False,
        discovered_after: str | None = Query(None, max_length=40),
        discovered_before: str | None = Query(None, max_length=40),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10_000),
        session=Depends(require_session),
    ):
        """Search indexed jobs (session-gated, read-only; 01 §45).

        The response always reports the active search mode and warning, so a
        substring-fallback host can never be mistaken for an FTS5/BM25 host.
        """
        from jobscraper.search.capability import SEARCH_MODE_FTS5
        from jobscraper.search.query import LISTING_STATUSES, search_jobs

        if listing_status not in LISTING_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"listing_status must be one of {sorted(LISTING_STATUSES)}",
            )
        try:
            result = search_jobs(
                conn(),
                query=q,
                listing_status=listing_status,
                company_id=company_id,
                source_id=source_id,
                remote_only=remote_only,
                discovered_after=discovered_after,
                discovered_before=discovered_before,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "query": result.query,
            "mode": result.mode,
            "warning": result.warning,
            "bm25": result.mode == SEARCH_MODE_FTS5,
            "filters": result.filters,
            "total": result.total,
            "limit": result.limit,
            "offset": result.offset,
            "hits": [
                {
                    "job_id": hit.job_id,
                    "title": hit.title,
                    "company": hit.company,
                    "locations": list(hit.locations),
                    "posted_at": hit.posted_at,
                    "listing_status": hit.listing_status,
                    "score": hit.score,
                }
                for hit in result.hits
            ],
        }
