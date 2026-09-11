import sqlite3

import pytest

from jobscraper.acquisition.crawler.cursor import CursorCompatibilityError, load_cursor, save_cursor
from jobscraper.acquisition.crawler.pagination import PaginationGuardState
from jobscraper.adapters.contract import CrawlCursor


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE crawl_cursors(
        id TEXT PRIMARY KEY, source_id TEXT, binding_id TEXT,
        binding_revision_id TEXT, adapter_id TEXT, adapter_version TEXT,
        cursor_schema_version INTEGER, state_json TEXT, guard_state_json TEXT,
        checkpoint_run_source_plan_id TEXT, checkpoint_at TEXT)""")
    return conn


def _plan(**changes):
    plan = dict(source_id="src", binding_id="bnd", binding_revision_id="rev1", adapter_id="fixture", adapter_version="1", cursor_schema_version=1)
    plan.update(changes)
    return plan


def _cursor(**changes):
    values = dict(source_id="src", binding_id="bnd", adapter_id="fixture", adapter_version="1", cursor_schema_version=1, state_json='{"page":2}', checkpoint_at="old")
    values.update(changes)
    return CrawlCursor(**values)


def test_cursor_round_trip_resumes_guard_for_same_plan():
    conn = _conn()
    guard = PaginationGuardState(
        consecutive_no_new_jobs_pages=1,
        seen_url_identities=("https://jobs.example.test/p1",),
    )
    save_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan(), cursor=_cursor(), guard_state=guard, now="now")
    loaded = load_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan())
    assert loaded.cursor.state_json == '{"page":2}'
    assert loaded.guard_state.consecutive_no_new_jobs_pages == 1
    assert loaded.guard_state.seen_url_identities == ("https://jobs.example.test/p1",)


def test_cross_run_resume_returns_cursor_with_fresh_guard():
    conn = _conn()
    guard = PaginationGuardState(
        consecutive_no_new_jobs_pages=2,
        seen_url_identities=("https://jobs.example.test/p1",),
    )
    save_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan(), cursor=_cursor(), guard_state=guard, now="now")
    # A new run under compatible pins resumes the cursor row but never the
    # previous run's trap/no-progress accounting.
    loaded = load_cursor(conn, run_source_plan_id="rsp2", plan_row=_plan())
    assert loaded.cursor.state_json == '{"page":2}'
    assert loaded.guard_state == PaginationGuardState()
    rows = conn.execute("SELECT COUNT(*) FROM crawl_cursors").fetchone()[0]
    assert rows == 1


def test_cross_run_save_advances_checkpoint_provenance_without_duplicating_row():
    conn = _conn()
    save_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan(), cursor=_cursor(), guard_state=PaginationGuardState(), now="t1")
    save_cursor(conn, run_source_plan_id="rsp2", plan_row=_plan(), cursor=_cursor(state_json='{"page":3}'), guard_state=PaginationGuardState(), now="t2")
    rows = conn.execute("SELECT state_json, checkpoint_run_source_plan_id FROM crawl_cursors").fetchall()
    assert len(rows) == 1
    assert rows[0]["state_json"] == '{"page":3}'
    assert rows[0]["checkpoint_run_source_plan_id"] == "rsp2"


def test_legacy_unbound_cursor_is_preserved_but_not_resumed():
    conn = _conn()
    conn.execute("INSERT INTO crawl_cursors VALUES('old','src','bnd',NULL,'fixture','1',1,'{}','{}',NULL,'old')")
    with pytest.raises(CursorCompatibilityError, match="legacy pre-v16"):
        load_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan())


def test_changed_binding_revision_restarts_fresh_without_reusing_state():
    conn = _conn()
    save_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan(), cursor=_cursor(), guard_state=PaginationGuardState(), now="now")
    # A re-pinned binding never silently resumes another revision's state;
    # the new revision starts fresh and leaves the old row untouched.
    assert load_cursor(conn, run_source_plan_id="rsp2", plan_row=_plan(binding_revision_id="rev2")) is None
    rows = conn.execute("SELECT binding_revision_id, state_json FROM crawl_cursors").fetchall()
    assert [(r["binding_revision_id"], r["state_json"]) for r in rows] == [("rev1", '{"page":2}')]


def test_cursor_code_provenance_mismatch_is_refused_not_silently_reset():
    conn = _conn()
    with pytest.raises(CursorCompatibilityError, match="adapter_version"):
        save_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan(), cursor=_cursor(adapter_version="2"), guard_state=PaginationGuardState(), now="now")
    with pytest.raises(CursorCompatibilityError, match="cursor_schema_version"):
        save_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan(), cursor=_cursor(cursor_schema_version=2), guard_state=PaginationGuardState(), now="now")
    with pytest.raises(CursorCompatibilityError, match="adapter_id"):
        save_cursor(conn, run_source_plan_id="rsp1", plan_row=_plan(), cursor=_cursor(adapter_id="other"), guard_state=PaginationGuardState(), now="now")
    assert conn.execute("SELECT COUNT(*) FROM crawl_cursors").fetchone()[0] == 0
