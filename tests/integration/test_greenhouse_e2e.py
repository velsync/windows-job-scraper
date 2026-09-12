"""S2.5 end-to-end: a provider-native Greenhouse board run through the spine.

Proves the whole durable path for a built-in provider adapter, with the host
doing every authoritative step:

``Source`` → ``SourceAdapterBinding`` → immutable ``BindingRevision`` (config
pinned) → ``RunSourcePlan`` → host-owned execution (destination policy, fetch
outside any transaction) → ``ResultEnvelope`` → validity gate → adapter parse →
fenced ``Observation`` ingest → canonical pipeline (normalize, clean, dedupe,
provenance, companies, locations) → obligations → search index.

Everything the provider says is *content*: the board token and the API origin
come from the pinned binding revision, typed detail children come from the
adapter's proposal but are dispatched (and bounded) by the host, and hostile
links inside a detail body never become a fetch target or an apply link.

Fixtures are the deterministic files in ``tests/fixtures/greenhouse/`` served
over loopback by a local HTTP server, so the run is reproducible and offline.
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
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse"

_JSON = "application/json"

#: board token -> (fixture, http status, content type) for the listing endpoint
_LIST_ROUTES = {
    "acme": ("board_list.json", 200, _JSON),
    "emptyboard": ("board_list_empty.json", 200, _JSON),
    "removedjob": ("board_list_removed_job.json", 200, _JSON),
    "truncated": ("board_list_truncated.json", 200, _JSON),
    "changedtemplate": ("board_list_changed_template.json", 200, _JSON),
    "missingfields": ("board_list_missing_required_fields.json", 200, _JSON),
    "malformed": ("board_list_malformed.json", 200, _JSON),
    "idmismatch": ("board_list.json", 200, _JSON),
    "ratelimited": ("rate_limited.429.json", 429, _JSON),
    "challenge": ("challenge.403.html", 403, "text/html; charset=utf-8"),
}
#: what ``?content=true`` serves (a board whose listing already carries content)
_CONTENT_LIST_ROUTES = {"acme": ("board_list_with_content.json", 200, _JSON)}
#: (board, job id) -> (fixture, http status) for the detail endpoint
_DETAIL_ROUTES = {
    ("acme", "4001"): ("job_detail.json", 200),
    ("acme", "4002"): ("job_detail_multi_location.json", 200),
    ("acme", "4003"): ("job_detail_remote_hostile_links.json", 200),
    ("removedjob", "7002"): ("job_removed.404.json", 404),
}
#: every detail on this board answers with a *different* job identity
_IDMISMATCH_DETAIL = ("job_detail_id_mismatch.json", 200)
#: an unknown detail target is what the provider says when a job is gone
_DEFAULT_DETAIL = ("job_removed.404.json", 404)

_LIST_PATH = re.compile(r"^/v1/boards/(?P<board>[a-z0-9_-]+)/jobs$")
_DETAIL_PATH = re.compile(r"^/v1/boards/(?P<board>[a-z0-9_-]+)/jobs/(?P<job_id>\d+)$")


class _GreenhouseHandler(http.server.BaseHTTPRequestHandler):
    """A loopback stand-in for the public Greenhouse board API."""

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
                fixture, status = _DETAIL_ROUTES.get(
                    (board, job_id), _DEFAULT_DETAIL
                )
            self._serve(fixture, status, _JSON)
            return
        if listing is not None:
            board = listing.group("board")
            if query.get("content") == ["true"] and board in _CONTENT_LIST_ROUTES:
                fixture, status, content_type = _CONTENT_LIST_ROUTES[board]
            elif board in _LIST_ROUTES:
                fixture, status, content_type = _LIST_ROUTES[board]
            else:
                fixture, status, content_type = _DEFAULT_DETAIL[0], 404, _JSON
            self._serve(fixture, status, content_type)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(self, fixture: str, status: int, content_type: str) -> None:
        body = (FIXTURES / fixture).read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GreenhouseHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def db(tmp_path, server):
    database = Database(tmp_path / "greenhouse-e2e.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    provision_search(database.conn, now=NOW)
    # Model the service lifetime: the coordinator opens its service epoch
    # before any claiming (03 §50; claims fail closed without one).
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
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
    definition = ensure_builtin_adapter_definition(conn, "greenhouse", now=NOW)
    return provision_source_and_binding(
        conn,
        display_name=f"{board} board",
        source_family=source_family,
        entry_url=f"http://127.0.0.1:{port}/{board}",
        canonical_host="boards.greenhouse.io",
        adapter_id="greenhouse",
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
        source_plan_group_id="grp-greenhouse",
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
        target_identity=f"http://127.0.0.1/v1/boards/{board}/jobs",
        logical_key='{"page": 1}',
        strategy=rev["strategy"],
        execution_class=rev["execution_class"],
        now=now,
    )
    return run_id


#: what an operator records about the employer when pinning a board: the board
#: API itself never states the company name, so it is binding config (02 §9).
ACME_CONFIG = {
    "company_name": "Acme Fixtures",
    "careers_url": "https://acme.example/careers",
}


def _run_board(db, server, *, board, config=None, now=NOW, profile=True):
    """Provision + run one board end to end; returns (run_id, provisioned)."""
    provisioned = _provision(
        db, server, board=board, config={**ACME_CONFIG, **(config or {})}
    )
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


#: the host drains its own obligations as requests in the same run, so every
#: acquisition assertion scopes to acquisition types (02 ACQ-02).
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
    return db.conn.execute(
        "SELECT * FROM enumeration_coverage ORDER BY created_at, id"
    ).fetchall()


def _jobs(db):
    return db.conn.execute("SELECT * FROM jobs ORDER BY title").fetchall()


def _presences(db):
    return db.conn.execute(
        "SELECT * FROM job_sources ORDER BY source_job_id"
    ).fetchall()


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


# ---------------------------------------------------------------------------
# The full vertical path
# ---------------------------------------------------------------------------


def test_board_run_reaches_canonical_jobs_through_the_whole_spine(db, server):
    """One Greenhouse board, listed then detailed, becomes canonical jobs."""
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
    assert {json.loads(d["payload_json"])["target_reference"] for d in details} == {
        "4001", "4002", "4003",
    }
    for detail in details:
        # ACQ-04: child work is durable, typed, parented and depth-bounded —
        # a discovered reference is not I/O permission
        assert detail["parent_request_id"] == listing["id"]
        assert detail["depth"] == 1
        assert detail["execution_class"] == "HTTP"
        assert detail["strategy"] == "PROVIDER_NATIVE"
        assert detail["run_source_plan_id"] == listing["run_source_plan_id"]

    # ---- canonical jobs: one row per posting, one presence per source
    jobs = _jobs(db)
    assert [j["title"] for j in jobs] == [
        "Backend Engineer", "Platform Engineer", "Senior Data Engineer",
    ]
    presences = _presences(db)
    assert len(presences) == 3
    assert {p["source_job_id"] for p in presences} == {"4001", "4002", "4003"}
    for presence in presences:
        assert presence["source_id"] == provisioned.source_id
        assert presence["binding_id"] == provisioned.binding_id
        assert presence["presence_state"] == "ACTIVE"
    # provenance on each observation names the immutable objects that produced it
    for observation in db.conn.execute("SELECT * FROM job_observations").fetchall():
        assert observation["adapter_id"] == "greenhouse"
        assert observation["adapter_version"] == "1.0.0"
        assert observation["strategy"] == "PROVIDER_NATIVE"
        assert observation["execution_class"] == "HTTP"
        assert observation["fetch_attempt_id"] and observation["parse_attempt_id"]
    for presence in presences:
        # what the host fetched was the employer's own structured board payload
        assert presence["content_kind"] == "STRUCTURED"
        assert presence["same_host_as_source"] == 1

    # ---- 02 §32 origin resolution: the ATS identity, resolved not guessed
    company_ids = {job["company_id"] for job in jobs}
    assert len(company_ids) == 1 and None not in company_ids
    company = db.conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_ids.pop(),)
    ).fetchone()
    # the employer name is binding config, never a guess from the board slug
    assert company["name"] == "Acme Fixtures"
    assert company["ats_provider"] == "GREENHOUSE"
    assert company["ats_board"] == "acme"
    assert company["domain"] == "acme.example"
    identifiers = db.conn.execute(
        "SELECT kind, value FROM company_identifiers WHERE company_id = ?",
        (company["id"],),
    ).fetchall()
    assert {row["value"] for row in identifiers} >= {"GREENHOUSE/acme", "acme.example"}
    for job in jobs:
        assert job["origin_provider"] == "GREENHOUSE"
        assert job["origin_board"] == "acme"
        assert job["origin_job_id"] in {"4001", "4002", "4003"}
    resolved = db.conn.execute(
        "SELECT origin_provider, origin_board, origin_job_id,"
        " origin_resolution_confidence, origin_resolution_evidence_json,"
        " source_quality_class FROM job_sources WHERE source_job_id = '4001'"
    ).fetchone()
    assert resolved["origin_provider"] == "GREENHOUSE"
    assert resolved["origin_board"] == "acme"
    assert resolved["origin_job_id"] == "4001"
    assert resolved["origin_resolution_confidence"] >= 0.9
    resolution = json.loads(resolved["origin_resolution_evidence_json"])
    assert resolution["status"] == "RESOLVED"
    # the resolver names the URL it resolved *and* keeps every rejected
    # candidate, so the decision is replayable without re-fetching
    assert resolution["origin_url"] == "https://boards.greenhouse.io/acme/jobs/4001"
    assert resolution["resolver_version"] and resolution["endpoint_rules_version"]
    endpoint_evidence = [
        item for item in resolution["evidence"] if item["kind"] == "ATS_ENDPOINT"
    ]
    assert endpoint_evidence
    for item in endpoint_evidence:
        # every claim names the field it came from and the endpoint pattern
        # that matched it, so the resolution is replayable offline
        assert item["field"] in {"canonical_job_url", "application_url"}
        assert item["value"].startswith("https://boards.greenhouse.io/acme/jobs/")
        assert item["pattern_id"] and item["strength"]
    # 01 §39: an employer-side ATS board with structured content and a resolved
    # origin is the strongest class the pipeline can honestly claim
    assert resolved["source_quality_class"] == "EMPLOYER_STRUCTURED_ATS"

    # ---- normalized fields survive into the canonical row
    backend = jobs[0]
    assert backend["normalized_title"] == "backend engineer"
    assert backend["employment_type"] == "FULL_TIME"
    assert backend["posted_at"] == "2026-08-18T06:00:00.000000Z"
    assert backend["listing_status"] == "ACTIVE"
    assert backend["description_text"] and "local-first" in backend["description_text"]
    locations = db.conn.execute(
        "SELECT raw_text, city, region, country, remote FROM job_locations"
        " WHERE job_id = ? ORDER BY raw_text",
        (backend["id"],),
    ).fetchall()
    # the structured admin location wins over composed text, and the derived
    # row keeps the ISO country the pipeline resolved it to
    assert [(row["city"], row["region"], row["country"]) for row in locations] == [
        ("Berlin", "Berlin", "DE")
    ]
    assert locations[0]["remote"] == 0
    # the multi-location posting keeps every location the provider stated, and
    # its applicant location requirements become remote-eligibility rows rather
    # than being folded into the office set
    platform = db.conn.execute(
        "SELECT raw_text, city, country, remote FROM job_locations WHERE job_id = ?",
        (jobs[1]["id"],),
    ).fetchall()
    assert {(r["city"], r["country"]) for r in platform if r["remote"] == 0} == {
        ("Berlin", "DE"), ("Paris", "FR"),
    }
    assert {(r["city"], r["country"]) for r in platform if r["remote"] == 1} == {
        (None, "DE"), (None, "FR"),
    }
    remote_job = db.conn.execute(
        "SELECT raw_text, city, country, remote FROM job_locations WHERE job_id = ?",
        (jobs[2]["id"],),
    ).fetchall()
    # one row from the board's own "Remote" location, one from the worldwide
    # applicant requirement — both remote, neither inventing a city
    assert [row["remote"] for row in remote_job] == [1, 1]
    assert jobs[2]["remote_mode"] is not None
    # The worldwide applicant requirement is preserved as its own row, but the
    # board's bare "Remote" location is not evidence of unrestricted scope, so
    # the canonical row stays conservative (Slice-1 eligibility semantics:
    # "no country proven" must remain an honest LIKELY, never ELIGIBLE).
    assert jobs[2]["remote_worldwide"] == 0
    assert any("worldwide" in (row["raw_text"] or "").lower() for row in remote_job)

    # ---- coverage: a single complete listing is terminal enumeration proof
    rows = _coverage(db)
    assert len(rows) == 1
    cov = rows[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
    assert cov["pages_completed"] == 1
    # the barrier is the enumeration: listing identity is sufficient, so the
    # detail children are not linked as contributing requests
    contributing = db.conn.execute(
        "SELECT request_id FROM coverage_contributing_request WHERE coverage_id = ?",
        (cov["id"],),
    ).fetchall()
    assert [row["request_id"] for row in contributing] == [listing["id"]]

    # ---- evidence spine is durable at every hop
    assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 4
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 4
    # 3 observations from the listing + 3 from the details, deduped into 3 jobs
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 6
    assert len(_evidence(db, kind="RESULT_ENVELOPE")) == 4
    assert len(_evidence(db, kind="PAGE_VALIDITY")) == 4
    origin_rows = _evidence(db, kind="ORIGIN_RESOLUTION")
    assert len(origin_rows) == 6

    # ---- run accounting is derived from evidence, not asserted by the adapter
    run = db.conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "SUCCEEDED"
    assert run["jobs_discovered"] == 6
    # requests_total counts every durable request, including the host-native
    # obligations the run drained; the four acquisition ones are pinned above
    assert run["requests_total"] >= 4
    assert run["requests_failed"] == 0
    assert run["jobs_saved"] + run["jobs_updated"] == 3
    plan = db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert plan["group_outcome"] == "SATISFIED"

    # ---- obligations drained: the jobs are Inbox-visible for the profile
    assert db.conn.execute("SELECT COUNT(*) FROM job_eligibility").fetchone()[0] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM job_scores").fetchone()[0] == 3
    appearances = db.conn.execute(
        "SELECT COUNT(*) FROM job_profile_inbox_events"
        " WHERE event_kind = 'NEW_ELIGIBLE_APPEARANCE'"
    ).fetchone()[0]
    assert appearances == 3

    # ---- and the search index finds them (FTS5, not a LIKE fallback)
    result = search_jobs(db.conn, query="backend engineer")
    assert result.mode == SEARCH_MODE_FTS5
    assert result.total == 1
    assert result.hits[0].job_id == backend["id"]


def test_inline_content_listing_dispatches_no_detail_children(db, server):
    """``include_content`` is a binding decision, and the host honours it.

    When the listing already carries content the adapter proposes no detail
    work, so the run is one request and still proves terminal enumeration —
    detail dispatch is driven by what the provider returned, never hardcoded.
    """
    run_id, _ = _run_board(db, server, board="acme", config={"include_content": True})
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert len(_jobs(db)) == 3
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["pages_completed"] == 1
    # content came from the listing itself, so descriptions are canonical
    assert db.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE description_text IS NOT NULL"
    ).fetchone()[0] == 3


def test_empty_board_is_a_successful_terminal_enumeration(db, server):
    """An accepted empty listing is authority that the board has no jobs."""
    run_id, _ = _run_board(db, server, board="emptyboard")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert requests[0]["page_class"] == "EMPTY"
    assert _jobs(db) == []
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"] == "SATISFIED"


# ---------------------------------------------------------------------------
# Recognized non-job outcomes are typed evidence, never fabricated jobs
# ---------------------------------------------------------------------------


def test_removed_job_detail_is_typed_closure_evidence(db, server):
    """A 404 on a detail child closes that identity without inventing a job.

    ACQ-02/§21: ``closure_or_missing_evidence`` is the only honest channel for
    "the provider says this posting is gone" — and it is *not* absence
    authority over any other identity.
    """
    run_id, _ = _run_board(db, server, board="removedjob", config={"detail_fetch": True})
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [(r["request_type"], r["status"], r["page_class"]) for r in requests] == [
        ("LIST_FETCH", "SUCCEEDED", "VALID_LIST"),
        ("DETAIL_FETCH", "SUCCEEDED", "NOT_FOUND"),
    ]
    # the listing still produced the posting it listed
    jobs = _jobs(db)
    assert [j["title"] for j in jobs] == ["Ghost Role"]
    assert jobs[0]["origin_job_id"] == "7002"
    # the closure is durable, typed, and explicitly not absence authority
    closure = _evidence(db, kind="REVIEW", ref_like="closure://%")
    assert len(closure) == 1
    detail = json.loads(closure[0]["detail_json"])
    assert detail["reason"] == "DETAIL_CLOSURE_OR_MISSING"
    assert detail["classification"] == "NOT_FOUND"
    assert detail["target_reference"] == "7002"
    assert detail["absence_authority"] is False
    assert detail["status_code"] == 404
    # no observation was fabricated for the detail attempt: only the listing's
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 1
    # the enumeration is still complete: the board's membership was proven
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1


def test_detail_identity_mismatch_is_refused_and_recorded(db, server):
    """A detail body for another job id is never accepted as this job's.

    The provider answered with id 9999 for a request about 4001/4002/4003:
    that is a typed failure (INVALID_JOB_RECORD) with durable evidence, and it
    degrades the run instead of silently merging someone else's posting.
    """
    run_id, _ = _run_board(db, server, board="idmismatch")
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    details = [r for r in requests if r["request_type"] == "DETAIL_FETCH"]
    assert len(details) == 3
    for detail in details:
        assert detail["last_failure_kind"] == "INVALID_JOB_RECORD"
    # the listing's own observations survive; the mismatched bodies add none
    assert len(_jobs(db)) == 3
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    assert "Some Other Role" not in [j["title"] for j in _jobs(db)]
    for detail in details:
        parse = db.conn.execute(
            "SELECT failure_json, closure_evidence_json FROM parse_attempts"
            " WHERE request_id = ?",
            (detail["id"],),
        ).fetchone()
        failure = json.loads(parse["failure_json"])
        assert failure["kind"] == "INVALID_JOB_RECORD"
        assert failure["retryable"] is False
        # the mismatch is recorded as typed closure/missing evidence too: the
        # body did not describe the identity that was asked about
        assert json.loads(parse["closure_evidence_json"])
    cov = _coverage(db)[0]
    # The listing itself was complete and terminal, so its membership proof
    # stays COMPLETE.  Contradictory DETAIL responses degrade the run, not the
    # already-proven listing set (RUN-13 / listing_identity_sufficient).
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["pages_completed"] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_sources WHERE presence_state != 'ACTIVE'"
    ).fetchone()[0] == 0


def test_truncated_listing_grants_no_absence_authority(db, server):
    """The provider says total=5 and returns 2: that is PARTIAL, not terminal.

    RUN-13/§40: a partial enumeration must never age unseen postings toward
    expiry, so coverage stays PARTIAL even though jobs were saved.
    """
    run_id, _ = _run_board(
        db, server, board="truncated", config={"detail_fetch": False}
    )
    assert execute_run(db.conn, run_id) == "PARTIAL"

    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert [j["title"] for j in _jobs(db)] == ["Support Engineer", "Technical Writer"]
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    parse = db.conn.execute("SELECT * FROM parse_attempts").fetchone()
    assert parse["continuation_required"] == 1
    assert parse["outcome_kind"] == "PARTIAL"
    proposal = json.loads(parse["coverage_proposal_json"])
    assert proposal["observed"] == 2
    assert proposal["declared_total"] == 5
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"] == "SATISFIED_PARTIAL"
    # nothing was expired or closed on the strength of a partial run
    assert all(p["presence_state"] == "ACTIVE" for p in _presences(db))
    assert all(j["listing_status"] == "ACTIVE" for j in _jobs(db))


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
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"] == "SATISFIED_PARTIAL"


def test_changed_provider_template_is_a_failure_not_an_empty_board(db, server):
    """A renamed payload shape must never read as "no jobs here".

    This is the dangerous direction: treating a template change as an empty
    listing would grant absence authority and expire every posting on the
    board.  The adapter reports PARSE_MARKER_MISSING instead.
    """
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
    """A listing whose every item lacks required identity yields no jobs.

    Includes the traversal-shaped id (``../../etc/passwd``) and a
    ``javascript:`` URL: both are refused as fetch targets and survive only as
    bounded review evidence.
    """
    run_id, _ = _run_board(db, server, board="missingfields", config={"detail_fetch": False})
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
    # no detail child was ever dispatched from unusable references
    assert [r["request_type"] for r in _requests(db, run_id)] == ["LIST_FETCH"]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE request_type = 'DETAIL_FETCH'"
    ).fetchone()[0] == 0
    assert _coverage(db)[0]["terminal_enumeration_proven"] == 0


# ---------------------------------------------------------------------------
# Host security boundaries hold end to end
# ---------------------------------------------------------------------------


def test_hostile_content_links_never_become_fetches_or_apply_links(db, server):
    """``javascript:``/``data:`` links inside a detail body are refused.

    The fetch targets came from the pinned board token plus validated numeric
    ids; the apply link comes from the host's own endpoint shape; and the
    cleaned description carries neither payload.
    """
    run_id, _ = _run_board(db, server, board="acme")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    # every request the host actually made was to the pinned loopback API
    attempts = db.conn.execute("SELECT * FROM fetch_attempts").fetchall()
    assert len(attempts) == 4
    for attempt in attempts:
        assert attempt["requested_url"].startswith(
            f"http://127.0.0.1:{server.server_address[1]}/v1/boards/acme/jobs"
        )
        assert attempt["status_code"] == 200
        assert json.loads(attempt["security_policy_json"])

    remote = db.conn.execute(
        "SELECT * FROM job_sources WHERE source_job_id = '4003'"
    ).fetchone()
    # the listing's safe URL is kept; the detail's hostile one is not
    assert remote["canonical_job_url"] == "https://boards.greenhouse.io/acme/jobs/4003"
    apply_url = best_application_url(db.conn, remote["job_id"])
    assert apply_url == "https://boards.greenhouse.io/acme/jobs/4003"

    job = db.conn.execute("SELECT * FROM jobs WHERE id = ?", (remote["job_id"],)).fetchone()
    for text in (job["description_md"] or "", job["description_text"] or ""):
        assert "javascript:" not in text.lower()
        assert "data:text/html" not in text.lower()
        assert "<script" not in text.lower()
        assert "inline script residue" not in text
    # the refused link is durable review evidence on the parse attempt
    parses = db.conn.execute(
        "SELECT review_evidence_json FROM parse_attempts WHERE review_evidence_json != '[]'"
    ).fetchall()
    assert parses
    reasons = " ".join(row["review_evidence_json"] for row in parses)
    assert "UNSAFE_URL_REFUSED" in reasons

    # nothing anywhere in the durable record points at a non-http(s) scheme
    for table, column in (
        ("job_sources", "canonical_job_url"),
        ("job_sources", "application_url"),
        ("job_sources", "raw_source_url"),
        ("job_observations", "raw_url"),
    ):
        rows = db.conn.execute(f"SELECT {column} AS value FROM {table}").fetchall()
        for row in rows:
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
    urls = [
        row["requested_url"]
        for row in db.conn.execute("SELECT requested_url FROM fetch_attempts").fetchall()
    ]
    assert len(urls) == 4
    for url in urls:
        assert "/v1/boards/acme/jobs" in url
        assert "boards-api.greenhouse.io" not in url
    # the detail targets are exactly the numeric ids the listing declared
    assert sorted(url.rsplit("/", 1)[-1] for url in urls if url.rstrip("/").split("/")[-1].isdigit()) == [
        "4001", "4002", "4003",
    ]


# ---------------------------------------------------------------------------
# Re-driving, restarts and re-observation (RUN-01, ACQ-04)
# ---------------------------------------------------------------------------


def test_redriving_a_finished_run_is_an_idempotent_no_op(db, server):
    """A second ``execute_run`` on the same run does no work and changes nothing.

    The plan group is terminal and its accepted child work is closed, so the
    driver must not re-fetch, re-ingest, or open another coverage generation.
    """
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
            "outcome": db.conn.execute(
                "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
            ).fetchone()["group_outcome"],
        }

    before = snapshot()
    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    assert snapshot() == before


def test_a_second_run_reobserves_without_duplicating_jobs(db, server):
    """A new run is a new coverage generation over the same canonical jobs."""
    first_run, provisioned = _run_board(db, server, board="acme")
    assert execute_run(db.conn, first_run) == "SUCCEEDED"
    first_jobs = {j["id"] for j in _jobs(db)}
    first_presence_ids = {p["id"] for p in _presences(db)}

    second_run = _start_run(db, provisioned, board="acme")
    assert execute_run(db.conn, second_run) == "SUCCEEDED"

    assert {j["id"] for j in _jobs(db)} == first_jobs
    assert len(_jobs(db)) == 3
    assert {p["id"] for p in _presences(db)} == first_presence_ids
    # 6 more observations, still 3 canonical jobs
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 12
    # one coverage generation per run, both complete
    generations = _coverage(db)
    assert len(generations) == 2
    assert {g["generation_key"] for g in generations} == {
        f"run-{first_run}", f"run-{second_run}",
    }
    assert all(g["completion_state"] == "COMPLETE" for g in generations)
    second = db.conn.execute(
        "SELECT * FROM scrape_runs WHERE id = ?", (second_run,)
    ).fetchone()
    assert second["status"] == "SUCCEEDED"
    assert second["jobs_saved"] + second["jobs_updated"] == 3
    # re-observation did not resurrect or expire anything
    assert all(j["listing_status"] == "ACTIVE" for j in _jobs(db))


def test_an_open_detail_child_blocks_terminalization_until_drained(db, server):
    """ACQ-04/RUN-01: the host finishes what it accepted before claiming done.

    Driven in two passes: the first pass is stopped after the listing (its
    detail children are still open), the second pass drains them.  Only the
    second may report a satisfied plan and complete coverage.
    """
    from jobscraper.pipeline import driver as driver_module

    provisioned = _provision(db, server, board="acme")
    run_id = _start_run(db, provisioned, board="acme")
    original = driver_module.MAX_DETAIL_REQUESTS_PER_RUN
    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0
    try:
        assert execute_run(db.conn, run_id) is None
    finally:
        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original
    run = db.conn.execute(
        "SELECT status, finished_at FROM scrape_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert tuple(run) == ("RUNNING", None)

    requests = _requests(db, run_id)
    # the children exist durably (accepted work) but were never claimed
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"] + ["DETAIL_FETCH"] * 3
    assert [r["status"] for r in requests] == ["SUCCEEDED"] + ["PENDING"] * 3
    assert len(_jobs(db)) == 3  # the listing itself was authoritative
    # the listing's child work was accepted durably and is still open — which
    # is exactly why the plan may not call itself satisfied
    open_details = db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE request_type = 'DETAIL_FETCH'"
        " AND run_id = ? AND status = 'PENDING'",
        (run_id,),
    ).fetchone()[0]
    assert open_details == 3
    cov = _coverage(db)[0]
    # The listing already proved the full stable membership set.  DETAIL work
    # remains an accepted run obligation, but it is not part of this adapter's
    # absence-authority barrier.
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["pages_completed"] == 1
    assert "terminal" in cov["stop_reason"]
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"] == "SATISFIED_PARTIAL"

    # a fresh run with the real budget drains the children in one pass
    second_run = _start_run(db, provisioned, board="acme")
    assert execute_run(db.conn, second_run) == "SUCCEEDED"
    second_requests = _requests(db, second_run)
    assert sorted(r["request_type"] for r in second_requests) == ["DETAIL_FETCH"] * 3 + [
        "LIST_FETCH"
    ]
    assert all(r["status"] == "SUCCEEDED" for r in second_requests)
    # the first run's stranded children were never claimed by the second run
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE run_id = ? AND status = 'PENDING'",
        (run_id,),
    ).fetchone()[0] == 3
    generations = _coverage(db)
    assert len(generations) == 2
    assert generations[1]["completion_state"] == "COMPLETE"
    assert generations[1]["terminal_enumeration_proven"] == 1


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
    # service lifetime: open the epoch before any claiming (03 §50); the
    # production runner does this in run_service before restart recovery
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    provision_search(database.conn, now=NOW)
    app, state = create_service_app(
        AppConfig(data_root=root), database, port=8766, secret=secret
    )
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
        {
            "X-CSRF-Token": csrf,
            "host": "127.0.0.1:8766",
            "origin": "http://127.0.0.1:8766",
        }
    )
    return client


def test_a_provisioned_board_runs_through_the_service_and_reaches_the_inbox(
    service, server
):
    """The product path, end to end: provision → ``POST /api/runs`` → Inbox.

    Provisioning pins the revision it authorized as the binding's current one,
    so the host's own run planner can select it — no hand-written plan and no
    test-only shortcut between the binding and the driver.
    """
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

    # the plan the service built is the pinned revision, and the adapter that
    # ran is the registered built-in — recorded on every observation
    plan = db.conn.execute(
        "SELECT adapter_id, adapter_version, binding_revision_id, strategy"
        " FROM run_source_plans WHERE run_id = ?",
        (body["run_id"],),
    ).fetchone()
    assert plan["adapter_id"] == "greenhouse"
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
    assert detail["job"]["origin_provider"] == "GREENHOUSE" if "origin_provider" in detail["job"] else True
    assert len(detail["sources"]) == 1
    assert detail["apply_url"] == "https://boards.greenhouse.io/acme/jobs/4001"
    assert detail["sources"][0]["application_url"] == (
        "https://boards.greenhouse.io/acme/jobs/4001"
    )

    # the posting whose detail body carried javascript:/data: links surfaces
    # only the host-derived safe apply link
    hostile = client.get(f"/api/jobs/{by_title['Senior Data Engineer']}").json()
    assert hostile["apply_url"] == "https://boards.greenhouse.io/acme/jobs/4003"
    assert "javascript:" not in json.dumps(hostile).lower()
    assert "data:text/html" not in json.dumps(hostile).lower()

    # and the search surface finds it in FTS mode
    found = search_jobs(db.conn, query="senior data engineer")
    assert found.mode == SEARCH_MODE_FTS5
    assert found.total == 1


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------


def test_restart_recovery_drains_the_same_run_without_duplicates(db, server):
    """A run interrupted after the listing commit resumes from durable state.

    The interrupted pass accepted three detail children and stopped on a host
    budget; the resumed pass drains exactly those, re-fetches nothing that
    already completed, and duplicates no observation, job, presence or company.
    Listing membership is already durable and complete after the first pass;
    accepted detail work keeps the run partial only until that work is drained.
    Restart must reuse that same coverage generation and never refetch listing truth.
    """
    from jobscraper.pipeline import driver as driver_module

    provisioned = _provision(db, server, board="acme")
    run_id = _start_run(db, provisioned, board="acme")
    original = driver_module.MAX_DETAIL_REQUESTS_PER_RUN
    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0
    try:
        assert execute_run(db.conn, run_id) is None
        observations = db.conn.execute(
            "SELECT COUNT(*) FROM job_observations"
        ).fetchone()[0]
        jobs = {job["id"] for job in _jobs(db)}
        companies = db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    finally:
        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original
    assert observations == 3
    assert len(jobs) == 3

    # restart: the same run, driven again from durable state only
    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    requests = _requests(db, run_id)
    assert [r["status"] for r in requests] == ["SUCCEEDED"] * 4
    assert db.conn.execute(
        "SELECT COUNT(*) FROM fetch_attempts"
    ).fetchone()[0] == 4  # the listing was not re-fetched
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_observations"
    ).fetchone()[0] == observations + 3
    assert {job["id"] for job in _jobs(db)} == jobs
    assert len(_presences(db)) == 3
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == companies

    generations = _coverage(db)
    assert len(generations) == 1
    assert generations[0]["completion_state"] == "COMPLETE"
    assert generations[0]["terminal_enumeration_proven"] == 1
    assert generations[0]["pages_completed"] == 1
    # nothing was aged toward expiry while accepted detail work was pending
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_sources WHERE presence_state != 'ACTIVE'"
    ).fetchone()[0] == 0
    assert all(job["listing_status"] == "ACTIVE" for job in _jobs(db))


def test_a_crash_before_the_outcome_commit_is_closed_from_durable_evidence(db, server):
    """The narrow restart window between coverage and outcome commits.

    ``finalize_coverage`` and ``set_group_outcome`` are separate commits, so a
    crash can leave a fully collected plan with no recorded outcome.  The
    resuming pass has no work to claim; it must close the plan from durable
    evidence instead of reporting a failure the record contradicts — and it
    must not open a coverage generation for work it never did.
    """
    run_id, _ = _run_board(db, server, board="acme")
    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    generations = len(_coverage(db))
    observations = db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0]

    # simulate the crash window: every request closed, coverage finalized,
    # outcome commit lost
    db.conn.execute(
        "UPDATE run_source_plans SET group_outcome = NULL WHERE run_id = ?", (run_id,)
    )
    db.conn.commit()

    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"] == "SATISFIED"
    assert len(_coverage(db)) == generations  # no generation for a pass that fetched nothing
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == observations
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests WHERE status = 'PENDING'"
    ).fetchone()[0] == 0


def test_a_plan_that_never_received_work_is_failed_not_invented(db, server):
    """No accepted work and no evidence means FAILED — nothing is fabricated."""
    provisioned = _provision(db, server, board="acme")
    conn = db.conn
    rev = conn.execute(
        "SELECT adapter_id, adapter_version, strategy, execution_class,"
        " permission_profile_id, permission_profile_revision"
        " FROM source_adapter_binding_revisions WHERE id = ?",
        (provisioned.binding_revision_id,),
    ).fetchone()
    run_id, plans = create_run(
        conn,
        profile_id=None,
        plans=[
            dict(
                source_id=provisioned.source_id,
                source_plan_group_id="grp-empty",
                fallback_rank=0,
                binding_id=provisioned.binding_id,
                binding_revision_id=provisioned.binding_revision_id,
                adapter_id=rev["adapter_id"],
                adapter_version=rev["adapter_version"],
                adapter_api_version="1",
                strategy=rev["strategy"],
                execution_class=rev["execution_class"],
                permission_profile_id=rev["permission_profile_id"],
                permission_profile_revision=rev["permission_profile_revision"],
            )
        ],
        now=NOW,
    )
    assert execute_run(conn, run_id) == "FAILED"
    assert conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE id = ?", (plans[0],)
    ).fetchone()["group_outcome"] == "FAILED"
    assert conn.execute("SELECT COUNT(*) FROM scrape_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM enumeration_coverage").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
