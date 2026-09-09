"""S2.7 end-to-end: a provider-native Ashby board run through the spine.

Proves the whole durable path for the third built-in provider adapter, with
the host doing every authoritative step:

``Source`` → ``SourceAdapterBinding`` → immutable ``BindingRevision`` (config
pinned) → ``RunSourcePlan`` → host-owned execution (destination policy, fetch
outside any transaction) → ``ResultEnvelope`` → validity gate → adapter parse →
fenced ``Observation`` ingest → canonical pipeline (normalize, clean, dedupe,
provenance, companies, locations) → obligations → search index.

Ashby-specific truths pinned here (not copied from Greenhouse/Lever):

* the board API is a **single full document** with no pagination and no
  declared total — one recognized response is the whole membership, so a run
  is exactly one ``LIST_FETCH``, there is never a cursor, and coverage is
  ``AUTHORITATIVE_FULL_SOURCE`` (there is no detail endpoint, so no
  ``DETAIL_FETCH`` request may ever exist);
* closure has no per-job 404 shape: a posting that leaves the board is
  judged **UNCERTAIN by absence** when a later COMPLETE generation does not
  see it (03 §40, RUN-13);
* ``isListed: false`` postings are evidence, never members;
* a rejected member or an unrecognized ``apiVersion`` is a durable PARTIAL:
  observations persist, absence authority does not;
* ``publishedAt`` is the provider-stated publication time; compensation
  arrives inline when the board is asked for it.

Fixtures are the deterministic files in ``tests/fixtures/ashby/`` served over
loopback by a local HTTP server, so the run is reproducible and offline.
Between runs of the *same* binding, a test may swap what a board serves
(how provider content actually changes over time) — each server instance is
fresh per test, so this stays deterministic.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

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

NOW = "2026-09-09T09:00:00.000000Z"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ashby"

_JSON = "application/json"

ID1 = "5a1b2c3d-0000-4000-8000-000000005001"
ID2 = "5a1b2c3d-0000-4000-8000-000000005002"
ID3 = "5a1b2c3d-0000-4000-8000-000000005003"
UNLISTED = "5a1b2c3d-0000-4000-8000-000000005009"
HOSTED = "https://jobs.ashbyhq.com/acme"

#: board token -> (fixture, http status, content type) for the board endpoint.
#: Copied per server instance; a test may swap an entry between runs to model
#: provider content changing over time (each test gets a fresh server).
_LIST_ROUTES = {
    "acme": ("board_jobs.json", 200, _JSON),
    "fading": ("board_jobs.json", 200, _JSON),
    "healing": ("board_jobs_rejected_member.json", 200, _JSON),
    "emptyboard": ("board_jobs_empty.json", 200, _JSON),
    "unlisted": ("board_jobs_unlisted.json", 200, _JSON),
    "onlyunlisted": ("board_jobs_only_unlisted.json", 200, _JSON),
    "rejectedmember": ("board_jobs_rejected_member.json", 200, _JSON),
    "apiv2": ("board_jobs_api_v2.json", 200, _JSON),
    "changedtemplate": ("board_jobs_changed_template.json", 200, _JSON),
    "missingfields": ("board_jobs_missing_required_fields.json", 200, _JSON),
    "malformed": ("board_jobs_malformed.json", 200, _JSON),
    "ratelimited": ("rate_limited.429.json", 429, _JSON),
    "challenge": ("challenge.403.html", 403, "text/html; charset=utf-8"),
}

_LIST_PATH = re.compile(r"^/posting-api/job-board/(?P<board>[a-z0-9_-]+)$")


class _AshbyHandler(http.server.BaseHTTPRequestHandler):
    """A loopback stand-in for the public Ashby job posting API.

    There is exactly one endpoint; any other path (including the per-job
    shape ``/job/{id}``, which the real API answers 401) is a plain 404.
    """

    def do_GET(self):  # noqa: N802 - http.server interface
        parts = urlsplit(self.path)
        listing = _LIST_PATH.fullmatch(parts.path)
        if listing is not None:
            board = listing.group("board")
            fixture, status, content_type = self.server.routes.get(
                board, (None, 404, _JSON)
            )
            if fixture is not None:
                self._serve((FIXTURES / fixture).read_bytes(), status, content_type)
            else:
                self._serve(b"Not Found", 404, "text/plain; charset=utf-8")
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
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AshbyHandler)
    srv.routes = dict(_LIST_ROUTES)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def db(tmp_path, server):
    database = Database(tmp_path / "ashby-e2e.db")
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
    definition = ensure_builtin_adapter_definition(conn, "ashby", now=NOW)
    return provision_source_and_binding(
        conn,
        display_name=f"{board} postings",
        source_family=source_family,
        entry_url=f"http://127.0.0.1:{port}/{board}",
        canonical_host="jobs.ashbyhq.com",
        adapter_id="ashby",
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
        source_plan_group_id="grp-ashby",
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
        target_identity=f"http://127.0.0.1/posting-api/job-board/{board}",
        logical_key='{"page": 1}',
        strategy=rev["strategy"],
        execution_class=rev["execution_class"],
        now=now,
    )
    return run_id


#: what an operator records about the employer when pinning a board: the
#: posting API itself never states the company name, so it is binding config.
ACME_CONFIG = {
    "company_name": "Acme Fixtures",
    "careers_url": "https://acme.example/careers",
}


def _run_board(db, server, *, board, config=None, now=NOW, profile=True):
    """Provision + run one board end to end; returns (run_id, provisioned)."""
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


def test_board_run_reaches_canonical_jobs_through_the_whole_spine(db, server):
    """One Ashby board document becomes canonical jobs — in one request."""
    run_id, provisioned = _run_board(db, server, board="acme")

    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    # ---- requests: exactly one enumeration; no detail surface may exist
    requests = _requests(db, run_id)
    assert [(r["request_type"], r["status"], r["page_class"]) for r in requests] == [
        ("LIST_FETCH", "SUCCEEDED", "VALID_LIST"),
    ]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE request_type = 'DETAIL_FETCH'"
    ).fetchone()[0] == 0
    listing = requests[0]

    # ---- canonical jobs: one row per posted job, one presence per source
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
        assert observation["adapter_id"] == "ashby"
        assert observation["adapter_version"] == "1.0.0"
        assert observation["strategy"] == "PROVIDER_NATIVE"
        assert observation["execution_class"] == "HTTP"
        assert observation["fetch_attempt_id"] and observation["parse_attempt_id"]

    # ---- 02 §32 origin resolution: the ATS identity, resolved not guessed
    company_ids = {job["company_id"] for job in jobs}
    assert len(company_ids) == 1 and None not in company_ids
    company = db.conn.execute("SELECT * FROM companies WHERE id = ?", (company_ids.pop(),)).fetchone()
    assert company["name"] == "Acme Fixtures"  # binding config, never the token
    assert company["ats_provider"] == "ASHBY"
    assert company["ats_board"] == "acme"
    assert company["domain"] == "acme.example"
    identifiers = db.conn.execute(
        "SELECT kind, value FROM company_identifiers WHERE company_id = ?", (company["id"],)
    ).fetchall()
    assert {row["value"] for row in identifiers} >= {"ASHBY/acme", "acme.example"}
    for job in jobs:
        assert job["origin_provider"] == "ASHBY"
        assert job["origin_board"] == "acme"
        assert job["origin_job_id"] in {ID1, ID2, ID3}
    resolved = db.conn.execute(
        "SELECT origin_provider, origin_board, origin_job_id,"
        " origin_resolution_confidence, origin_resolution_evidence_json,"
        " source_quality_class, canonical_job_url, application_url"
        " FROM job_sources WHERE source_job_id = ?",
        (ID1,),
    ).fetchone()
    assert resolved["origin_provider"] == "ASHBY"
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
    assert backend["employment_type"] == "FULL_TIME"  # provider "FullTime"
    # Ashby states publishedAt (last publication) — a real posted_at
    assert backend["posted_at"] == "2026-06-01T09:30:00.000000Z"
    assert backend["listing_status"] == "ACTIVE"
    assert backend["description_text"] and "local-first" in backend["description_text"]
    assert "Salary band" in backend["description_text"]
    # inline compensation: the scrapeable summary is the salary signal and is
    # preserved verbatim as evidence.  The host parser expands the K-suffix on
    # *each* endpoint of a provider range (DF-1 closed in S2.9), so the max no
    # longer collapses to the min; the adapter never rewrites the provider's
    # string.
    assert backend["salary_original_text"] == "$150K - $210K"
    assert backend["salary_currency"] == "USD"
    assert backend["salary_min"] == 150000
    assert backend["salary_max"] == 210000
    backend_locations = db.conn.execute(
        "SELECT raw_text, city, region, country, remote FROM job_locations"
        " WHERE job_id = ?",
        (backend["id"],),
    ).fetchall()
    assert [(r["city"], r["country"], r["remote"]) for r in backend_locations] == [("Berlin", "DE", 0)]

    # the hybrid PartTime posting keeps every secondary location the provider
    # stated, none of them flagged remote
    platform = jobs[1]
    assert platform["employment_type"] == "PART_TIME"  # provider "PartTime"
    platform_locations = db.conn.execute(
        "SELECT raw_text, city, country, remote FROM job_locations WHERE job_id = ?",
        (platform["id"],),
    ).fetchall()
    assert {(r["city"], r["country"], r["remote"]) for r in platform_locations} == {
        ("Berlin", "DE", 0), ("Paris", "FR", 0), ("Lisbon", "PT", 0),
    }

    # isRemote/workplaceType Remote → every location row remote, no invention
    remote_job = db.conn.execute(
        "SELECT raw_text, remote FROM job_locations WHERE job_id = ?", (jobs[2]["id"],)
    ).fetchall()
    assert remote_job and all(row["remote"] == 1 for row in remote_job)
    assert jobs[2]["remote_mode"] == "REMOTE"
    # a scoped "Remote - European Union" is not evidence of unrestricted scope
    assert jobs[2]["remote_worldwide"] == 0

    # ---- coverage: one document is terminal enumeration proof
    rows = _coverage(db)
    assert len(rows) == 1
    cov = rows[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["absence_inference_allowed"] == 1
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
    assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    assert len(_evidence(db, kind="RESULT_ENVELOPE")) == 1
    assert len(_evidence(db, kind="PAGE_VALIDITY")) == 1
    assert len(_evidence(db, kind="ORIGIN_RESOLUTION")) == 3
    # field evidence names the Ashby locators, publishedAt as posted_at
    locators = {row["locator_value"] for row in db.conn.execute(
        "SELECT locator_value FROM field_evidence"
    ).fetchall()}
    assert {"id", "title", "publishedAt", "isRemote", "workplaceType", "employmentType",
            "location+secondaryLocations", "descriptionHtml", "company_name",
            "compensation.scrapeableCompensationSalarySummary"} <= locators

    # ---- run accounting is derived from evidence, not asserted by the adapter
    run = db.conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "SUCCEEDED"
    assert run["jobs_discovered"] == 3
    assert run["requests_total"] >= 1
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


def test_empty_board_is_a_successful_terminal_enumeration(db, server):
    """A recognized empty board is authority that the board lists no jobs.

    The validity classifier reads ``{"jobs": []}`` as an EMPTY page; the
    *adapter* recognizes SUCCESS_EMPTY, which the host records as a terminal
    enumeration (ACQ-03).
    """
    run_id, _ = _run_board(db, server, board="emptyboard")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert requests[0]["page_class"] == "EMPTY"
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert parse["outcome_kind"] == "SUCCESS_EMPTY"
    assert _jobs(db) == []
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert _outcome(db, run_id) == "SATISFIED"


# ---------------------------------------------------------------------------
# Absence is the only closure shape (03 §40, RUN-13)
# ---------------------------------------------------------------------------


def test_a_posting_missing_from_a_later_complete_generation_is_uncertain(db, server):
    """Closure for Ashby is absence: the posting stops appearing in the
    single board document.  A later COMPLETE generation that does not see it
    transitions the presence to UNCERTAIN — with the coverage generation that
    proved it named, never the failed or partial one."""
    provisioned = _provision(db, server, board="fading", config=dict(ACME_CONFIG))
    first = _start_run(db, provisioned, board="fading")
    assert execute_run(db.conn, first) == "SUCCEEDED"
    assert {p["source_job_id"] for p in _presences(db)} == {ID1, ID2, ID3}

    # the provider's document changes: the third posting is unpublished
    server.routes["fading"] = ("board_jobs_removed.json", 200, _JSON)
    second = _start_run(db, provisioned, board="fading")
    assert execute_run(db.conn, second) == "SUCCEEDED"

    generations = _coverage(db)
    assert [g["completion_state"] for g in generations] == ["COMPLETE", "COMPLETE"]
    assert all(g["absence_inference_allowed"] == 1 for g in generations)
    ghost = db.conn.execute(
        "SELECT presence_state, last_absence_coverage_id FROM job_sources"
        " WHERE source_job_id = ?",
        (ID3,),
    ).fetchone()
    assert ghost["presence_state"] == "UNCERTAIN"
    assert ghost["last_absence_coverage_id"] == generations[1]["id"]
    # the still-listed postings were re-observed and stay ACTIVE
    active = {p["source_job_id"] for p in _presences(db) if p["presence_state"] == "ACTIVE"}
    assert active == {ID1, ID2}
    assert _outcome(db, second) == "SATISFIED"


def test_a_partial_generation_ages_nothing_then_a_clean_one_judges(db, server):
    """The host-enforced invariant through the real adapter: a generation
    containing a rejected member finalizes PARTIAL (durable degradation), so
    only the *next clean* COMPLETE generation may judge a disappeared
    posting."""
    provisioned = _provision(db, server, board="healing", config=dict(ACME_CONFIG))
    first = _start_run(db, provisioned, board="healing")
    assert execute_run(db.conn, first) == "PARTIAL"

    # ---- the PARTIAL document: good observations persist, absence is refused
    assert [j["title"] for j in _jobs(db)] == ["Backend Engineer", "Platform Engineer"]
    generations = _coverage(db)
    assert len(generations) == 1
    degraded = generations[0]
    assert degraded["completion_state"] == "PARTIAL"
    assert degraded["absence_inference_allowed"] == 0
    assert degraded["terminal_enumeration_proven"] == 0
    assert _outcome(db, first) == "SATISFIED_PARTIAL"

    # ---- the provider fixes the document: the next generation is clean
    server.routes["healing"] = ("board_jobs_removed.json", 200, _JSON)
    second = _start_run(db, provisioned, board="healing")
    assert execute_run(db.conn, second) == "SUCCEEDED"

    generations = _coverage(db)
    assert [g["completion_state"] for g in generations] == ["PARTIAL", "COMPLETE"]
    assert [g["absence_inference_allowed"] for g in generations] == [0, 1]
    assert all(p["presence_state"] == "ACTIVE" for p in _presences(db))
    assert _outcome(db, second) == "SATISFIED"


# ---------------------------------------------------------------------------
# The declared health capability through the real dispatch path
# ---------------------------------------------------------------------------


def test_a_health_probe_rides_the_same_pins_and_leaves_coverage_untouched(db, server):
    """``health`` is a declared capability: the driver dispatches
    ``SOURCE_HEALTH_CHECK`` → ``AdapterTaskKind.HEALTH`` through the same
    typed request machinery.  The probe plans the *same* pinned board
    document (never a task-supplied URL), parses recognition-only, emits no
    observations, and is never a coverage-contributing request — so it can
    never grant or break absence authority."""
    provisioned = _provision(db, server, board="acme", config=dict(ACME_CONFIG))
    run_id = _start_run(db, provisioned, board="acme")
    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=db.conn.execute(
            "SELECT id FROM run_source_plans WHERE run_id = ?", (run_id,)
        ).fetchone()["id"],
        source_id=provisioned.source_id,
        binding_id=provisioned.binding_id,
        request_type="SOURCE_HEALTH_CHECK",
        target_identity="ashby://acme/health",
        logical_key='{"probe": "health"}',
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        now=NOW,
    )

    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = sorted(_requests(db, run_id), key=lambda r: r["request_type"])
    assert [(r["request_type"], r["status"], r["page_class"]) for r in requests] == [
        ("LIST_FETCH", "SUCCEEDED", "VALID_LIST"),
        ("SOURCE_HEALTH_CHECK", "SUCCEEDED", "VALID_LIST"),
    ]
    health = requests[1]
    # the probe fetched exactly the pinned board document, from config only
    health_fetch = db.conn.execute(
        "SELECT requested_url FROM fetch_attempts WHERE request_id = ?", (health["id"],)
    ).fetchone()
    assert health_fetch
    assert health_fetch["requested_url"] == (
        f"http://127.0.0.1:{server.server_address[1]}"
        "/posting-api/job-board/acme?includeCompensation=true"
    )
    parse_attempts = db.conn.execute(
        "SELECT * FROM parse_attempts ORDER BY parsed_at, id"
    ).fetchall()
    assert len(parse_attempts) == 2
    probe = next(p for p in parse_attempts if p["outcome_kind"] == "SUCCESS_EMPTY")
    probe_review = json.loads(probe["review_evidence_json"])
    recognized = [item for item in probe_review if item["reason"] == "HEALTH_PROBE_RECOGNIZED"]
    assert recognized and recognized[0]["postings_total"] == 3 and recognized[0]["listed_members"] == 3
    # recognition-only means exactly that: no observations from the probe
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    probe_parse_attempts = db.conn.execute(
        "SELECT COUNT(*) FROM job_observations WHERE parse_attempt_id = ?", (probe["id"],)
    ).fetchone()[0]
    assert probe_parse_attempts == 0
    # and it is never a page in the enumeration proof
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["absence_inference_allowed"] == 1
    contributing = {row[0] for row in db.conn.execute(
        "SELECT request_id FROM coverage_contributing_request WHERE coverage_id = ?",
        (cov["id"],),
    )}
    listing_id = next(r["id"] for r in requests if r["request_type"] == "LIST_FETCH")
    assert contributing == {listing_id}
    assert _outcome(db, run_id) == "SATISFIED"


# ---------------------------------------------------------------------------
# Listing discipline: isListed is membership
# ---------------------------------------------------------------------------


def test_unlisted_postings_are_evidence_never_members(db, server):
    """``isListed: false`` means direct-link-only: not part of the public
    board.  It is excluded with typed review evidence; the listed member is
    observed; coverage of the *listed* board stays COMPLETE."""
    run_id, _ = _run_board(db, server, board="unlisted")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    assert [j["title"] for j in _jobs(db)] == ["Backend Engineer"]
    assert "Stealth Role" not in [j["title"] for j in _jobs(db)]
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_observations WHERE source_job_id = ?", (UNLISTED,)
    ).fetchone()[0] == 0
    parse = db.conn.execute("SELECT review_evidence_json FROM parse_attempts").fetchone()
    review = json.loads(parse["review_evidence_json"])
    exclusions = [item for item in review if item["reason"] == "UNLISTED_POSTING_EXCLUDED"]
    assert len(exclusions) == 1
    assert exclusions[0]["posting_id"] == UNLISTED
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    seen = {row[0] for row in db.conn.execute(
        "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id = ?",
        (cov["id"],),
    )}
    assert seen == {ID1}


def test_a_board_of_only_unlisted_postings_is_successfully_empty(db, server):
    """The listed set is genuinely empty: SUCCESS_EMPTY, terminal, and the
    exclusion is durable review evidence — never fabricated observations."""
    run_id, _ = _run_board(db, server, board="onlyunlisted")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    parse = db.conn.execute("SELECT outcome_kind, review_evidence_json FROM parse_attempts").fetchone()
    assert parse["outcome_kind"] == "SUCCESS_EMPTY"
    assert _jobs(db) == []
    review = json.loads(parse["review_evidence_json"])
    assert any(item["reason"] == "UNLISTED_POSTING_EXCLUDED" for item in review)
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert _outcome(db, run_id) == "SATISFIED"


# ---------------------------------------------------------------------------
# Coverage authority: PARTIAL and FAILURE never grant absence
# ---------------------------------------------------------------------------


def test_a_rejected_member_degrades_the_generation_durably(db, server):
    """One listed member without an id: PARTIAL, good observations persisted,
    the generation durably refuses absence inference (ACQ-03, RUN-13)."""
    run_id, _ = _run_board(db, server, board="rejectedmember")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    assert [j["title"] for j in _jobs(db)] == ["Backend Engineer", "Platform Engineer"]
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert parse["outcome_kind"] == "PARTIAL"
    review = json.loads(parse["review_evidence_json"])
    assert any(item["reason"] == "REQUIRED_FIELD_MISSING" for item in review)
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["absence_inference_allowed"] == 0
    assert cov["terminal_enumeration_proven"] == 0
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"
    # nothing was expired or closed on the strength of a partial run
    assert all(p["presence_state"] == "ACTIVE" for p in _presences(db))


def test_an_unrecognized_api_version_never_grants_absence_authority(db, server):
    """A parseable document stamped with a contract version the adapter was
    not reviewed against: jobs persist, but the generation is PARTIAL."""
    run_id, _ = _run_board(db, server, board="apiv2")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    assert [j["title"] for j in _jobs(db)] == ["Backend Engineer"]
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert parse["outcome_kind"] == "PARTIAL"
    review = json.loads(parse["review_evidence_json"])
    version_review = [item for item in review if item["reason"] == "UNRECOGNIZED_API_VERSION"]
    assert version_review and version_review[0]["api_version"] == "2"
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["absence_inference_allowed"] == 0
    assert cov["terminal_enumeration_proven"] == 0


def test_rate_limited_board_is_partial_and_never_terminal(db, server):
    """A 429 is a typed page class, not a parser failure and not an empty board."""
    run_id, _ = _run_board(db, server, board="ratelimited")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert requests[0]["page_class"] == "RATE_LIMITED"
    assert _jobs(db) == []
    # an invalid class never reaches the parser (ACQ-02)
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert len(_evidence(db, kind="PAGE_VALIDITY")) == 1


def test_challenge_page_board_is_partial_and_keeps_evidence(db, server):
    """A bot challenge is recorded as such; it proves nothing about membership."""
    run_id, _ = _run_board(db, server, board="challenge")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert requests[0]["page_class"] == "CHALLENGE_PAGE"
    assert _jobs(db) == []
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"


def test_changed_provider_template_is_a_failure_not_an_empty_board(db, server):
    """A renamed payload shape must never read as "no jobs here".

    This is the dangerous direction: treating a template change as an empty
    board would grant absence authority and age every posting.  The adapter
    reports PARSE_MARKER_MISSING instead, with its ACQ-09 evidence refs."""
    run_id, _ = _run_board(db, server, board="changedtemplate")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert _jobs(db) == []
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert parse is not None
    assert json.loads(parse["failure_json"])["kind"] == "PARSE_MARKER_MISSING"
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0


def test_malformed_body_never_reaches_the_parser(db, server):
    """Undecodable JSON is stopped by the host validity gate."""
    run_id, _ = _run_board(db, server, board="malformed")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert requests[0]["page_class"] == "UNEXPECTED_CONTENT"
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert _coverage(db)[0]["terminal_enumeration_proven"] == 0


def test_all_items_invalid_is_a_parse_failure_not_a_success(db, server):
    """A board whose every item lacks required identity yields no jobs.

    Includes the traversal-shaped id (``../../etc/passwd``) and a
    ``javascript:`` URL: both are refused and survive only as bounded review
    evidence — never identities, never fetch targets."""
    run_id, _ = _run_board(db, server, board="missingfields")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    assert _jobs(db) == []
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert json.loads(parse["failure_json"])["kind"] == "PARSE_MARKER_MISSING"
    review = json.loads(parse["review_evidence_json"])
    assert len(review) == 3
    # the traversal-shaped id survives only as bounded evidence, never as a
    # fetch target or an identity
    assert any("passwd" in json.dumps(item) for item in review)
    assert db.conn.execute("SELECT COUNT(*) FROM scrape_requests").fetchone()[0] == 1
    # no child work was ever dispatched from unusable references
    assert [r["request_type"] for r in _requests(db, run_id)] == ["LIST_FETCH"]
    assert _coverage(db)[0]["terminal_enumeration_proven"] == 0


# ---------------------------------------------------------------------------
# Host security boundaries hold end to end
# ---------------------------------------------------------------------------


def test_hostile_content_links_never_become_fetches_or_apply_links(db, server):
    """``javascript:``/``data:`` ``jobUrl``/``applyUrl`` and body links are
    refused: the only fetch is the pinned board document, the apply link
    comes from the endpoint table, the cleaned description carries no payload."""
    run_id, _ = _run_board(db, server, board="acme")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    attempts = db.conn.execute("SELECT * FROM fetch_attempts").fetchall()
    assert len(attempts) == 1
    assert attempts[0]["requested_url"] == (
        f"http://127.0.0.1:{server.server_address[1]}"
        "/posting-api/job-board/acme?includeCompensation=true"
    )
    assert attempts[0]["status_code"] == 200
    assert json.loads(attempts[0]["security_policy_json"])

    remote = db.conn.execute(
        "SELECT job_id, canonical_job_url FROM job_sources WHERE source_job_id = ?", (ID3,)
    ).fetchone()
    # the hostile jobUrl was refused; the derived, reviewed link stands
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


def test_only_the_pinned_board_and_origin_are_reachable(db, server):
    """Content cannot re-point a fetch: the board token is config, not payload."""
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
    assert len(urls) == 1
    assert "/posting-api/job-board/acme" in urls[0]
    assert "api.ashbyhq.com" not in urls[0]


def test_host_policy_widens_a_reviewed_ashby_entry_host_to_the_api_host_only():
    """Provider API authority comes from reviewed host code, never config."""
    from jobscraper.adapters.ashby import AshbyConfig
    from jobscraper.pipeline.driver import source_policy

    policy = source_policy({"entry_url": "https://jobs.ashbyhq.com/acme"}, adapter_id="ashby")
    assert policy.allowed_hosts == frozenset({"jobs.ashbyhq.com", "api.ashbyhq.com"})
    # the embed host is a reviewed hosted host too, and widens the same way
    embed = source_policy({"entry_url": "https://apps.ashbyhq.com/embed/acme"}, adapter_id="ashby")
    assert embed.allowed_hosts == frozenset({"apps.ashbyhq.com", "api.ashbyhq.com"})
    # a Greenhouse entry host under the Ashby adapter gets no widening at all
    crossed = source_policy({"entry_url": "https://boards.greenhouse.io/acme"}, adapter_id="ashby")
    assert crossed.allowed_hosts == frozenset({"boards.greenhouse.io"})
    # an unreviewed entry host is pinned to itself
    other = source_policy({"entry_url": "https://careers.example/jobs"}, adapter_id="ashby")
    assert other.allowed_hosts == frozenset({"careers.example"})
    # an operator-pinned alternate origin is syntactically valid to the adapter
    # but is not network authority unless host policy separately approves it
    config = AshbyConfig(board="acme", api_base_url="https://evil.example")
    assert config.api_base_url == "https://evil.example"
    assert "evil.example" not in policy.allowed_hosts


# ---------------------------------------------------------------------------
# Re-driving, restarts and re-observation (RUN-01)
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
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 6
    generations = _coverage(db)
    assert len(generations) == 2
    assert {g["generation_key"] for g in generations} == {f"run-{first_run}", f"run-{second_run}"}
    assert all(g["completion_state"] == "COMPLETE" for g in generations)
    second = db.conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (second_run,)).fetchone()
    assert second["status"] == "SUCCEEDED"
    assert second["jobs_saved"] + second["jobs_updated"] == 3
    assert all(j["listing_status"] == "ACTIVE" for j in _jobs(db))
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_a_crash_before_the_outcome_commit_is_closed_from_durable_evidence(db, server):
    """A single-document run leaves no recoverable mid-walk state (there are
    no pages); the restart path that matters is closing from durable state
    when the outcome row was never written."""
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


def test_a_provisioned_board_runs_through_the_service_and_reaches_the_inbox(service, server):
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
    assert plan["adapter_id"] == "ashby"
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
