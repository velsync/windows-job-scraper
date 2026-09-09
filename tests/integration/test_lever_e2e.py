"""S2.6 end-to-end: a provider-native Lever site run through the spine.

Proves the whole durable path for the second built-in provider adapter, with
the host doing every authoritative step:

``Source`` → ``SourceAdapterBinding`` → immutable ``BindingRevision`` (config
pinned) → ``RunSourcePlan`` → host-owned execution (destination policy, fetch
outside any transaction) → ``ResultEnvelope`` → validity gate → adapter parse →
fenced ``Observation`` ingest → canonical pipeline (normalize, clean, dedupe,
provenance, companies, locations) → obligations → search index.

Lever-specific truths pinned here (not copied from Greenhouse):

* the list endpoint is a bare array with **no declared total** and
  ``skip``/``limit`` paging — a short page is terminal, a full page continues
  through a host-persisted cursor, a server that ignores ``skip`` is a trap
  that leaves coverage PARTIAL, and a later run never resumes an old offset;
* ``createdAt`` is not a claimed ``posted_at``; ``workplaceType: remote`` is
  the remote signal; ``hostedUrl``/``applyUrl`` are accepted only on reviewed
  hosted hosts, and the apply link the product shows comes from the endpoint
  table + pinned site token, never from content.

Fixtures are the deterministic files in ``tests/fixtures/lever/`` served over
loopback by a local HTTP server, so the run is reproducible and offline.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from jobscraper.applications.applylink import best_application_url
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.driver import execute_run
from jobscraper.profiles.core import create_profile
from jobscraper.runtime.provisioning import (
    ensure_builtin_adapter_definition,
    provision_source_and_binding,
)
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run
from jobscraper.search.capability import SEARCH_MODE_FTS5
from jobscraper.search.provision import provision_search
from jobscraper.search.query import search_jobs

NOW = "2026-09-08T09:00:00.000000Z"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lever"

_JSON = "application/json"

ID1 = "1a2b3c4d-0000-4000-8000-000000004001"
ID2 = "1a2b3c4d-0000-4000-8000-000000004002"
ID3 = "1a2b3c4d-0000-4000-8000-000000004003"
GHOST = "7a7b7c7d-0000-4000-8000-000000007002"
HOSTED = "https://jobs.lever.co/acme"

#: site token -> (fixture, http status, content type) for the listing endpoint
_LIST_ROUTES = {
    "acme": ("postings_list.json", 200, _JSON),
    "paged": ("postings_list_with_content.json", 200, _JSON),
    "ignoreskip": ("postings_list_with_content.json", 200, _JSON),
    "emptyboard": ("postings_list_empty.json", 200, _JSON),
    "removedjob": ("postings_list_removed_posting.json", 200, _JSON),
    "changedtemplate": ("postings_list_changed_template.json", 200, _JSON),
    "missingfields": ("postings_list_missing_required_fields.json", 200, _JSON),
    "malformed": ("postings_list_malformed.json", 200, _JSON),
    "idmismatch": ("postings_list.json", 200, _JSON),
    "rejectedthenclean": ("postings_list_rejected_member_paged.json", 200, _JSON),
    "ratelimited": ("rate_limited.429.json", 429, _JSON),
    "challenge": ("challenge.403.html", 403, "text/html; charset=utf-8"),
}
#: (site, posting id) -> (fixture, http status) for the detail endpoint
_DETAIL_ROUTES = {
    ("acme", ID1): ("posting_detail.json", 200),
    ("acme", ID2): ("posting_detail_multi_location.json", 200),
    ("acme", ID3): ("posting_detail_remote_hostile_links.json", 200),
    ("removedjob", GHOST): ("posting_removed.404.json", 404),
}
#: every detail on this site answers with a *different* posting identity
_IDMISMATCH_DETAIL = ("posting_detail_id_mismatch.json", 200)
#: an unknown detail target is what the provider says when a posting is gone
_DEFAULT_DETAIL = ("posting_removed.404.json", 404)

_LIST_PATH = re.compile(r"^/v0/postings/(?P<board>[a-z0-9_-]+)$")
_DETAIL_PATH = re.compile(r"^/v0/postings/(?P<board>[a-z0-9_-]+)/(?P<job_id>[a-z0-9_-]{8,40})$")


class _LeverHandler(http.server.BaseHTTPRequestHandler):
    """A loopback stand-in for the public Lever postings API.

    Array fixtures honour ``skip``/``limit`` like the provider does; the
    ``ignoreskip`` site serves the first ``limit`` items regardless of
    ``skip`` (a paging trap).
    """

    def do_GET(self):  # noqa: N802 - http.server interface
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        detail = _DETAIL_PATH.fullmatch(parts.path)
        listing = _LIST_PATH.fullmatch(parts.path)
        if detail is not None:
            board, job_id = detail.group("board"), detail.group("job_id")
            if board == "idmismatch":
                fixture, status = _IDMISMATCH_DETAIL
            else:
                fixture, status = _DETAIL_ROUTES.get((board, job_id), _DEFAULT_DETAIL)
            self._serve((FIXTURES / fixture).read_bytes(), status, _JSON)
            return
        if listing is not None:
            board = listing.group("board")
            if board in _LIST_ROUTES:
                fixture, status, content_type = _LIST_ROUTES[board]
            else:
                fixture, status, content_type = _DEFAULT_DETAIL[0], 404, _JSON
            body = (FIXTURES / fixture).read_bytes()
            if status == 200 and content_type == _JSON:
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                if isinstance(payload, list):
                    skip = int((query.get("skip") or ["0"])[0])
                    limit = int((query.get("limit") or ["100"])[0])
                    if board == "ignoreskip":
                        skip = 0
                    body = json.dumps(payload[skip:skip + limit]).encode("utf-8")
            self._serve(body, status, content_type)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(self, body: bytes, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LeverHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def db(tmp_path, server):
    database = Database(tmp_path / "lever-e2e.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    provision_search(database.conn, now=NOW)
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Host-owned harness: provisioning + run creation from the pinned revision
# ---------------------------------------------------------------------------


def _provision(db, server, *, board, config=None, source_family="ATS_BOARD"):
    """Provision Source → Binding → immutable BindingRevision (config pinned).

    The entry URL is a loopback literal, so the driver's narrow host rule
    grants that exact host and nothing else (04 §5.1); the canonical host is
    the operator's statement of what the source *is*.
    """
    conn = db.conn
    port = server.server_address[1]
    conn.executescript(
        f"""
        INSERT INTO adapter_permission_profiles (id, display_name, created_at)
        VALUES ('perm-1', 'default', '{NOW}');
        INSERT INTO adapter_permission_profile_revisions
            (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1', 'perm-1', 1, '{{}}', '{NOW}');
        """
    )
    # the durable adapter identity row comes from the registry manifest, not
    # from a hand-written string
    definition = ensure_builtin_adapter_definition(conn, "lever", now=NOW)
    return provision_source_and_binding(
        conn,
        display_name=f"{board} postings",
        source_family=source_family,
        entry_url=f"http://127.0.0.1:{port}/{board}",
        canonical_host="jobs.lever.co",
        adapter_id="lever",
        adapter_version=definition.adapter_version,
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        config={
            "board": board,
            "api_base_url": f"http://127.0.0.1:{port}",
            **(config or {}),
        },
        now=NOW,
    )


def _start_run(db, provisioned, *, board, now=NOW):
    """Create a run whose plan mirrors the pinned revision exactly."""
    conn = db.conn
    rev = conn.execute(
        """
        SELECT r.adapter_id, r.adapter_version, r.strategy, r.execution_class,
               r.permission_profile_id, r.permission_profile_revision,
               d.adapter_api_version
        FROM source_adapter_binding_revisions r
        JOIN adapter_definitions d
          ON d.adapter_id = r.adapter_id
         AND d.adapter_version = r.adapter_version
        WHERE r.id = ?
        """,
        (provisioned.binding_revision_id,),
    ).fetchone()
    plan = dict(
        source_id=provisioned.source_id,
        source_plan_group_id="grp-lever",
        fallback_rank=0,
        binding_id=provisioned.binding_id,
        binding_revision_id=provisioned.binding_revision_id,
        adapter_id=rev["adapter_id"],
        adapter_version=rev["adapter_version"],
        adapter_api_version=rev["adapter_api_version"],
        strategy=rev["strategy"],
        execution_class=rev["execution_class"],
        permission_profile_id=rev["permission_profile_id"],
        permission_profile_revision=rev["permission_profile_revision"],
    )
    run_id, plans = create_run(conn, profile_id=None, plans=[plan], now=now)
    enqueue_request(
        conn,
        run_id=run_id,
        run_source_plan_id=plans[0],
        source_id=provisioned.source_id,
        binding_id=provisioned.binding_id,
        request_type="LIST_FETCH",
        target_identity=f"http://127.0.0.1/v0/postings/{board}",
        logical_key='{"page": 1}',
        strategy=rev["strategy"],
        execution_class=rev["execution_class"],
        now=now,
    )
    return run_id


#: what an operator records about the employer when pinning a site: the
#: postings API itself never states the company name, so it is binding config.
ACME_CONFIG = {
    "company_name": "Acme Fixtures",
    "careers_url": "https://acme.example/careers",
}


def _run_board(db, server, *, board, config=None, now=NOW, profile=True):
    """Provision + run one site end to end; returns (run_id, provisioned)."""
    provisioned = _provision(db, server, board=board, config={**ACME_CONFIG, **(config or {})})
    run_id = _start_run(db, provisioned, board=board, now=now)
    if profile:
        create_profile(
            db.conn,
            snapshot={
                "name": "Seeker",
                "keywords": ["python", "engineer"],
                "eligible_countries": ["DE", "FR"],
                "remote_rules": {"remote_ok": True},
                "min_score_inbox": 0,
            },
            now=now,
        )
    return run_id, provisioned


_ACQUISITION_TYPES = (
    "SOURCE_HEALTH_CHECK", "SOURCE_DISCOVERY", "LIST_FETCH", "SOURCE_CRAWL",
    "DETAIL_FETCH", "ADAPTER_SMOKE",
)


def _requests(db, run_id=None):
    """Durable *acquisition* requests, in claim order."""
    placeholders = ",".join("?" for _ in _ACQUISITION_TYPES)
    sql = f"SELECT * FROM scrape_requests WHERE request_type IN ({placeholders})"
    params: list = list(_ACQUISITION_TYPES)
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    return db.conn.execute(sql + " ORDER BY created_at, id", params).fetchall()


def _coverage(db):
    return db.conn.execute("SELECT * FROM enumeration_coverage ORDER BY created_at, id").fetchall()


def _jobs(db):
    return db.conn.execute("SELECT * FROM jobs ORDER BY title").fetchall()


def _presences(db):
    return db.conn.execute("SELECT * FROM job_sources ORDER BY source_job_id").fetchall()


def _evidence(db, kind=None, ref_like=None):
    sql = "SELECT * FROM acquisition_evidence WHERE 1 = 1"
    params: list = []
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    if ref_like is not None:
        sql += " AND ref LIKE ?"
        params.append(ref_like)
    return db.conn.execute(sql + " ORDER BY created_at, id", params).fetchall()


def _fetch_urls(db):
    return [r["requested_url"] for r in db.conn.execute(
        "SELECT requested_url FROM fetch_attempts ORDER BY fetched_at, id"
    ).fetchall()]


def _outcome(db, run_id):
    return db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"]


# ---------------------------------------------------------------------------
# The full vertical path
# ---------------------------------------------------------------------------


def test_site_run_reaches_canonical_jobs_through_the_whole_spine(db, server):
    """One Lever site, listed then detailed, becomes canonical jobs."""
    run_id, provisioned = _run_board(db, server, board="acme")

    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    # ---- requests: one enumeration page + three typed detail children
    requests = _requests(db, run_id)
    assert [(r["request_type"], r["status"], r["page_class"]) for r in requests] == [
        ("LIST_FETCH", "SUCCEEDED", "VALID_LIST"),
        ("DETAIL_FETCH", "SUCCEEDED", "VALID_JOB"),
        ("DETAIL_FETCH", "SUCCEEDED", "VALID_JOB"),
        ("DETAIL_FETCH", "SUCCEEDED", "VALID_JOB"),
    ]
    listing = requests[0]
    details = requests[1:]
    assert {json.loads(d["payload_json"])["target_reference"] for d in details} == {ID1, ID2, ID3}
    for detail in details:
        # ACQ-04: child work is durable, typed, parented and depth-bounded
        assert detail["parent_request_id"] == listing["id"]
        assert detail["depth"] == 1
        assert detail["execution_class"] == "HTTP"
        assert detail["strategy"] == "PROVIDER_NATIVE"
        assert detail["run_source_plan_id"] == listing["run_source_plan_id"]

    # ---- canonical jobs: one row per posting, one presence per source
    jobs = _jobs(db)
    assert [j["title"] for j in jobs] == ["Backend Engineer", "Platform Engineer", "Senior Data Engineer"]
    presences = _presences(db)
    assert len(presences) == 3
    assert {p["source_job_id"] for p in presences} == {ID1, ID2, ID3}
    for presence in presences:
        assert presence["source_id"] == provisioned.source_id
        assert presence["binding_id"] == provisioned.binding_id
        assert presence["presence_state"] == "ACTIVE"
        # what the host fetched was the employer's own structured payload, and
        # the posting links live on the host this source is about
        assert presence["content_kind"] == "STRUCTURED"
        assert presence["same_host_as_source"] == 1
    for observation in db.conn.execute("SELECT * FROM job_observations").fetchall():
        assert observation["adapter_id"] == "lever"
        assert observation["adapter_version"] == "1.0.0"
        assert observation["strategy"] == "PROVIDER_NATIVE"
        assert observation["execution_class"] == "HTTP"
        assert observation["fetch_attempt_id"] and observation["parse_attempt_id"]

    # ---- 02 §32 origin resolution: the ATS identity, resolved not guessed
    company_ids = {job["company_id"] for job in jobs}
    assert len(company_ids) == 1 and None not in company_ids
    company = db.conn.execute("SELECT * FROM companies WHERE id = ?", (company_ids.pop(),)).fetchone()
    assert company["name"] == "Acme Fixtures"  # binding config, never the slug
    assert company["ats_provider"] == "LEVER"
    assert company["ats_board"] == "acme"
    assert company["domain"] == "acme.example"
    identifiers = db.conn.execute(
        "SELECT kind, value FROM company_identifiers WHERE company_id = ?", (company["id"],)
    ).fetchall()
    assert {row["value"] for row in identifiers} >= {"LEVER/acme", "acme.example"}
    for job in jobs:
        assert job["origin_provider"] == "LEVER"
        assert job["origin_board"] == "acme"
        assert job["origin_job_id"] in {ID1, ID2, ID3}
    resolved = db.conn.execute(
        "SELECT origin_provider, origin_board, origin_job_id,"
        " origin_resolution_confidence, origin_resolution_evidence_json,"
        " source_quality_class, canonical_job_url, application_url"
        " FROM job_sources WHERE source_job_id = ?",
        (ID1,),
    ).fetchone()
    assert resolved["origin_provider"] == "LEVER"
    assert resolved["origin_board"] == "acme"
    assert resolved["origin_job_id"] == ID1
    assert resolved["origin_resolution_confidence"] >= 0.9
    resolution = json.loads(resolved["origin_resolution_evidence_json"])
    assert resolution["status"] == "RESOLVED"
    assert resolution["origin_url"] == f"{HOSTED}/{ID1}"
    assert resolution["resolver_version"] and resolution["endpoint_rules_version"]
    endpoint_evidence = [item for item in resolution["evidence"] if item["kind"] == "ATS_ENDPOINT"]
    assert endpoint_evidence
    for item in endpoint_evidence:
        assert item["field"] in {"canonical_job_url", "application_url"}
        assert item["value"].startswith(f"{HOSTED}/")
        assert item["pattern_id"] and item["strength"]
    assert resolved["source_quality_class"] == "EMPLOYER_STRUCTURED_ATS"
    assert resolved["canonical_job_url"] == f"{HOSTED}/{ID1}"
    assert resolved["application_url"] == f"{HOSTED}/{ID1}"

    # ---- normalized fields survive into the canonical row
    backend = jobs[0]
    assert backend["normalized_title"] == "backend engineer"
    assert backend["employment_type"] == "FULL_TIME"  # from categories.commitment
    # Lever states createdAt (creation), not a publication time: no posted_at
    # is asserted, and nothing was guessed in its place
    assert backend["posted_at"] is None
    assert backend["listing_status"] == "ACTIVE"
    assert backend["description_text"] and "local-first" in backend["description_text"]
    assert "Own the ingestion pipeline" in backend["description_text"]
    assert "Salary band" in backend["description_text"]
    locations = db.conn.execute(
        "SELECT raw_text, city, region, country, remote FROM job_locations"
        " WHERE job_id = ? ORDER BY raw_text",
        (backend["id"],),
    ).fetchall()
    assert [(row["city"], row["country"], row["remote"]) for row in locations] == [("Berlin", "DE", 0)]
    # the hybrid multi-location posting keeps every location the provider
    # stated in allLocations, none of them flagged remote
    platform = db.conn.execute(
        "SELECT raw_text, city, country, remote FROM job_locations WHERE job_id = ?",
        (jobs[1]["id"],),
    ).fetchall()
    assert {(r["city"], r["country"], r["remote"]) for r in platform} == {
        ("Berlin", "DE", 0), ("Paris", "FR", 0),
    }
    # workplaceType: remote → the one location row is remote, no city invented
    remote_job = db.conn.execute(
        "SELECT raw_text, city, country, remote FROM job_locations WHERE job_id = ?",
        (jobs[2]["id"],),
    ).fetchall()
    assert [(r["city"], r["remote"]) for r in remote_job] == [(None, 1)]
    assert jobs[2]["remote_mode"] == "REMOTE"
    # a bare "Remote" is not evidence of unrestricted scope (Slice-1 semantics)
    assert jobs[2]["remote_worldwide"] == 0

    # ---- coverage: a single short page is terminal enumeration proof
    rows = _coverage(db)
    assert len(rows) == 1
    cov = rows[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
    assert cov["pages_completed"] == 1
    contributing = db.conn.execute(
        "SELECT request_id FROM coverage_contributing_request WHERE coverage_id = ?", (cov["id"],)
    ).fetchall()
    assert [row["request_id"] for row in contributing] == [listing["id"]]
    seen = db.conn.execute(
        "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id = ?", (cov["id"],)
    ).fetchall()
    assert {row["stable_source_identity"] for row in seen} == {ID1, ID2, ID3}

    # ---- evidence spine is durable at every hop
    assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 4
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 4
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 6
    assert len(_evidence(db, kind="RESULT_ENVELOPE")) == 4
    assert len(_evidence(db, kind="PAGE_VALIDITY")) == 4
    assert len(_evidence(db, kind="ORIGIN_RESOLUTION")) == 6
    # field evidence names the Lever locators, including createdAt as evidence
    locators = {row["locator_value"] for row in db.conn.execute(
        "SELECT locator_value FROM field_evidence"
    ).fetchall()}
    assert {"id", "text", "categories.allLocations", "categories.commitment", "createdAt",
            "workplaceType", "company_name"} <= locators
    assert db.conn.execute(
        "SELECT COUNT(*) FROM field_evidence WHERE field_name = 'posted_at'"
    ).fetchone()[0] == 0

    # ---- run accounting is derived from evidence, not asserted by the adapter
    run = db.conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "SUCCEEDED"
    assert run["jobs_discovered"] == 6
    assert run["requests_total"] >= 4
    assert run["requests_failed"] == 0
    assert run["jobs_saved"] + run["jobs_updated"] == 3
    assert _outcome(db, run_id) == "SATISFIED"

    # ---- obligations drained: the jobs are Inbox-visible for the profile
    assert db.conn.execute("SELECT COUNT(*) FROM job_eligibility").fetchone()[0] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM job_scores").fetchone()[0] == 3
    appearances = db.conn.execute(
        "SELECT COUNT(*) FROM job_profile_inbox_events WHERE event_kind = 'NEW_ELIGIBLE_APPEARANCE'"
    ).fetchone()[0]
    assert appearances == 3

    # ---- and the search index finds them (FTS5, not a LIKE fallback)
    result = search_jobs(db.conn, query="backend engineer")
    assert result.mode == SEARCH_MODE_FTS5
    assert result.total == 1
    assert result.hits[0].job_id == backend["id"]


def test_inline_content_listing_dispatches_no_detail_children(db, server):
    """When the listing already carries content the adapter proposes no
    detail work; the run is one request and still proves terminal enumeration."""
    run_id, _ = _run_board(db, server, board="paged")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert len(_jobs(db)) == 3
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["pages_completed"] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE description_text IS NOT NULL"
    ).fetchone()[0] == 3


def test_empty_site_is_a_successful_terminal_enumeration(db, server):
    """An accepted empty postings array is authority that the site has no jobs.

    The validity classifier reads a bare ``[]`` as a valid list (it has no
    envelope key to inspect); the *adapter* recognizes it as SUCCESS_EMPTY,
    which the host records as a terminal enumeration (ACQ-03).
    """
    run_id, _ = _run_board(db, server, board="emptyboard")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert requests[0]["page_class"] == "VALID_LIST"
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert parse["outcome_kind"] == "SUCCESS_EMPTY"
    assert _jobs(db) == []
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert _outcome(db, run_id) == "SATISFIED"


# ---------------------------------------------------------------------------
# Offset paging (02 §19): continuation, termination, traps, run isolation
# ---------------------------------------------------------------------------


def test_a_full_page_continues_through_a_host_cursor_until_a_short_page(db, server):
    """``limit=2`` over 3 postings: page 1 is full (continue), page 2 is short
    (terminal).  The continuation is a durable request planned from the
    host-persisted cursor, and coverage counts both pages."""
    run_id, provisioned = _run_board(db, server, board="paged", config={"page_size": 2})
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [(r["request_type"], r["status"], r["page_class"]) for r in requests] == [
        ("LIST_FETCH", "SUCCEEDED", "VALID_LIST"),
        ("LIST_FETCH", "SUCCEEDED", "VALID_LIST"),
    ]
    urls = _fetch_urls(db)
    assert [u.rsplit("?", 1)[1] for u in urls] == ["mode=json&skip=0&limit=2", "mode=json&skip=2&limit=2"]
    parses = db.conn.execute(
        "SELECT outcome_kind, continuation_required, coverage_proposal_json, cursor_proposal_json"
        " FROM parse_attempts ORDER BY parsed_at, id"
    ).fetchall()
    assert [(p["outcome_kind"], p["continuation_required"]) for p in parses] == [
        ("SUCCESS_WITH_JOBS", 1), ("SUCCESS_WITH_JOBS", 0),
    ]
    assert json.loads(parses[0]["coverage_proposal_json"])["page_full"] is True
    assert json.loads(parses[1]["coverage_proposal_json"])["page_full"] is False

    assert [j["title"] for j in _jobs(db)] == ["Backend Engineer", "Platform Engineer", "Senior Data Engineer"]
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["pages_completed"] == 2
    contributing = db.conn.execute(
        "SELECT COUNT(*) FROM coverage_contributing_request WHERE coverage_id = ?", (cov["id"],)
    ).fetchone()[0]
    assert contributing == 2
    assert _outcome(db, run_id) == "SATISFIED"

    # the durable cursor is scoped to the plan that produced it
    cursor = db.conn.execute("SELECT * FROM crawl_cursors").fetchone()
    assert cursor["adapter_id"] == "lever"
    assert cursor["binding_id"] == provisioned.binding_id
    state = json.loads(cursor["state_json"])
    assert state["plan"] == requests[0]["run_source_plan_id"]
    assert state["skip"] == 2


def test_a_later_run_never_resumes_an_earlier_runs_offset(db, server):
    """Coverage is full-source per run: run 2 starts from ``skip=0`` even
    though the binding still carries run 1's offset cursor."""
    first_run, provisioned = _run_board(db, server, board="paged", config={"page_size": 2})
    assert execute_run(db.conn, first_run) == "SUCCEEDED"
    assert json.loads(db.conn.execute("SELECT state_json FROM crawl_cursors").fetchone()[0])["skip"] == 2

    second_run = _start_run(db, provisioned, board="paged")
    assert execute_run(db.conn, second_run) == "SUCCEEDED"

    urls = _fetch_urls(db)
    assert [u.rsplit("?", 1)[1] for u in urls] == [
        "mode=json&skip=0&limit=2", "mode=json&skip=2&limit=2",
        "mode=json&skip=0&limit=2", "mode=json&skip=2&limit=2",
    ]
    generations = _coverage(db)
    assert len(generations) == 2
    assert all(g["completion_state"] == "COMPLETE" and g["pages_completed"] == 2 for g in generations)
    assert len(_jobs(db)) == 3
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 6
    # still exactly one cursor row per binding, now naming the second plan
    cursors = db.conn.execute("SELECT state_json FROM crawl_cursors").fetchall()
    assert len(cursors) == 1
    second_plan = _requests(db, second_run)[0]["run_source_plan_id"]
    assert json.loads(cursors[0]["state_json"])["plan"] == second_plan


def test_a_server_that_ignores_skip_is_a_trap_not_a_complete_enumeration(db, server):
    """The second page repeats the first page's posting set: the adapter stops
    proposing cursors, nothing is duplicated, and coverage stays PARTIAL —
    a paging trap never becomes absence authority (02 §19, RUN-13)."""
    run_id, _ = _run_board(db, server, board="ignoreskip", config={"page_size": 2})
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert [(r["request_type"], r["status"]) for r in requests] == [
        ("LIST_FETCH", "SUCCEEDED"), ("LIST_FETCH", "SUCCEEDED"),
    ]
    assert [u.rsplit("?", 1)[1] for u in _fetch_urls(db)] == [
        "mode=json&skip=0&limit=2", "mode=json&skip=2&limit=2",
    ]
    # the repeated page was ingested idempotently: 2 jobs, no duplicates
    assert [j["title"] for j in _jobs(db)] == ["Backend Engineer", "Platform Engineer"]
    assert len(_presences(db)) == 2
    parses = db.conn.execute(
        "SELECT outcome_kind, continuation_required FROM parse_attempts ORDER BY parsed_at, id"
    ).fetchall()
    assert [(p["outcome_kind"], p["continuation_required"]) for p in parses] == [
        ("SUCCESS_WITH_JOBS", 1), ("SUCCESS_WITH_JOBS", 1),
    ]
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert cov["pages_completed"] == 2
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"
    # nothing was expired or closed on the strength of a trapped walk
    assert all(p["presence_state"] == "ACTIVE" for p in _presences(db))


def test_a_rejected_member_on_an_earlier_page_is_never_laundered_into_complete(db, server):
    """S2.6 corrective (ACQ-03, RUN-13).

    Page 1 (``limit=2``) is full but carries a listed member the adapter must
    reject (no usable id).  Page 2 would be a clean short page.  If the walk
    continued, the clean terminal page would finalize the *same* generation
    COMPLETE and grant absence authority over a membership set that was
    provably incomplete on page 1.  The adapter therefore proposes no cursor
    after a PARTIAL page: coverage stays PARTIAL, and the good observations
    from page 1 are still persisted (PARTIAL is not FAILURE).
    """
    run_id, _ = _run_board(
        db, server, board="rejectedthenclean", config={"page_size": 2, "detail_fetch": False}
    )
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    # exactly one page was fetched: the walk stopped at the degraded page
    assert [(r["request_type"], r["status"]) for r in requests] == [("LIST_FETCH", "SUCCEEDED")]
    assert [u.rsplit("?", 1)[1] for u in _fetch_urls(db)] == ["mode=json&skip=0&limit=2"]
    parse = db.conn.execute(
        "SELECT outcome_kind, continuation_required, coverage_proposal_json, cursor_proposal_json,"
        " review_evidence_json FROM parse_attempts"
    ).fetchone()
    assert parse["outcome_kind"] == "PARTIAL"
    assert parse["continuation_required"] == 1
    proposal = json.loads(parse["coverage_proposal_json"])
    assert proposal["rejected_members"] == 1
    assert proposal["page_full"] is True
    review = json.loads(parse["review_evidence_json"])
    assert any(item["reason"] == "REQUIRED_FIELD_MISSING" for item in review)
    assert db.conn.execute("SELECT COUNT(*) FROM crawl_cursors").fetchone()[0] == 0

    # the valid member was persisted; nothing was invented for the rejected one
    assert [j["title"] for j in _jobs(db)] == ["Backend Engineer"]
    assert len(_presences(db)) == 1

    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert cov["pages_completed"] == 1
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"
    assert all(p["presence_state"] == "ACTIVE" for p in _presences(db))


# ---------------------------------------------------------------------------
# Recognized non-job outcomes are typed evidence, never fabricated jobs
# ---------------------------------------------------------------------------


def test_removed_posting_detail_is_typed_closure_evidence(db, server):
    """A 404 on a detail child closes that identity without inventing a job."""
    run_id, _ = _run_board(db, server, board="removedjob", config={"detail_fetch": True})
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [(r["request_type"], r["status"], r["page_class"]) for r in requests] == [
        ("LIST_FETCH", "SUCCEEDED", "VALID_LIST"),
        ("DETAIL_FETCH", "SUCCEEDED", "NOT_FOUND"),
    ]
    jobs = _jobs(db)
    assert [j["title"] for j in jobs] == ["Ghost Role"]
    assert jobs[0]["origin_job_id"] == GHOST
    closure = _evidence(db, kind="REVIEW", ref_like="closure://%")
    assert len(closure) == 1
    detail = json.loads(closure[0]["detail_json"])
    assert detail["reason"] == "DETAIL_CLOSURE_OR_MISSING"
    assert detail["classification"] == "NOT_FOUND"
    assert detail["target_reference"] == GHOST
    assert detail["absence_authority"] is False
    assert detail["status_code"] == 404
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 1
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1


def test_detail_identity_mismatch_is_refused_and_recorded(db, server):
    """A detail body for another posting id is never accepted as this one's."""
    run_id, _ = _run_board(db, server, board="idmismatch")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    details = [r for r in _requests(db, run_id) if r["request_type"] == "DETAIL_FETCH"]
    assert len(details) == 3
    for detail in details:
        assert detail["last_failure_kind"] == "INVALID_JOB_RECORD"
    assert len(_jobs(db)) == 3
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    assert "Some Other Role" not in [j["title"] for j in _jobs(db)]
    for detail in details:
        parse = db.conn.execute(
            "SELECT failure_json, closure_evidence_json FROM parse_attempts WHERE request_id = ?",
            (detail["id"],),
        ).fetchone()
        failure = json.loads(parse["failure_json"])
        assert failure["kind"] == "INVALID_JOB_RECORD"
        assert failure["retryable"] is False
        assert json.loads(parse["closure_evidence_json"])
    cov = _coverage(db)[0]
    # the listing itself was complete and terminal; contradictory DETAIL
    # responses degrade the run, not the already-proven listing set
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["pages_completed"] == 1
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_sources WHERE presence_state != 'ACTIVE'"
    ).fetchone()[0] == 0


def test_rate_limited_site_is_partial_and_never_terminal(db, server):
    """A 429 is a typed page class, not a parser failure and not an empty site."""
    run_id, _ = _run_board(db, server, board="ratelimited")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert requests[0]["page_class"] == "RATE_LIMITED"
    assert _jobs(db) == []
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert len(_evidence(db, kind="PAGE_VALIDITY")) == 1


def test_challenge_page_site_is_partial_and_keeps_evidence(db, server):
    run_id, _ = _run_board(db, server, board="challenge")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert requests[0]["page_class"] == "CHALLENGE_PAGE"
    assert _jobs(db) == []
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"


def test_changed_provider_template_is_a_failure_not_an_empty_site(db, server):
    """A renamed payload shape must never read as "no jobs here"."""
    run_id, _ = _run_board(db, server, board="changedtemplate")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert _jobs(db) == []
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert parse is not None
    assert json.loads(parse["failure_json"])["kind"] == "PARSE_MARKER_MISSING"
    assert requests[0]["last_failure_kind"] == "PARSE_MARKER_MISSING"
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0


def test_malformed_body_never_reaches_the_parser(db, server):
    run_id, _ = _run_board(db, server, board="malformed")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert requests[0]["page_class"] == "UNEXPECTED_CONTENT"
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert _coverage(db)[0]["terminal_enumeration_proven"] == 0


def test_all_items_invalid_is_a_parse_failure_not_a_success(db, server):
    run_id, _ = _run_board(db, server, board="missingfields", config={"detail_fetch": False})
    assert execute_run(db.conn, run_id) == "PARTIAL"

    assert _jobs(db) == []
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert json.loads(parse["failure_json"])["kind"] == "PARSE_MARKER_MISSING"
    review = json.loads(parse["review_evidence_json"])
    assert len(review) == 3
    assert any("passwd" in json.dumps(item) for item in review)
    assert db.conn.execute("SELECT COUNT(*) FROM scrape_requests").fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE request_type = 'DETAIL_FETCH'"
    ).fetchone()[0] == 0
    assert _coverage(db)[0]["terminal_enumeration_proven"] == 0


# ---------------------------------------------------------------------------
# Host security boundaries hold end to end
# ---------------------------------------------------------------------------


def test_hostile_content_links_never_become_fetches_or_apply_links(db, server):
    """``javascript:``/``data:`` ``hostedUrl``/``applyUrl`` and body links are
    refused: fetch targets came from the pinned site token plus validated ids,
    the apply link comes from the endpoint table, the cleaned description
    carries no payload."""
    run_id, _ = _run_board(db, server, board="acme")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    attempts = db.conn.execute("SELECT * FROM fetch_attempts").fetchall()
    assert len(attempts) == 4
    for attempt in attempts:
        assert attempt["requested_url"].startswith(
            f"http://127.0.0.1:{server.server_address[1]}/v0/postings/acme"
        )
        assert attempt["status_code"] == 200
        assert json.loads(attempt["security_policy_json"])

    remote = db.conn.execute("SELECT * FROM job_sources WHERE source_job_id = ?", (ID3,)).fetchone()
    # the listing's reviewed hostedUrl is kept; the detail's hostile one is not
    assert remote["canonical_job_url"] == f"{HOSTED}/{ID3}"
    assert best_application_url(db.conn, remote["job_id"]) == f"{HOSTED}/{ID3}"

    job = db.conn.execute("SELECT * FROM jobs WHERE id = ?", (remote["job_id"],)).fetchone()
    for text in (job["description_md"] or "", job["description_text"] or ""):
        assert "javascript:" not in text.lower()
        assert "data:text/html" not in text.lower()
        assert "<script" not in text.lower()
        assert "inline script residue" not in text
    reasons = " ".join(row["review_evidence_json"] for row in db.conn.execute(
        "SELECT review_evidence_json FROM parse_attempts WHERE review_evidence_json != '[]'"
    ).fetchall())
    assert "UNSAFE_URL_REFUSED" in reasons

    for table, column in (
        ("job_sources", "canonical_job_url"),
        ("job_sources", "application_url"),
        ("job_sources", "raw_source_url"),
        ("job_observations", "raw_url"),
        ("job_observations", "canonical_url_candidate"),
        ("job_observations", "application_url_candidate"),
    ):
        for row in db.conn.execute(f"SELECT {column} AS value FROM {table}").fetchall():
            value = row["value"] or ""
            assert not value.lower().startswith(("javascript:", "data:", "file:")), value


def test_only_the_pinned_site_and_origin_are_reachable(db, server):
    """Content cannot re-point a fetch: the site token is config, not payload."""
    run_id, provisioned = _run_board(db, server, board="acme")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    revision = db.conn.execute(
        "SELECT config_json FROM source_adapter_binding_revisions WHERE id = ?",
        (provisioned.binding_revision_id,),
    ).fetchone()
    config = json.loads(revision["config_json"])
    assert config["board"] == "acme"
    assert config["api_base_url"] == f"http://127.0.0.1:{server.server_address[1]}"
    urls = _fetch_urls(db)
    assert len(urls) == 4
    for url in urls:
        assert "/v0/postings/acme" in url
        assert "api.lever.co" not in url
    detail_ids = sorted(url.rsplit("/", 1)[-1] for url in urls if "?" not in url)
    assert detail_ids == sorted([ID1, ID2, ID3])


def test_host_policy_widens_a_reviewed_lever_entry_host_to_the_api_host_only():
    """Provider API authority comes from reviewed host code, never config."""
    from jobscraper.adapters.lever import LeverConfig
    from jobscraper.pipeline.driver import source_policy

    policy = source_policy({"entry_url": "https://jobs.lever.co/acme"}, adapter_id="lever")
    assert policy.allowed_hosts == frozenset({"jobs.lever.co", "api.lever.co"})
    # a Greenhouse entry host under the Lever adapter gets no widening at all
    crossed = source_policy({"entry_url": "https://boards.greenhouse.io/acme"}, adapter_id="lever")
    assert crossed.allowed_hosts == frozenset({"boards.greenhouse.io"})
    # an unreviewed entry host is pinned to itself
    other = source_policy({"entry_url": "https://careers.example/jobs"}, adapter_id="lever")
    assert other.allowed_hosts == frozenset({"careers.example"})
    # an operator-pinned alternate origin is syntactically valid to the adapter
    # but is not network authority unless host policy separately approves it
    config = LeverConfig(board="acme", api_base_url="https://evil.example")
    assert config.api_base_url == "https://evil.example"
    assert "evil.example" not in policy.allowed_hosts


# ---------------------------------------------------------------------------
# Re-driving, restarts and re-observation (RUN-01, ACQ-04)
# ---------------------------------------------------------------------------


def test_redriving_a_finished_run_is_an_idempotent_no_op(db, server):
    run_id, _ = _run_board(db, server, board="acme")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    def snapshot():
        return {
            "requests": db.conn.execute("SELECT COUNT(*) FROM scrape_requests").fetchone()[0],
            "observations": db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0],
            "jobs": db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "presences": db.conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0],
            "fetches": db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0],
            "coverage": db.conn.execute("SELECT COUNT(*) FROM enumeration_coverage").fetchone()[0],
            "status": db.conn.execute(
                "SELECT status FROM scrape_runs WHERE id = ?", (run_id,)
            ).fetchone()["status"],
            "outcome": _outcome(db, run_id),
        }

    before = snapshot()
    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    assert snapshot() == before


def test_a_second_run_reobserves_without_duplicating_jobs(db, server):
    first_run, provisioned = _run_board(db, server, board="acme")
    assert execute_run(db.conn, first_run) == "SUCCEEDED"
    first_jobs = {j["id"] for j in _jobs(db)}
    first_presence_ids = {p["id"] for p in _presences(db)}

    second_run = _start_run(db, provisioned, board="acme")
    assert execute_run(db.conn, second_run) == "SUCCEEDED"

    assert {j["id"] for j in _jobs(db)} == first_jobs
    assert len(_jobs(db)) == 3
    assert {p["id"] for p in _presences(db)} == first_presence_ids
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 12
    generations = _coverage(db)
    assert len(generations) == 2
    assert {g["generation_key"] for g in generations} == {f"run-{first_run}", f"run-{second_run}"}
    assert all(g["completion_state"] == "COMPLETE" for g in generations)
    second = db.conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (second_run,)).fetchone()
    assert second["status"] == "SUCCEEDED"
    assert second["jobs_saved"] + second["jobs_updated"] == 3
    assert all(j["listing_status"] == "ACTIVE" for j in _jobs(db))
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_an_open_detail_child_blocks_terminalization_until_drained(db, server):
    from jobscraper.pipeline import driver as driver_module

    provisioned = _provision(db, server, board="acme")
    run_id = _start_run(db, provisioned, board="acme")
    original = driver_module.MAX_DETAIL_REQUESTS_PER_RUN
    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0
    try:
        assert execute_run(db.conn, run_id) == "PARTIAL"
    finally:
        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"] + ["DETAIL_FETCH"] * 3
    assert [r["status"] for r in requests] == ["SUCCEEDED"] + ["PENDING"] * 3
    assert len(_jobs(db)) == 3
    cov = _coverage(db)[0]
    # listing identity is sufficient: the membership proof is COMPLETE while
    # accepted DETAIL work keeps the *run* partial
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["pages_completed"] == 1
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"

    second_run = _start_run(db, provisioned, board="acme")
    assert execute_run(db.conn, second_run) == "SUCCEEDED"
    second_requests = _requests(db, second_run)
    assert sorted(r["request_type"] for r in second_requests) == ["DETAIL_FETCH"] * 3 + ["LIST_FETCH"]
    assert all(r["status"] == "SUCCEEDED" for r in second_requests)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE run_id = ? AND status = 'PENDING'", (run_id,)
    ).fetchone()[0] == 3
    generations = _coverage(db)
    assert len(generations) == 2
    assert generations[1]["completion_state"] == "COMPLETE"


def test_restart_recovery_drains_the_same_run_without_duplicates(db, server):
    """A run interrupted after the listing commit resumes from durable state:
    the listing is not re-fetched, the same coverage generation is reused,
    and no observation, job, presence or company is duplicated."""
    from jobscraper.pipeline import driver as driver_module

    provisioned = _provision(db, server, board="acme")
    run_id = _start_run(db, provisioned, board="acme")
    original = driver_module.MAX_DETAIL_REQUESTS_PER_RUN
    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0
    try:
        assert execute_run(db.conn, run_id) == "PARTIAL"
        observations = db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0]
        jobs = {job["id"] for job in _jobs(db)}
        companies = db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        coverage_id = _coverage(db)[0]["id"]
    finally:
        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original
    assert observations == 3
    assert len(jobs) == 3

    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [r["status"] for r in requests] == ["SUCCEEDED"] * 4
    assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 4
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == observations + 3
    assert {job["id"] for job in _jobs(db)} == jobs
    assert len(_presences(db)) == 3
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == companies
    generations = _coverage(db)
    assert len(generations) == 1
    assert generations[0]["id"] == coverage_id
    assert generations[0]["completion_state"] == "COMPLETE"
    assert generations[0]["terminal_enumeration_proven"] == 1
    assert generations[0]["pages_completed"] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_sources WHERE presence_state != 'ACTIVE'"
    ).fetchone()[0] == 0
    assert _outcome(db, run_id) == "SATISFIED"


def test_a_crash_between_pages_resumes_the_pending_page_from_the_cursor(db, server, monkeypatch):
    """The process dies after page 1 committed (page 2 enqueued, cursor saved):
    the resumed pass plans page 2 from the durable cursor, never fetches page 1
    again, and finishes the *same* coverage generation with every identity."""
    from jobscraper.pipeline import driver as driver_module

    provisioned = _provision(db, server, board="paged", config={"page_size": 2})
    run_id = _start_run(db, provisioned, board="paged")

    real_claim = driver_module.claim_next_request
    claims = {"n": 0}

    def dying_claim(*args, **kwargs):
        claims["n"] += 1
        if claims["n"] == 2:
            raise RuntimeError("simulated process death between pages")
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(driver_module, "claim_next_request", dying_claim)
    with pytest.raises(RuntimeError):
        execute_run(db.conn, run_id)
    monkeypatch.setattr(driver_module, "claim_next_request", real_claim)

    # durable state after the crash: page 1 done, page 2 pending, cursor saved,
    # generation open (not finalized), run still RUNNING, no outcome claimed
    requests = _requests(db, run_id)
    assert [(r["request_type"], r["status"]) for r in requests] == [
        ("LIST_FETCH", "SUCCEEDED"), ("LIST_FETCH", "PENDING"),
    ]
    # the pending page's identity was derived from the durable cursor state
    # (RUN-05): the same plan + state must hash to the same unique key
    from jobscraper.runtime.requests import request_unique_key

    cursor_state = db.conn.execute("SELECT state_json FROM crawl_cursors").fetchone()[0]
    assert json.loads(cursor_state)["skip"] == 2
    assert requests[1]["request_unique_key"] == request_unique_key(
        run_source_plan_id=requests[1]["run_source_plan_id"],
        request_type="LIST_FETCH",
        target_identity=_fetch_urls(db)[0],
        strategy=requests[1]["strategy"],
        logical_key=cursor_state,
    )
    generations = _coverage(db)
    assert len(generations) == 1
    assert generations[0]["completion_state"] is None
    assert generations[0]["finalized_at"] is None
    assert db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id = ?", (run_id,)
    ).fetchone()["status"] == "RUNNING"
    assert _outcome(db, run_id) is None
    assert len(_jobs(db)) == 2

    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    assert [u.rsplit("?", 1)[1] for u in _fetch_urls(db)] == [
        "mode=json&skip=0&limit=2", "mode=json&skip=2&limit=2",
    ]
    assert [j["title"] for j in _jobs(db)] == ["Backend Engineer", "Platform Engineer", "Senior Data Engineer"]
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    generations = _coverage(db)
    assert len(generations) == 1
    assert generations[0]["completion_state"] == "COMPLETE"
    assert generations[0]["terminal_enumeration_proven"] == 1
    contributing = db.conn.execute(
        "SELECT COUNT(*) FROM coverage_contributing_request WHERE coverage_id = ?",
        (generations[0]["id"],),
    ).fetchone()[0]
    assert contributing == 2
    seen = {row[0] for row in db.conn.execute(
        "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id = ?",
        (generations[0]["id"],),
    )}
    assert seen == {ID1, ID2, ID3}
    # nothing seen on page 1 was demoted by the resumed walk
    assert all(p["presence_state"] == "ACTIVE" for p in _presences(db))
    assert _outcome(db, run_id) == "SATISFIED"


def test_a_crash_before_the_outcome_commit_is_closed_from_durable_evidence(db, server):
    run_id, _ = _run_board(db, server, board="acme")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    generations = len(_coverage(db))
    observations = db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0]

    db.conn.execute("UPDATE run_source_plans SET group_outcome = NULL WHERE run_id = ?", (run_id,))
    db.conn.commit()

    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    assert _outcome(db, run_id) == "SATISFIED"
    assert len(_coverage(db)) == generations
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == observations
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE status = 'PENDING'"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The service surface: Inbox-visible jobs with a safe direct apply link
# ---------------------------------------------------------------------------


@pytest.fixture()
def service(tmp_path, server):
    """The Slice-1 service app over a database this suite provisions."""
    from jobscraper.config import AppConfig
    from jobscraper.paths import build_app_paths, ensure_app_directories
    from jobscraper.security.install_secret import load_or_create_install_secret
    from jobscraper.service.app import create_service_app

    root = tmp_path / "service-root"
    paths = build_app_paths(root)
    ensure_app_directories(paths)
    secret = load_or_create_install_secret(paths)
    database = Database(paths.database_file)
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    provision_search(database.conn, now=NOW)
    app, state = create_service_app(AppConfig(data_root=root), database, port=8766, secret=secret)
    yield {"app": app, "state": state, "db": database}
    database.close()


def _client(service):
    from fastapi.testclient import TestClient

    client = TestClient(service["app"], base_url="http://127.0.0.1:8766")
    state = service["state"]
    session_id, csrf = state.sessions.create(state.instance_id)
    client.cookies.set("wjs_session", session_id)
    client.cookies.set("wjs_csrf", csrf)
    client.headers.update(
        {"X-CSRF-Token": csrf, "host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}
    )
    return client


def test_a_provisioned_site_runs_through_the_service_and_reaches_the_inbox(service, server):
    """The product path, end to end: provision → ``POST /api/runs`` → Inbox."""
    db = service["db"]
    provisioned = _provision(db, server, board="acme", config=dict(ACME_CONFIG))
    client = _client(service)

    profile = client.post(
        "/api/profiles",
        json={
            "name": "Berlin Python",
            "keywords": ["python", "engineer"],
            "eligible_countries": ["DE", "FR"],
            "remote_rules": {"remote_ok": True},
            "min_score_inbox": 0,
        },
    ).json()

    run = client.post("/api/runs", json={"profile_id": profile["id"]})
    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "SUCCEEDED"
    assert body["jobs_saved"] == 3
    assert body["requests_failed"] == 0

    plan = db.conn.execute(
        "SELECT adapter_id, adapter_version, binding_revision_id, strategy"
        " FROM run_source_plans WHERE run_id = ?",
        (body["run_id"],),
    ).fetchone()
    assert plan["adapter_id"] == "lever"
    assert plan["binding_revision_id"] == provisioned.binding_revision_id
    assert plan["strategy"] == "PROVIDER_NATIVE"

    inbox = client.get(f"/api/inbox?profile_id={profile['id']}").json()["inbox"]
    assert {item["title"] for item in inbox} == {
        "Backend Engineer", "Platform Engineer", "Senior Data Engineer",
    }
    assert all(item["score"] is not None and item["breakdown"] for item in inbox)

    by_title = {item["title"]: item["job_id"] for item in inbox}
    detail = client.get(f"/api/jobs/{by_title['Backend Engineer']}").json()
    assert detail["job"]["title"] == "Backend Engineer"
    assert len(detail["sources"]) == 1
    assert detail["apply_url"] == f"{HOSTED}/{ID1}"
    assert detail["sources"][0]["application_url"] == f"{HOSTED}/{ID1}"

    hostile = client.get(f"/api/jobs/{by_title['Senior Data Engineer']}").json()
    assert hostile["apply_url"] == f"{HOSTED}/{ID3}"
    assert "javascript:" not in json.dumps(hostile).lower()
    assert "data:text/html" not in json.dumps(hostile).lower()

    found = search_jobs(db.conn, query="senior data engineer")
    assert found.mode == SEARCH_MODE_FTS5
    assert found.total == 1
