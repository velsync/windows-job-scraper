"""S2.3 unit tests: FTS5 capability honesty, provisioning and indexing.

Authority: docs/plans/slice-2-worker-implementation-plan-v0313.md S2.3;
docs/spec/v0.3.1.3/01_product_and_workflow.md §45.

Proves:

* provisioning is capability-gated and idempotent: FTS5 tables/triggers are
  created only when the connection reports FTS5, the recorded capability row
  always matches reality, and a non-FTS5 host records an honest
  SUBSTRING_FALLBACK with a warning and never claims BM25;
* document indexing is host-owned and revision-checked
  (``job_search_state.indexed_content_hash``): re-syncing unchanged content
  is a no-op and changed content replaces the row exactly once;
* queries return the active mode, never fabricate BM25 scores in substring
  mode, and keep structured filters (listing status, company, source, remote,
  discovery date) outside FTS.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobscraper.db.connection import fts5_available
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.search.capability import (
    SEARCH_MODE_FTS5,
    SEARCH_MODE_SUBSTRING,
)
from jobscraper.search.index import sync_search_doc
from jobscraper.search.provision import (
    FTS_TABLE,
    fts_table_present,
    provision_search,
    triggers_exist,
)
from jobscraper.search.query import SearchResult, search_jobs

NOW = "2026-09-08T00:00:00.000000Z"
LATER = "2026-09-08T02:00:00.000000Z"


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate_schema(conn, LATEST_SCHEMA_VERSION)
    return conn


def _seed_job(
    conn,
    *,
    job_id: str,
    title: str,
    description: str,
    company_id: str | None = None,
    discovered_at: str = NOW,
    listing_status: str = "ACTIVE",
) -> None:
    conn.execute(
        "INSERT INTO jobs (id, company_id, title, normalized_title,"
        " description_text, discovered_at, first_seen_at, last_seen_at,"
        " listing_status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            company_id,
            title,
            title.lower(),
            description,
            discovered_at,
            NOW,
            NOW,
            listing_status,
            NOW,
            NOW,
        ),
    )


def _seed_company(conn, *, company_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, first_seen_at,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (company_id, name, name.lower(), NOW, NOW, NOW),
    )


def _seed_location(conn, *, job_id: str, raw: str, remote: int = 0) -> None:
    conn.execute(
        "INSERT INTO job_locations (id, job_id, raw_text, remote)"
        " VALUES (?, ?, ?, ?)",
        (f"jl-{job_id}-{raw}", job_id, raw, remote),
    )


@pytest.fixture()
def conn():
    database = _mem_db()
    yield database
    database.close()


# ---------------------------------------------------- capability provisioning


def test_provision_creates_fts5_index_and_records_capability(conn) -> None:
    assert fts5_available(conn) is True
    report = provision_search(conn, now=NOW)
    assert report["mode"] == SEARCH_MODE_FTS5
    assert report["fts5_detected"] is True
    assert report["warning"] is None
    row = conn.execute("SELECT * FROM search_capability WHERE id = 1").fetchone()
    assert row["mode"] == SEARCH_MODE_FTS5
    assert row["fts5_detected"] == 1
    assert row["warning"] is None
    name = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (FTS_TABLE,),
    ).fetchone()
    assert name is not None
    assert triggers_exist(conn) is True


def test_provision_is_idempotent(conn) -> None:
    provision_search(conn, now=NOW)
    first_cap = dict(conn.execute("SELECT * FROM search_capability").fetchone())
    again = provision_search(conn, now=NOW)
    assert again["mode"] == SEARCH_MODE_FTS5
    assert conn.execute("SELECT COUNT(*) FROM search_capability").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
        " AND name LIKE 'job_search_fts_%'"
    ).fetchone()[0] == 3
    second_cap = dict(conn.execute("SELECT * FROM search_capability").fetchone())
    # provisioned_at survives re-provisioning (idempotent, not a rewrite)
    assert second_cap["provisioned_at"] == first_cap["provisioned_at"]


def test_provision_without_fts5_records_honest_substring_fallback(
    conn, monkeypatch
) -> None:
    monkeypatch.setattr("jobscraper.search.provision.fts5_available", lambda _c: False)
    report = provision_search(conn, now=NOW)
    assert report["mode"] == SEARCH_MODE_SUBSTRING
    assert report["fts5_detected"] is False
    assert report["warning"] and "not claim" in report["warning"]
    row = conn.execute("SELECT * FROM search_capability WHERE id = 1").fetchone()
    assert row["mode"] == SEARCH_MODE_SUBSTRING
    assert row["warning"] is not None
    # no virtual table and no triggers were created
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (FTS_TABLE,),
    ).fetchone()[0] == 0
    assert triggers_exist(conn) is False


def test_capability_guard_degrades_honestly_when_fts_objects_disappear(conn) -> None:
    provision_search(conn, now=NOW)
    conn.execute(f"DROP TABLE {FTS_TABLE}")
    # recorded FTS5_ACTIVE must never crash a query: degrade to substring with
    # an explicit warning instead of running a MATCH against nothing
    result = search_jobs(conn, query="backend")
    assert result.mode == SEARCH_MODE_SUBSTRING
    assert result.warning and "provision" in result.warning


def test_fts_degrades_when_sync_triggers_are_lost_but_table_remains(conn) -> None:
    """01 §45: never claim BM25 over an index that can no longer stay in sync.

    A table without its INSERT/UPDATE/DELETE triggers silently serves stale
    rows — returning FTS5_ACTIVE there would be exactly the kind of
    FTS/BM25 claim the spec forbids when it is not truly active.
    """
    provision_search(conn, now=NOW)
    _seed_company(conn, company_id="co-a", name="Alpha Labs")
    _seed_job(
        conn, job_id="job-a", title="Backend Engineer",
        description="Build reliable python services.", company_id="co-a",
    )
    sync_search_doc(conn, job_id="job-a", now=NOW)
    assert search_jobs(conn, query="python").mode == SEARCH_MODE_FTS5

    # triggers die (table stays): a new doc row would no longer be mirrored
    conn.execute(f"DROP TRIGGER IF EXISTS {FTS_TABLE}_ai")
    conn.execute(f"DROP TRIGGER IF EXISTS {FTS_TABLE}_au")
    conn.execute(f"DROP TRIGGER IF EXISTS {FTS_TABLE}_ad")
    assert triggers_exist(conn) is False
    assert fts_table_present(conn) is True

    result = search_jobs(conn, query="python")
    assert result.mode == SEARCH_MODE_SUBSTRING
    assert result.warning and "incomplete" in result.warning
    # the durable doc corpus is still searched honestly (no stale MATCH),
    # but BM25 is never claimed
    assert result.total == 1
    assert all(h.score is None for h in result.hits)

    # idempotent re-provisioning rebuilds the triggers and honest FTS mode
    provision_search(conn, now=NOW)
    assert triggers_exist(conn) is True
    assert search_jobs(conn, query="python").mode == SEARCH_MODE_FTS5


# -------------------------------------------------------------- doc indexing


def _seed_docs(conn) -> None:
    _seed_company(conn, company_id="co-a", name="Alpha Labs")
    _seed_company(conn, company_id="co-b", name="Beta Works")
    _seed_job(
        conn,
        job_id="job-a",
        title="Backend Engineer",
        description="Build reliable python services for Alpha.",
        company_id="co-a",
    )
    _seed_location(conn, job_id="job-a", raw="Berlin, Germany")
    _seed_job(
        conn,
        job_id="job-b",
        title="Frontend Engineer",
        description="React and typescript on the Beta web app.",
        company_id="co-b",
        discovered_at=LATER,
    )
    _seed_location(conn, job_id="job-b", raw="Remote", remote=1)
    _seed_job(
        conn,
        job_id="job-c",
        title="Backend Engineer (closed)",
        description="legacy monolith role",
        company_id="co-a",
        listing_status="EXPIRED",
    )
    provision_search(conn, now=NOW)
    for job_id in ("job-a", "job-b", "job-c"):
        sync_search_doc(conn, job_id=job_id, now=NOW)


def test_sync_creates_doc_and_state_exactly_once(conn) -> None:
    provision_search(conn, now=NOW)
    _seed_company(conn, company_id="co-a", name="Alpha Labs")
    _seed_job(
        conn, job_id="job-a", title="Backend Engineer",
        description="Build reliable python services.", company_id="co-a",
    )
    assert sync_search_doc(conn, job_id="job-a", now=NOW) is True
    assert sync_search_doc(conn, job_id="job-a", now=NOW) is False  # no change
    docs = conn.execute("SELECT * FROM job_search_docs").fetchall()
    assert len(docs) == 1
    state = conn.execute("SELECT * FROM job_search_state").fetchall()
    assert len(state) == 1
    assert state[0]["indexed_content_hash"]
    fts_rows = conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0]
    assert fts_rows == 1


def test_content_change_replaces_doc_without_duplicating(conn) -> None:
    provision_search(conn, now=NOW)
    _seed_company(conn, company_id="co-a", name="Alpha Labs")
    _seed_job(
        conn, job_id="job-a", title="Backend Engineer",
        description="First description version.", company_id="co-a",
    )
    sync_search_doc(conn, job_id="job-a", now=NOW)
    conn.execute(
        "UPDATE jobs SET description_text = 'Second description version.',"
        " updated_at = ? WHERE id = 'job-a'",
        (LATER,),
    )
    assert sync_search_doc(conn, job_id="job-a", now=LATER) is True
    docs = conn.execute("SELECT * FROM job_search_docs").fetchall()
    assert len(docs) == 1
    assert docs[0]["description_text"] == "Second description version."
    assert conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0] == 1
    fts = conn.execute(
        f"SELECT description_text FROM {FTS_TABLE}"
    ).fetchone()[0]
    assert fts == "Second description version."


# ------------------------------------------------------------------- queries


def test_fts_query_returns_matching_jobs_with_mode_and_score(conn) -> None:
    _seed_docs(conn)
    result = search_jobs(conn, query="backend", listing_status="ANY")
    assert result.mode == SEARCH_MODE_FTS5
    assert result.warning is None
    assert result.total == 2
    assert {h.job_id for h in result.hits} == {"job-a", "job-c"}
    assert all(h.score is not None for h in result.hits)
    # BM25 orders ascending (lower is better), ties stable by id
    scores = [h.score for h in result.hits]
    assert scores == sorted(scores)


def test_fts_query_distinguishes_terms_and_escapes_quotes(conn) -> None:
    _seed_docs(conn)
    assert {h.job_id for h in search_jobs(conn, query="python").hits} == {"job-a"}
    # quotes inside the query are literal text, never an injection
    result = search_jobs(conn, query='say "python"')
    assert result.total == 0  # no document contains 'say'
    assert result.hits == ()


def test_listing_status_filter_stays_outside_fts(conn) -> None:
    _seed_docs(conn)
    active = search_jobs(conn, query="backend", listing_status="ACTIVE")
    assert {h.job_id for h in active.hits} == {"job-a"}
    all_statuses = search_jobs(conn, query="backend", listing_status="ANY")
    assert {h.job_id for h in all_statuses.hits} == {"job-a", "job-c"}
    expired = search_jobs(conn, query="backend", listing_status="EXPIRED")
    assert {h.job_id for h in expired.hits} == {"job-c"}


def test_company_remote_and_date_filters(conn) -> None:
    _seed_docs(conn)
    assert {h.job_id for h in search_jobs(conn, query="engineer").hits} == {
        "job-a",
        "job-b",
    }
    by_company = search_jobs(conn, query="engineer", company_id="co-a")
    assert {h.job_id for h in by_company.hits} == {"job-a"}
    remote = search_jobs(conn, query="engineer", remote_only=True)
    assert {h.job_id for h in remote.hits} == {"job-b"}
    window = search_jobs(conn, query="engineer", discovered_after="2026-09-08T01:00:00Z")
    assert {h.job_id for h in window.hits} == {"job-b"}


def test_paging_bounds_the_result(conn) -> None:
    _seed_docs(conn)
    first = search_jobs(conn, query="engineer", limit=1, offset=0)
    second = search_jobs(conn, query="engineer", limit=1, offset=1)
    assert first.total == 2 and second.total == 2
    assert len(first.hits) == 1 and len(second.hits) == 1
    assert first.hits[0].job_id != second.hits[0].job_id
    assert {first.hits[0].job_id, second.hits[0].job_id} == {"job-a", "job-b"}


def test_hits_carry_structured_fields_and_locations(conn) -> None:
    _seed_docs(conn)
    (hit,) = search_jobs(conn, query="python").hits
    assert hit.job_id == "job-a"
    assert hit.title == "Backend Engineer"
    assert hit.company == "Alpha Labs"
    assert hit.listing_status == "ACTIVE"
    assert "Berlin, Germany" in hit.locations


def test_substring_fallback_mode_is_honest_and_never_scores(conn, monkeypatch) -> None:
    monkeypatch.setattr("jobscraper.search.provision.fts5_available", lambda _c: False)
    _seed_docs(conn)
    result = search_jobs(conn, query="python")
    assert result.mode == SEARCH_MODE_SUBSTRING
    assert result.warning and "not claim" in result.warning
    assert {h.job_id for h in result.hits} == {"job-a"}
    assert all(h.score is None for h in result.hits)
    # no FTS objects exist behind the fallback
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (FTS_TABLE,),
    ).fetchone()[0] == 0


def test_substring_mode_treats_query_characters_literally(conn, monkeypatch) -> None:
    monkeypatch.setattr("jobscraper.search.provision.fts5_available", lambda _c: False)
    _seed_company(conn, company_id="co-a", name="Alpha Labs")
    _seed_job(
        conn, job_id="job-a", title="Bonus 100% remote",
        description="Save 100% now", company_id="co-a",
    )
    sync_search_doc(conn, job_id="job-a", now=NOW)
    # % and _ are literal characters in the user's query, not LIKE wildcards
    assert {h.job_id for h in search_jobs(conn, query="100%").hits} == {"job-a"}
    assert search_jobs(conn, query="Save_100").total == 0


def test_result_object_carries_filters_and_paging(conn) -> None:
    _seed_docs(conn)
    result = search_jobs(
        conn,
        query="backend",
        listing_status="ACTIVE",
        company_id="co-a",
        limit=5,
        offset=0,
    )
    assert isinstance(result, SearchResult)
    assert result.limit == 5 and result.offset == 0
    assert result.filters["listing_status"] == "ACTIVE"
    assert result.filters["company_id"] == "co-a"
