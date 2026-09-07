"""Server-sent events stream for the protected event log.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.6
(``GET /api/events/stream`` protected hook/SSE); module 05 section 46.

The stream is private: the route requires an authenticated browser session
(enforced by the owning route in ``service.app``). Blocking SQLite reads are
dispatched to a worker thread so the event loop is never blocked (module 03
section 50 recommended connection model).
"""

from __future__ import annotations

import asyncio
import json

from fastapi.responses import StreamingResponse

from jobscraper.db.connection import Database
from jobscraper.diagnostics.events import list_recent_events

POLL_INTERVAL_S = 1.0
MAX_STREAM_EVENTS = 200
# Bounded stream lifetime; EventSource clients reconnect automatically, so a
# long-lived stream is never required (and a leaked one cannot pin resources).
MAX_STREAM_DURATION_S = 900.0


def _format_sse(event: dict) -> str:
    payload = json.dumps(event, default=str)
    return f"data: {payload}\n\n"


def _snapshot(db: Database, after_id: int) -> tuple[list[dict], int]:
    """Read new events (id > after_id), newest-first page reversed to order."""
    rows = db.query(
        "SELECT id, at, level, kind, message, data_json FROM events "
        "WHERE id > ? ORDER BY id ASC LIMIT ?",
        (after_id, MAX_STREAM_EVENTS),
    )
    events: list[dict] = []
    last_id = after_id
    for row in rows:
        last_id = max(last_id, int(row["id"]))
        try:
            data = json.loads(row["data_json"])
        except Exception:  # pragma: no cover - defensive
            data = {}
        events.append(
            {
                "id": row["id"],
                "at": row["at"],
                "level": row["level"],
                "kind": row["kind"],
                "message": row["message"],
                "data": data,
            }
        )
    return events, last_id


async def event_stream(db: Database):
    # Stream only events that arrive after connection; history is served by
    # the /api/events read route.
    recent = await asyncio.to_thread(
        db.query,
        "SELECT id FROM events ORDER BY id DESC LIMIT 1",
        (),
    )
    last_id = int(recent[0]["id"]) if recent else 0
    yield ": connected\n\n".encode("utf-8")
    import time as _time

    deadline = _time.monotonic() + MAX_STREAM_DURATION_S
    while _time.monotonic() < deadline:
        events, new_last = await asyncio.to_thread(_snapshot, db, last_id)
        for event in events:
            yield _format_sse(event).encode("utf-8")
        last_id = new_last
        await asyncio.sleep(POLL_INTERVAL_S)


def sse_response(db: Database) -> StreamingResponse:
    return StreamingResponse(
        event_stream(db),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
