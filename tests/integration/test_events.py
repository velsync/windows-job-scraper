"""Integration tests for the unified event log (S0.4)."""

import json

import pytest

from jobscraper.diagnostics.events import EventRecord, append_event, list_recent_events
from jobscraper.security.redaction import REDACTED


def test_append_and_list_roundtrip(db):
    record = EventRecord(
        at="2026-01-01T00:00:00.000000Z",
        level="INFO",
        kind="test.event",
        message="hello",
        data={"k": "v"},
        source_id="src1",
    )
    row_id = append_event(db.conn, record)
    assert row_id > 0
    events = list_recent_events(db.conn, limit=10)
    assert len(events) == 1
    assert events[0].kind == "test.event"
    assert events[0].data == {"k": "v"}
    assert events[0].at == "2026-01-01T00:00:00.000000Z"


def test_append_is_redacted(db):
    record = EventRecord(
        at="2026-01-01T00:00:00.000000Z",
        level="INFO",
        kind="test.leaky",
        message="fetch failed",
        data={"Authorization": "Bearer sekrit", "nested": {"cookie": "a=b"}},
    )
    append_event(db.conn, record)
    stored = db.conn.execute("SELECT data_json, message FROM events WHERE kind='test.leaky'").fetchone()
    data = json.loads(stored["data_json"])
    assert data["Authorization"] == REDACTED
    assert data["nested"]["cookie"] == REDACTED


def test_secret_absent_from_sqlite_bytes(db):
    secret = "SUPERSECRETTICKETVALUE123"
    record = EventRecord(
        at="2026-01-01T00:00:00.000000Z",
        level="INFO",
        kind="test.scan",
        message="bootstrap issued",
        data={"bootstrap_ticket": secret, "note": f"ticket={secret}"},
    )
    append_event(db.conn, record)
    raw = b""
    for chunk in db.conn.iterdump():
        raw += chunk.encode("utf-8")
    assert secret.encode("utf-8") not in raw


def test_append_ordering_and_limit(db):
    for i in range(30):
        append_event(
            db.conn,
            EventRecord(at=f"2026-01-01T00:00:{i:02d}.000000Z", level="INFO", kind="k", message=f"m{i}"),
        )
    events = list_recent_events(db.conn, limit=10)
    assert len(events) == 10
    assert events[0].message == "m29"  # newest first
    assert events[-1].message == "m20"


def test_invalid_level_rejected():
    with pytest.raises(ValueError):
        EventRecord(at="2026-01-01T00:00:00.000000Z", level="NOPE", kind="k", message="m")


def test_filter_by_kind_and_run(db):
    append_event(db.conn, EventRecord(at="2026-01-01T00:00:00.000000Z", level="INFO", kind="a", message="1", run_id="r1"))
    append_event(db.conn, EventRecord(at="2026-01-01T00:00:01.000000Z", level="INFO", kind="b", message="2", run_id="r2"))
    assert [e.kind for e in list_recent_events(db.conn, kind="a")] == ["a"]
    assert [e.run_id for e in list_recent_events(db.conn, run_id="r2")] == ["r2"]
