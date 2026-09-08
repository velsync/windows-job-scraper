"""Corrective regressions found during independent S2.3 review.

These tests pin only the defects found after the original S2.3 implementation:
search provisioning must make the durable corpus complete before claiming FTS5,
and deterministic cleaning must recognize ordinary Markdown even when it has no
links.
"""

from __future__ import annotations

import sqlite3

from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.contentclean import clean
from jobscraper.search.capability import SEARCH_MODE_FTS5
from jobscraper.search.index import sync_search_doc
from jobscraper.search.provision import FTS_TABLE, provision_search
from jobscraper.search.query import search_jobs

NOW = "2026-09-08T09:00:00.000000Z"
LATER = "2026-09-08T10:00:00.000000Z"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate_schema(conn, LATEST_SCHEMA_VERSION)
    return conn


def _seed_job(conn: sqlite3.Connection, *, job_id: str = "job-a", description: str) -> None:
    conn.execute(
        "INSERT INTO jobs (id, title, normalized_title, description_text,"
        " discovered_at, first_seen_at, last_seen_at, listing_status, created_at, updated_at)"
        " VALUES (?, 'Backend Engineer', 'backend engineer', ?, ?, ?, ?, 'ACTIVE', ?, ?)",
        (job_id, description, NOW, NOW, NOW, NOW, NOW),
    )


def test_provision_backfills_preexisting_canonical_jobs_before_claiming_fts5() -> None:
    """A v12→v13-style existing corpus must be searchable immediately after boot."""
    conn = _db()
    try:
        _seed_job(conn, description="legacy python services")
        assert conn.execute("SELECT COUNT(*) FROM job_search_docs").fetchone()[0] == 0

        report = provision_search(conn, now=NOW)

        assert conn.execute("SELECT COUNT(*) FROM job_search_docs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM job_search_state").fetchone()[0] == 1
        result = search_jobs(conn, query="python")
        assert result.total == 1
        if report["mode"] == SEARCH_MODE_FTS5:
            assert result.mode == SEARCH_MODE_FTS5
            assert conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0] == 1
    finally:
        conn.close()


def test_reprovision_reconciles_docs_changed_while_fts_triggers_were_missing() -> None:
    """Recreating triggers alone must not reactivate a stale BM25 index."""
    conn = _db()
    try:
        provision_search(conn, now=NOW)
        _seed_job(conn, description="oldword services")
        assert sync_search_doc(conn, job_id="job-a", now=NOW) is True
        assert search_jobs(conn, query="oldword").total == 1

        for suffix in ("ai", "au", "ad"):
            conn.execute(f"DROP TRIGGER IF EXISTS {FTS_TABLE}_{suffix}")

        conn.execute(
            "UPDATE jobs SET description_text = 'newword services', updated_at = ? WHERE id = 'job-a'",
            (LATER,),
        )
        assert sync_search_doc(conn, job_id="job-a", now=LATER) is True

        degraded = search_jobs(conn, query="newword")
        assert degraded.total == 1
        assert degraded.mode != SEARCH_MODE_FTS5

        repaired = provision_search(conn, now=LATER)
        assert repaired["mode"] == SEARCH_MODE_FTS5
        assert search_jobs(conn, query="newword").total == 1
        assert search_jobs(conn, query="oldword").total == 0
    finally:
        conn.close()


def test_markdown_without_links_is_recognized_and_plain_text_drops_syntax() -> None:
    raw = "# Role\n\n- Python\n- SQL\n\n**Strong** experience"

    result = clean(raw)

    assert result.markdown is not None
    assert result.markdown.startswith("# Role")
    assert "- Python" in result.markdown
    assert "**Strong**" in result.markdown
    assert result.text is not None
    assert "# Role" not in result.text
    assert "- Python" not in result.text
    assert "**Strong**" not in result.text
    assert "Role" in result.text
    assert "Python" in result.text
    assert "Strong experience" in result.text
