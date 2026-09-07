"""Unified append-oriented event log.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md section 46.

Sensitive values are redacted before persistence via
:mod:`jobscraper.security.redaction` — there is no non-redacting append path.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from jobscraper.security.redaction import RedactionError, redact_value
from jobscraper.timeutil import to_rfc3339, utc_now

MAX_LIST_LIMIT = 1000


@dataclass(frozen=True)
class EventRecord:
    at: str
    level: str
    kind: str
    message: str
    data: dict = field(default_factory=dict)
    run_id: str | None = None
    source_id: str | None = None
    binding_id: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if self.level not in {"DEBUG", "INFO", "WARN", "ERROR"}:
            raise ValueError(f"invalid event level: {self.level}")
        if not self.kind or not self.message:
            raise ValueError("event kind/message required")


def event(
    level: str,
    kind: str,
    message: str,
    *,
    data: dict | None = None,
    run_id: str | None = None,
    source_id: str | None = None,
    binding_id: str | None = None,
    request_id: str | None = None,
    at: str | None = None,
) -> EventRecord:
    return EventRecord(
        at=at or to_rfc3339(utc_now()),
        level=level,
        kind=kind,
        message=message,
        data=dict(data or {}),
        run_id=run_id,
        source_id=source_id,
        binding_id=binding_id,
        request_id=request_id,
    )


def append_event(conn: sqlite3.Connection, record: EventRecord) -> int:
    """Append one redacted event; returns the row id."""
    try:
        redacted_data = redact_value(record.data)
        redacted_message = record.message
    except RedactionError as exc:
        # Fail closed: persist only the fact, never raw oversized payloads.
        redacted_data = {"redaction_error": str(exc)}
        redacted_message = record.message[:512]

    from jobscraper.security.redaction import redact_string

    redacted_message = redact_string(redacted_message)
    cur = conn.execute(
        "INSERT INTO events(at, level, kind, message, data_json, run_id, source_id, "
        "binding_id, request_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            record.at,
            record.level,
            record.kind,
            redacted_message,
            json.dumps(redacted_data, sort_keys=True, default=str),
            record.run_id,
            record.source_id,
            record.binding_id,
            record.request_id,
        ),
    )
    return int(cur.lastrowid or 0)


def list_recent_events(
    conn: sqlite3.Connection, *, limit: int = 100, kind: str | None = None, run_id: str | None = None
) -> list[EventRecord]:
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    sql = "SELECT * FROM events"
    clauses: list[str] = []
    params: list = []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out: list[EventRecord] = []
    for row in rows:
        try:
            data = json.loads(row["data_json"])
        except Exception:
            data = {"_corrupt": True}
        out.append(
            EventRecord(
                at=row["at"],
                level=row["level"],
                kind=row["kind"],
                message=row["message"],
                data=data,
                run_id=row["run_id"],
                source_id=row["source_id"],
                binding_id=row["binding_id"],
                request_id=row["request_id"],
            )
        )
    return out


class EventLog:
    """Convenience wrapper binding a connection with common event kinds."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def append(self, record: EventRecord) -> int:
        return append_event(self.conn, record)

    def info(self, kind: str, message: str, **kwargs) -> int:
        return append_event(self.conn, event("INFO", kind, message, **kwargs))

    def warn(self, kind: str, message: str, **kwargs) -> int:
        return append_event(self.conn, event("WARN", kind, message, **kwargs))

    def error(self, kind: str, message: str, **kwargs) -> int:
        return append_event(self.conn, event("ERROR", kind, message, **kwargs))
