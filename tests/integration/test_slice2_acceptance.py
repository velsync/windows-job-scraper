"""Slice 2 automated acceptance (S2.8) — the Slice-2 E2E gate.

This suite is the Slice-2 gate deliverable (plan v0313 §S2.8, deliverable gate
"E2E gate"): one file that proves — or refuses to prove — the slice's
end-to-end claims.  ``scripts/verify_slice2.py`` (the S2.9 deliverable)
aggregates this file as its e2e acceptance component, exactly as
``scripts/verify_slice1.py`` aggregates ``test_slice1_acceptance.py``.  Every
scenario below names the normative claim it pins, so a gate failure is
readable as a failed acceptance criterion, not as "some test broke".

Scenarios and their authority:

1. Cross-provider full chain (parametrized over Greenhouse, Lever, Ashby;
   02 §12.1/§12.2/§12.3, §19, §22, §31/§32, ACQ-02/03/04/09; 03 §40;
   06 §72.5/§72.7/§72.8/§72.9/§72.13/§72.14) — for each graduated provider:
   an operator-added careers URL is probed through the host executor under
   the host's own destination policy, the acquired content is fingerprinted
   (evidence-first, versioned), the strategy router emits the
   ``PROVIDER_NATIVE`` candidate, provisioning records the fingerprint + route
   decision as durable evidence and pins the immutable binding revision, and
   the driver run acquires → validates → parses → observes → resolves origin
   → resolves company → projects multi-location → cleans content → selects
   canonical provenance → drains obligations → lands the job in the FTS5
   index and the Inbox with a safe direct apply link.

2. Multi-source same-origin merge (01 §33/§38/§39 via plan §3; 06 §72.8/§72.15)
   — an aggregator feed and the employer's own Greenhouse board observe the
   same postings: one canonical job per posting (never a duplicate), the
   employer presence wins presentation (``EMPLOYER_STRUCTURED_ATS`` over
   ``AGGREGATOR_WITH_RESOLVED_ORIGIN``), and the aggregator presence stays
   fully inspectable with its own discovery URL — presentation, not deletion.
   A hostile ``javascript:`` apply candidate inside the feed survives only as
   immutable evidence; every product-emitted link is safelink-approved
   (PROD-05) and the winner's derived link is what the product shows.

3. Generic careers URL falls back honestly (02 §12.1, ARC-08; 06 §72.7) — a
   careers page with no ATS evidence fingerprints to no family; the router
   records ``GENERIC_DISCOVERY_FALLBACK`` with no runnable candidate and the
   honest reason (Slice 2 has no generic-discovery adapter — recorded as
   ``GENERIC_DISCOVERY_NOT_IMPLEMENTED``, never fabricated).  The hunch-free
   fingerprint and the fallback decision are durable; nothing runnable is
   invented for the source.

4. Low confidence never forces a specialized route (02 §12.1: "Low-confidence
   fingerprinting MUST fall back to generic discovery rather than silently
   forcing a specialized adapter") — a weak single-marker Greenhouse hunch
   (0.60 < 0.70) records the family as evidence and still emits no
   specialized candidate; no binding is provisioned from a hunch.

5. FTS mode honesty (01 §45; 06 §72.13) — an FTS5-capable host records
   ``FTS5_ACTIVE`` and answers in that mode with no warning; a host whose
   SQLite reports no FTS5 records ``SUBSTRING_FALLBACK`` with the explicit
   warning, still finds the same jobs through the indexed documents, and
   never claims BM25/FTS.  Both states are produced by the same provisioning
   and query surface, so the report cannot drift from reality.

6. Security negatives stay denied (04 §5.1, SEC-02/03/09; 02 §26) — a
   redirect off a real run to a private address is denied at the redirect hop
   and lands as durable ``SECURITY_POLICY`` evidence with the run degraded and
   no absence authority; a private-range destination is denied by address
   classification before any connection; a redirect to a different loopback
   identity (``localhost`` vs the granted literal) is denied by the exact-host
   rule; an oversized body is capped and typed, never parsed; and a
   ``javascript:`` probe URL is refused at the acquisition boundary before
   any connection exists.

Provenance of the fixture content: the three provider payloads are the
reviewed deterministic corpora in ``tests/fixtures/{greenhouse,lever,ashby}/``
(live-verified 2026-09-09 per their READMEs; the deterministic files are the
regression authority, 02 §26).  The careers pages and the aggregator feed are
synthetic marker-bearing documents defined inline below — they exercise the
fingerprint classifier and the feed adapter, not any provider's real HTML.

Timestamps: the provider fixtures keep their reviewed published stamps (e.g.
Ashby ``publishedAt`` 2026-06-01, Greenhouse ``first_published_at``
2026-08-18) — those are content, not configuration, and are deliberately not
updated.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from jobscraper.acquisition.envelope import ExecutionPlanEnvelope, RequestPlan
from jobscraper.acquisition.httpexec import execute_request
from jobscraper.acquisition.pagevalidity import classify_page
from jobscraper.adapters.fingerprint import classify_content
from jobscraper.adapters.registry import BUILTIN_ADAPTERS
from jobscraper.adapters.router import RouteOutcome, plan_routes
from jobscraper.applications.applylink import best_application_url
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.net.safelinks import safe_external_url
from jobscraper.pipeline.driver import execute_run, source_policy
from jobscraper.profiles.core import create_profile
from jobscraper.runtime.provisioning import (
    ProvisionedSource,
    ensure_builtin_adapter_definition,
    provision_source_and_binding,
    record_fingerprint,
    record_route_decision,
)
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run
from jobscraper.search.capability import (
    SEARCH_MODE_FTS5,
    SEARCH_MODE_SUBSTRING,
    read_capability,
    record_capability,
)
from jobscraper.search.provision import (
    FTS_TABLE,
    fts_table_present,
    provision_search,
    triggers_exist,
)
from jobscraper.search.query import search_jobs

#: Deterministic constants, pinned in the past like every sibling suite: the
#: driver stamps its claims with the real UTC clock (``db_utc_now``), so a
#: seeded instant in the future would sort after the requests it scheduled and
#: scramble every ``created_at``-ordered assertion.  The session date lives in
#: the review record, not in this constant.
NOW = "2026-09-09T12:00:00.000000Z"
LATER = "2026-09-09T14:00:00.000000Z"

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_GREENHOUSE = _FIXTURES / "greenhouse"
_LEVER = _FIXTURES / "lever"
_ASHBY = _FIXTURES / "ashby"

_JSON = "application/json"

_GH_ID1, _GH_ID2, _GH_ID3 = "4001", "4002", "4003"
_LV_ID1 = "1a2b3c4d-0000-4000-8000-000000004001"
_LV_ID2 = "1a2b3c4d-0000-4000-8000-000000004002"
_LV_ID3 = "1a2b3c4d-0000-4000-8000-000000004003"
_AB_ID1 = "5a1b2c3d-0000-4000-8000-000000005001"
_AB_ID2 = "5a1b2c3d-0000-4000-8000-000000005002"
_AB_ID3 = "5a1b2c3d-0000-4000-8000-000000005003"

#: Provider truth table for the acceptance gate.  Everything the full-chain
#: scenario asserts about one provider is data here — the assertions are one
#: shared scenario, the expectations are the reviewed provider facts.
PROVIDERS = {
    "greenhouse": {
        "adapter": "greenhouse",
        "family": "GREENHOUSE",
        "board": "acme",
        "careers_slug": "greenhouse-acme",
        "canonical_host": "boards.greenhouse.io",
        "list_path": "/v1/boards/acme/jobs",
        "detail": True,
        "origin_job_ids": {_GH_ID1, _GH_ID2, _GH_ID3},
        "origin_url": f"https://boards.greenhouse.io/acme/jobs/{_GH_ID1}",
        "apply_backend": f"https://boards.greenhouse.io/acme/jobs/{_GH_ID1}",
        "posted_at": "2026-08-18T06:00:00.000000Z",
        "content_marker": "local-first",
        "platform_cities": {("Berlin", "DE"), ("Paris", "FR")},
        "platform_remote_cities": {("None", "DE"), ("None", "FR")},
        "remote_mode": None,  # asserted "is not None" (Greenhouse has no
        # structured remote signal; the e2e pins the same conservative shape)
        "observations": 6,
        "attempts": 4,
    },
    "lever": {
        "adapter": "lever",
        "family": "LEVER",
        "board": "acme",
        "careers_slug": "lever-acme",
        "canonical_host": "jobs.lever.co",
        "list_path": "/v0/postings/acme",
        "detail": True,
        "origin_job_ids": {_LV_ID1, _LV_ID2, _LV_ID3},
        "origin_url": f"https://jobs.lever.co/acme/{_LV_ID1}",
        "apply_backend": f"https://jobs.lever.co/acme/{_LV_ID1}",
        "posted_at": None,  # Lever states createdAt, never a publication time
        "content_marker": "local-first",
        "platform_cities": {("Berlin", "DE"), ("Paris", "FR")},
        "platform_remote_cities": set(),
        "remote_mode": "REMOTE",
        "observations": 6,
        "attempts": 4,
    },
    "ashby": {
        "adapter": "ashby",
        "family": "ASHBY",
        "board": "acme",
        "careers_slug": "ashby-acme",
        "canonical_host": "jobs.ashbyhq.com",
        "list_path": "/posting-api/job-board/acme",
        "detail": False,  # no public per-job endpoint exists (401) — one call
        "origin_job_ids": {_AB_ID1, _AB_ID2, _AB_ID3},
        "origin_url": f"https://jobs.ashbyhq.com/acme/{_AB_ID1}",
        "apply_backend": f"https://jobs.ashbyhq.com/acme/{_AB_ID1}",
        "posted_at": "2026-06-01T09:30:00.000000Z",
        "content_marker": "ingestion pipeline",
        "platform_cities": {("Berlin", "DE"), ("Paris", "FR"), ("Lisbon", "PT")},
        "platform_remote_cities": set(),
        "remote_mode": "REMOTE",
        "observations": 3,
        "attempts": 1,
    },
}

#: Synthetic careers pages (02 §12.1 fingerprint inputs).  Each carries real
#: provider evidence kinds — script URLs, iframe hosts, canonical links and
#: HTML markers from the versioned rule table — so the classifier decides on
#: the same evidence kinds a real careers page would present.
_CAREERS_PAGES = {
    "greenhouse-acme": (
        "<!doctype html><html><head><title>Acme Fixtures Careers</title>\n"
        "<script src='https://boards.greenhouse.io/embed/job_board?for=acme'>"
        "</script>\n"
        "<link rel='canonical' href='https://boards.greenhouse.io/acme'>\n"
        "</head><body><h1>Acme Fixtures careers</h1>\n"
        "<div id='grnhse_app'></div></body></html>"
    ),
    "lever-acme": (
        "<!doctype html><html><head><title>Acme Fixtures Careers</title>\n"
        "<script src='https://jobs.lever.co/acme'></script>\n"
        "<link rel='canonical' href='https://jobs.lever.co/acme'>\n"
        "</head><body><div class='postings-group' data-qa='posting'>"
        "</div></body></html>"
    ),
    "ashby-acme": (
        "<!doctype html><html><head><title>Acme Fixtures Careers</title>\n"
        "<script src='https://jobs.ashbyhq.com/embed.js'></script>\n"
        "<link rel='canonical' href='https://jobs.ashbyhq.com/acme'>\n"
        "</head><body><iframe src='https://jobs.ashbyhq.com/acme'></iframe>"
        "</body></html>"
    ),
    #: A weak hunch: exactly one Greenhouse marker, no corroboration (0.60).
    "weak-greenhouse": (
        "<!doctype html><html><body><h1>Acme Fixtures careers</h1>"
        "<div data-department='Engineering'></div></body></html>"
    ),
    #: A generic careers page: no ATS evidence of any kind.
    "generic": (
        "<!doctype html><html><body><h1>Acme Fixtures careers</h1>"
        "<p>We are hiring across teams. Write to jobs@acme.example.</p>"
        "</body></html>"
    ),
}

#: The aggregator feed: the same three postings the employer's Greenhouse
#: board serves, seen through a third-party feed.  The links point at the
#: ATS posting URLs (one with tracking parameters), so §32 origin resolution
#: can resolve the origin identity and §38 stage 2 can merge on it.  The
#: third item carries a hostile apply link: it must survive only as evidence.
_AGGREGATOR_ITEMS = [
    {
        "id": "AGG-GH-4001",
        "title": "Backend Engineer",
        "company": "Acme Fixtures",
        "job_url": "https://boards.greenhouse.io/acme/jobs/4001?utm_source=aggregator",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/4001/apply",
        "locations": ["Berlin, Germany"],
        "description": "<p>Aggregator copy of the Acme backend role.</p>",
    },
    {
        "id": "AGG-GH-4002",
        "title": "Platform Engineer",
        "company": "Acme Fixtures",
        "job_url": "https://boards.greenhouse.io/acme/jobs/4002",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/4002/apply",
        "locations": ["Berlin, Germany", "Paris, France"],
        "description": "<p>Aggregator copy of the Acme platform role.</p>",
    },
    {
        "id": "AGG-GH-4003",
        "title": "Senior Data Engineer",
        "company": "Acme Fixtures",
        "job_url": "https://boards.greenhouse.io/acme/jobs/4003",
        "apply_url": "javascript:alert(1)",
        "locations": ["Remote"],
        "description": "<p>Aggregator copy of the Acme data role.</p>",
    },
]

_AGGREGATOR_FIELDS = {
    "source_job_id": {"path": "id", "required": True},
    "title": {"path": "title", "required": True},
    "company": {"path": "company"},
    "job_url": {"path": "job_url"},
    "apply_url": {"path": "apply_url"},
    "locations": {"path": "locations", "many": True},
    "description": {"path": "description"},
}

#: Security-negative boards, served on the Greenhouse listing route.
_OVERSIZED_BODY = b"x" * 2_100_000  # adapter cap is 2_000_000 — never parsed


class _Slice2AcceptanceHandler(http.server.BaseHTTPRequestHandler):
    """One loopback stand-in serving all three provider APIs, the careers
    pages, the aggregator feed and the security-negative endpoints."""

    def do_GET(self):  # noqa: N802 - http.server interface
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        path = parts.path

        greenhouse_list = re.fullmatch(r"/v1/boards/([a-z0-9_-]+)/jobs", path)
        greenhouse_detail = re.fullmatch(r"/v1/boards/([a-z0-9_-]+)/jobs/(\d+)", path)
        lever_list = re.fullmatch(r"/v0/postings/([a-z0-9_-]+)", path)
        lever_detail = re.fullmatch(r"/v0/postings/([a-z0-9_-]+)/([a-z0-9-]{8,40})", path)
        ashby_list = re.fullmatch(r"/posting-api/job-board/([a-z0-9_-]+)", path)
        careers = re.fullmatch(r"/careers/([a-z0-9-]+)", path)

        if greenhouse_detail is not None:
            board, job_id = greenhouse_detail.group(1), greenhouse_detail.group(2)
            fixtures = {
                (_GH_ID1,): "job_detail.json",
                (_GH_ID2,): "job_detail_multi_location.json",
                (_GH_ID3,): "job_detail_remote_hostile_links.json",
            }
            fixture = fixtures.get((job_id,), "job_removed.404.json")
            status = 200 if board == "acme" and job_id in {
                _GH_ID1, _GH_ID2, _GH_ID3,
            } else 404
            self._serve_file(_GREENHOUSE / fixture, status, _JSON)
            return
        if greenhouse_list is not None:
            board = greenhouse_list.group(1)
            if board == "acme":
                self._serve_file(_GREENHOUSE / "board_list.json", 200, _JSON)
            elif board == "redirector":
                self._redirect("http://10.255.255.5/steal")
            elif board == "loopbacker":
                port = self.server.server_address[1]
                self._redirect(f"http://localhost:{port}/v1/boards/acme/jobs")
            elif board == "oversized":
                self._serve_bytes(_OVERSIZED_BODY, 200, _JSON)
            else:
                self._serve_bytes(b"Not Found", 404, _JSON)
            return
        if lever_detail is not None:
            board, job_id = lever_detail.group(1), lever_detail.group(2)
            fixtures = {
                _LV_ID1: "posting_detail.json",
                _LV_ID2: "posting_detail_multi_location.json",
                _LV_ID3: "posting_detail_remote_hostile_links.json",
            }
            fixture = fixtures.get(job_id, "posting_removed.404.json")
            status = 200 if board == "acme" and job_id in fixtures else 404
            self._serve_file(_LEVER / fixture, status, _JSON)
            return
        if lever_list is not None:
            if lever_list.group(1) == "acme":
                self._serve_file(_LEVER / "postings_list.json", 200, _JSON)
            else:
                self._serve_bytes(b"Not Found", 404, _JSON)
            return
        if ashby_list is not None:
            if ashby_list.group(1) == "acme":
                self._serve_file(_ASHBY / "board_jobs.json", 200, _JSON)
            else:
                self._serve_bytes(b"Not Found", 404, _JSON)
            return
        if careers is not None:
            page = _CAREERS_PAGES.get(careers.group(1))
            if page is None:
                self._serve_bytes(b"Not Found", 404, "text/plain; charset=utf-8")
            else:
                self._serve_bytes(
                    page.encode("utf-8"), 200, "text/html; charset=utf-8"
                )
            return
        if path == "/aggregator/feed":
            page = int((query.get("page") or ["1"])[0])
            items = _AGGREGATOR_ITEMS if page == 1 else []
            self._serve_bytes(
                json.dumps({"jobs": items}).encode("utf-8"), 200, _JSON
            )
            return
        self._serve_bytes(b"Not Found", 404, "text/plain; charset=utf-8")

    # -- responses ----------------------------------------------------------

    def _serve_file(self, path: Path, status: int, content_type: str) -> None:
        self._serve_bytes(path.read_bytes(), status, content_type)

    def _serve_bytes(self, body: bytes, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # a capped fetch (security scenario) stops reading mid-body
            pass

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slice2AcceptanceHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def db(tmp_path, server):
    database = Database(tmp_path / "slice2-acceptance.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    provision_search(database.conn, now=NOW)
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Step 1 of the chain: the careers-page probe (02 §12.1)
# ---------------------------------------------------------------------------


def _probe(server, *, path=None, content_type="text/html", max_bytes=1_000_000,
           url_override=None):
    """Acquire one careers page through the host executor.

    Slice 2 has no generic-discovery binding yet — the router records that
    limitation honestly (``GENERIC_DISCOVERY_NOT_IMPLEMENTED``) — so the
    careers probe is a host-executor fetch, not a run request.  It still
    exercises every host security surface a run fetch would: the destination
    policy is exactly what the host derives for the provisional source
    (``driver.source_policy``), the envelope is validated before any
    connection, redirects are revalidated per hop, and caps are enforced.
    The fingerprint recorded at provisioning cites this probe's URL.
    """
    port = server.server_address[1]
    probe_url = url_override or (
        f"http://127.0.0.1:{port}{path or '/careers/generic'}"
    )
    policy = source_policy({"entry_url": probe_url})
    envelope = ExecutionPlanEnvelope(
        plan_id="plan-acceptance-probe",
        request_id="req-acceptance-probe",
        attempt_id="att-acceptance-probe",
        run_id="run-acceptance-probe",
        run_source_plan_id="rsp-acceptance-probe",
        source_id="src-acceptance-probe",
        binding_id="bnd-acceptance-probe",
        binding_revision_id="bndrev-acceptance-probe",
        adapter_id="careers-probe",
        adapter_version="0",
        strategy="GENERIC_DISCOVERY",
        execution_class="HTTP",
        policy_snapshot_ref=None,
        permission_profile_id="perm-acceptance-probe",
        permission_profile_revision=1,
        payload_kind="REQUEST",
        payload=RequestPlan(
            method="GET",
            url=probe_url,
            headers={"Accept": content_type},
            expected_content_types=(content_type,),
            timeout_s=10.0,
            max_bytes=max_bytes,
            purpose="SOURCE_DISCOVERY",
        ),
    )
    result = execute_request(envelope, policy)
    return probe_url, result


def _fingerprint_and_route(probe_url, result):
    """Steps 2–3 of the chain: classify the acquired content, then route."""
    fingerprint = classify_content(
        url=probe_url, body=result.body, content_type=result.content_type or ""
    )
    decision = plan_routes(
        fingerprint=fingerprint,
        supported_execution_classes=frozenset({"HTTP"}),
    )
    return fingerprint, decision


# ---------------------------------------------------------------------------
# Steps 4–5: durable provisioning from the route decision, then the run
# ---------------------------------------------------------------------------


def _seed_permission_profiles(db) -> None:
    db.conn.executescript(
        f"""
        INSERT INTO adapter_permission_profiles (id, display_name, created_at)
        VALUES ('perm-1', 'default', '{NOW}');
        INSERT INTO adapter_permission_profile_revisions
            (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1', 'perm-1', 1, '{{}}', '{NOW}');
        """
    )


def _provision_from_route(
    db, server, spec, *, fingerprint, decision, now=NOW
) -> ProvisionedSource:
    """Step 4: provision source + binding with the route evidence attached.

    The operator's source identity is the careers URL they added (06 §72.7);
    the specialized binding it routed to is pinned as an immutable revision
    whose config names the loopback fixture origin of this acceptance run.
    """
    conn = db.conn
    port = server.server_address[1]
    _seed_permission_profiles(db)
    definition = ensure_builtin_adapter_definition(conn, spec["adapter"], now=now)
    return provision_source_and_binding(
        conn,
        display_name=f"Acme careers via {spec['adapter']}",
        source_family="ATS_BOARD",
        entry_url=f"http://127.0.0.1:{port}/careers/{spec['careers_slug']}",
        canonical_host=spec["canonical_host"],
        adapter_id=spec["adapter"],
        adapter_version=definition.adapter_version,
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        config={
            "board": spec["board"],
            "api_base_url": f"http://127.0.0.1:{port}",
            "company_name": "Acme Fixtures",
            "careers_url": "https://acme.example/careers",
        },
        now=now,
        fingerprint=fingerprint,
        decision=decision,
    )


def _start_run(db, provisioned, *, list_path, now=NOW):
    """Step 5a: create a run whose plan mirrors the pinned revision exactly."""
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
        source_plan_group_id=f"grp-{provisioned.source_id}",
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
        target_identity=list_path,
        logical_key='{"page": 1}',
        strategy=rev["strategy"],
        execution_class=rev["execution_class"],
        now=now,
    )
    return run_id


def _seeker_profile(db, *, now=NOW) -> None:
    create_profile(
        db.conn,
        snapshot={
            "name": "Seeker",
            "keywords": ["python", "engineer"],
            "eligible_countries": ["DE", "FR", "PT"],
            "remote_rules": {"remote_ok": True},
            "min_score_inbox": 0,
        },
        now=now,
    )


def _run_provider_chain(db, server, spec_key, *, now=NOW):
    """Steps 1–5 for one provider; returns (run_id, provisioned)."""
    spec = PROVIDERS[spec_key]
    probe_url, result = _probe(server, path=f"/careers/{spec['careers_slug']}")
    assert result.failure is None
    assert result.status_code == 200 and result.body
    fingerprint, decision = _fingerprint_and_route(probe_url, result)
    provisioned = _provision_from_route(
        db, server, spec, fingerprint=fingerprint, decision=decision, now=now
    )
    _seeker_profile(db, now=now)
    run_id = _start_run(
        db, provisioned, list_path=spec["list_path"], now=now
    )
    return run_id, provisioned


# ---------------------------------------------------------------------------
# Shared read helpers (same shape as the provider e2e suites)
# ---------------------------------------------------------------------------

_ACQUISITION_TYPES = (
    "SOURCE_HEALTH_CHECK", "SOURCE_DISCOVERY", "LIST_FETCH", "SOURCE_CRAWL",
    "DETAIL_FETCH", "ADAPTER_SMOKE",
)


def _requests(db, run_id=None):
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


def _presences(db, source_id=None):
    sql = "SELECT * FROM job_sources"
    params: list = []
    if source_id is not None:
        sql += " WHERE source_id = ?"
        params.append(source_id)
    return db.conn.execute(sql + " ORDER BY source_job_id", params).fetchall()


def _evidence(db, kind=None):
    sql = "SELECT * FROM acquisition_evidence"
    params: list = []
    if kind is not None:
        sql += " WHERE kind = ?"
        params.append(kind)
    return db.conn.execute(sql + " ORDER BY created_at, id", params).fetchall()


# ---------------------------------------------------------------------------
# Scenario 1 — cross-provider full chain (the slice's central e2e claim)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec_key", sorted(PROVIDERS))
def test_a_provider_careers_url_routes_specialized_and_delivers_a_searchable_result(
    db, server, spec_key
):
    """Fingerprint → route → provision → acquire → observation → company/
    location/provenance → cleaned/indexed searchable result, for each of the
    three graduated providers (02 §12/§19/§22/§31/§32, ACQ-02/03/04/09,
    03 §40; 06 §72.5/7/8/9/13/14)."""
    spec = PROVIDERS[spec_key]
    run_id, provisioned = _run_provider_chain(db, server, spec_key)

    # ---- the route decision and its causal fingerprint are durable
    fingerprint_row = db.conn.execute(
        "SELECT * FROM ats_fingerprints WHERE id = ?", (provisioned.fingerprint_id,)
    ).fetchone()
    assert fingerprint_row["family"] == spec["family"]
    assert fingerprint_row["recommended_adapter_id"] == spec["adapter"]
    assert fingerprint_row["confidence"] >= 0.70
    assert json.loads(fingerprint_row["evidence_json"]), "evidence-first: no naked confidence"
    decision_row = db.conn.execute(
        "SELECT * FROM source_route_decisions WHERE id = ?",
        (provisioned.route_decision_id,),
    ).fetchone()
    assert decision_row["outcome"] == "SPECIALIZED"
    assert decision_row["fingerprint_family"] == spec["family"]
    candidates = json.loads(decision_row["candidates_json"])
    assert candidates and candidates[0]["adapter_id"] == spec["adapter"]
    assert candidates[0]["strategy"] == "PROVIDER_NATIVE"
    assert candidates[0]["execution_class"] == "HTTP"

    assert execute_run(db.conn, run_id) == "SUCCEEDED"

    # ---- requests: one enumeration (+ typed detail children where the
    # provider has a detail surface), all bound to the pinned revision
    requests = _requests(db, run_id)
    expected_types = ["LIST_FETCH"] + (
        ["DETAIL_FETCH"] * 3 if spec["detail"] else []
    )
    assert [r["request_type"] for r in requests] == expected_types
    assert all(r["status"] == "SUCCEEDED" for r in requests)
    assert requests[0]["page_class"] == "VALID_LIST"
    if spec["detail"]:
        listing = requests[0]
        for detail in requests[1:]:
            assert detail["page_class"] == "VALID_JOB"
            assert detail["parent_request_id"] == listing["id"]
            assert detail["depth"] == 1
            assert detail["strategy"] == "PROVIDER_NATIVE"

    # ---- every fetch went to the pinned loopback origin, nothing else
    port = server.server_address[1]
    for attempt in db.conn.execute("SELECT * FROM fetch_attempts").fetchall():
        assert attempt["requested_url"].startswith(f"http://127.0.0.1:{port}")
        assert attempt["status_code"] == 200
        assert attempt["failure_kind"] is None

    # ---- canonical jobs: one row per posting, one ACTIVE presence each,
    # with full observation provenance (06 §72.14)
    jobs = _jobs(db)
    assert [j["title"] for j in jobs] == [
        "Backend Engineer", "Platform Engineer", "Senior Data Engineer",
    ]
    presences = _presences(db, provisioned.source_id)
    assert len(presences) == 3
    assert {p["source_job_id"] for p in presences} == spec["origin_job_ids"]
    for presence in presences:
        assert presence["presence_state"] == "ACTIVE"
        assert presence["content_kind"] == "STRUCTURED"
        assert presence["source_quality_class"] == "EMPLOYER_STRUCTURED_ATS"
        # 02 §31: the discovery URL is never overwritten by origin resolution
        assert presence["raw_source_url"].startswith(f"http://127.0.0.1:{port}")
    for observation in db.conn.execute("SELECT * FROM job_observations").fetchall():
        assert observation["adapter_id"] == spec["adapter"]
        assert observation["adapter_version"] == "1.0.0"
        assert observation["strategy"] == "PROVIDER_NATIVE"
        assert observation["execution_class"] == "HTTP"
        assert observation["fetch_attempt_id"] and observation["parse_attempt_id"]

    # ---- 02 §32 origin resolution: the ATS identity, resolved with evidence
    for job in jobs:
        assert job["origin_provider"] == spec["family"]
        assert job["origin_board"] == "acme"
        assert job["origin_job_id"] in spec["origin_job_ids"]
    backend_presence = db.conn.execute(
        "SELECT * FROM job_sources WHERE source_job_id = ?",
        (sorted(spec["origin_job_ids"])[0],),
    ).fetchone()
    assert backend_presence["origin_provider"] == spec["family"]
    assert backend_presence["origin_board"] == "acme"
    assert backend_presence["origin_resolution_confidence"] >= 0.9
    resolution = json.loads(backend_presence["origin_resolution_evidence_json"])
    assert resolution["status"] == "RESOLVED"
    assert resolution["origin_url"] == spec["origin_url"]
    assert resolution["resolver_version"] and resolution["endpoint_rules_version"]
    assert any(
        item["kind"] == "ATS_ENDPOINT" for item in resolution["evidence"]
    ), "the resolution names the endpoint patterns it matched"

    # ---- one company across every observation, identified strongly (06 §72.8)
    company_ids = {job["company_id"] for job in jobs}
    assert len(company_ids) == 1 and None not in company_ids
    company = db.conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_ids.pop(),)
    ).fetchone()
    assert company["name"] == "Acme Fixtures"  # binding config, never a guess
    assert company["ats_provider"] == spec["family"]
    assert company["ats_board"] == "acme"
    assert company["domain"] == "acme.example"
    identifiers = db.conn.execute(
        "SELECT value FROM company_identifiers WHERE company_id = ?",
        (company["id"],),
    ).fetchall()
    assert {row["value"] for row in identifiers} >= {
        f"{spec['family']}/acme", "acme.example",
    }

    # ---- multi-location projection from the provider's own evidence
    backend = jobs[0]
    platform = jobs[1]
    senior = jobs[2]
    backend_locations = db.conn.execute(
        "SELECT city, country, remote FROM job_locations WHERE job_id = ?",
        (backend["id"],),
    ).fetchall()
    assert [(r["city"], r["country"], r["remote"]) for r in backend_locations] == [
        ("Berlin", "DE", 0)
    ]
    platform_locations = db.conn.execute(
        "SELECT city, country, remote FROM job_locations WHERE job_id = ?",
        (platform["id"],),
    ).fetchall()
    assert {
        (r["city"], r["country"]) for r in platform_locations if r["remote"] == 0
    } == spec["platform_cities"]
    assert {
        (str(r["city"]), r["country"]) for r in platform_locations if r["remote"] == 1
    } == spec["platform_remote_cities"]
    senior_locations = db.conn.execute(
        "SELECT remote FROM job_locations WHERE job_id = ?", (senior["id"],)
    ).fetchall()
    assert senior_locations and all(r["remote"] == 1 for r in senior_locations)
    if spec["remote_mode"] is not None:
        assert senior["remote_mode"] == spec["remote_mode"]
    else:
        assert senior["remote_mode"] is not None
    # a bare "Remote" is not evidence of unrestricted scope (Slice-1 honesty)
    assert senior["remote_worldwide"] == 0

    # ---- normalized facts, cleaned content, safe direct apply link
    assert backend["normalized_title"] == "backend engineer"
    assert backend["employment_type"] == "FULL_TIME"
    assert backend["posted_at"] == spec["posted_at"]
    assert backend["listing_status"] == "ACTIVE"
    assert spec["content_marker"] in (backend["description_text"] or "")
    assert backend["content_cleaning_version"], "cleaning outcome is durable"
    assert best_application_url(db.conn, backend["id"]) == spec["apply_backend"]

    # hostile residue in the reviewed fixtures never survives cleaning
    for job in jobs:
        for text in (job["description_md"] or "", job["description_text"] or ""):
            assert "<script" not in text.lower()
            assert "javascript:" not in text.lower()
            assert "data:text/html" not in text.lower()
    assert db.conn.execute(
        "SELECT COUNT(*) FROM parse_attempts WHERE review_evidence_json LIKE '%UNSAFE_URL_REFUSED%'"
    ).fetchone()[0] >= 1

    # ---- coverage: terminal enumeration proven, absence authority earned
    rows = _coverage(db)
    assert len(rows) == 1
    cov = rows[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["absence_inference_allowed"] == 1
    assert cov["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
    contributing = db.conn.execute(
        "SELECT request_id FROM coverage_contributing_request WHERE coverage_id = ?",
        (cov["id"],),
    ).fetchall()
    assert [r["request_id"] for r in contributing] == [requests[0]["id"]]

    # ---- the evidence spine is durable at every hop
    assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == spec["attempts"]
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == spec["attempts"]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_observations"
    ).fetchone()[0] == spec["observations"]
    assert len(_evidence(db, kind="RESULT_ENVELOPE")) == spec["attempts"]
    assert len(_evidence(db, kind="PAGE_VALIDITY")) == spec["attempts"]

    # ---- honest run accounting, plan satisfied
    run = db.conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "SUCCEEDED"
    assert run["jobs_discovered"] == spec["observations"]
    assert run["requests_failed"] == 0
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"] == "SATISFIED"

    # ---- obligations drained: Inbox-visible (06 §72.13/§72.14)
    assert db.conn.execute("SELECT COUNT(*) FROM job_eligibility").fetchone()[0] == 3
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_profile_inbox_events"
        " WHERE event_kind = 'NEW_ELIGIBLE_APPEARANCE'"
    ).fetchone()[0] == 3

    # ---- and the result is searchable in the FTS5 index (06 §72.13)
    capability = read_capability(db.conn)
    assert capability["mode"] == SEARCH_MODE_FTS5
    result = search_jobs(db.conn, query="engineer")
    assert result.mode == SEARCH_MODE_FTS5
    assert result.warning is None
    assert result.total == 3
    assert {hit.job_id for hit in result.hits} == {j["id"] for j in jobs}


# ---------------------------------------------------------------------------
# Scenario 2 — multi-source same-origin merge with employer provenance
# ---------------------------------------------------------------------------


def _provision_aggregator(db, server, *, now=NOW) -> ProvisionedSource:
    """The third-party feed source: json_api_feed over the loopback feed."""
    conn = db.conn
    port = server.server_address[1]
    if conn.execute("SELECT 1 FROM adapter_permission_profiles").fetchone() is None:
        _seed_permission_profiles(db)
    definition = ensure_builtin_adapter_definition(conn, "json_api_feed", now=now)
    return provision_source_and_binding(
        conn,
        display_name="Acme aggregator feed",
        source_family="PUBLIC_BOARD",
        entry_url=f"http://127.0.0.1:{port}/aggregator/feed",
        canonical_host=None,
        adapter_id="json_api_feed",
        adapter_version=definition.adapter_version,
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        config={
            "url_template": f"http://127.0.0.1:{port}/aggregator/feed?page={{page}}",
            "items_path": "jobs",
            "fields": _AGGREGATOR_FIELDS,
        },
        now=now,
    )


def test_an_aggregator_and_the_employer_ats_merge_on_origin_identity_with_the_employer_presenting(
    db, server
):
    """Same-origin merge (01 §38 stage 2) with §39 provenance selection.

    The employer's Greenhouse board and a third-party aggregator feed observe
    the same postings.  The canonical job set must not duplicate; the employer
    presence owns the presentation; the aggregator presence stays inspectable;
    and a hostile ``javascript:`` apply candidate in the feed never becomes a
    product link (PROD-05) — the winner's derived link is what the product
    shows."""
    # ---- the employer's own board first (the full chain from scenario 1)
    employer_run, employer = _run_provider_chain(db, server, "greenhouse")
    assert execute_run(db.conn, employer_run) == "SUCCEEDED"
    jobs_before = {j["title"]: j["id"] for j in _jobs(db)}
    assert len(jobs_before) == 3

    # ---- then the aggregator feed observes the same postings
    aggregator = _provision_aggregator(db, server, now=LATER)
    assert aggregator.source_id != employer.source_id
    agg_run = _start_run(
        db, aggregator, list_path="/aggregator/feed?page=1", now=LATER
    )
    assert execute_run(db.conn, agg_run) == "SUCCEEDED"

    # ---- one canonical job per posting: merged, never duplicated
    jobs_after = {j["title"]: j["id"] for j in _jobs(db)}
    assert jobs_after == jobs_before

    # ---- both presences exist per job, both ACTIVE (presentation, not deletion)
    employer_presences = _presences(db, employer.source_id)
    aggregator_presences = _presences(db, aggregator.source_id)
    assert len(employer_presences) == 3 and len(aggregator_presences) == 3
    for presence in employer_presences + aggregator_presences:
        assert presence["presence_state"] == "ACTIVE"
    assert {r["job_id"] for r in db.conn.execute(
        "SELECT DISTINCT job_id FROM job_sources").fetchall()} == set(
        jobs_before.values()
    )

    # ---- the §39 class ordering picked the employer for every presentation
    for title, job_id in jobs_before.items():
        winner = db.conn.execute(
            "SELECT js.* FROM jobs j JOIN job_sources js ON js.id = j.canonical_provenance_id"
            " WHERE j.id = ?",
            (job_id,),
        ).fetchone()
        assert winner["source_id"] == employer.source_id
        assert winner["source_quality_class"] == "EMPLOYER_STRUCTURED_ATS"
        aggregator_presence = db.conn.execute(
            "SELECT * FROM job_sources WHERE job_id = ? AND source_id = ?",
            (job_id, aggregator.source_id),
        ).fetchone()
        # the aggregator's resolved origin identity is exactly the employer's
        assert aggregator_presence["origin_provider"] == "GREENHOUSE"
        assert aggregator_presence["origin_board"] == "acme"
        assert aggregator_presence["origin_job_id"] is not None
        assert aggregator_presence["source_quality_class"] == (
            "AGGREGATOR_WITH_RESOLVED_ORIGIN"
        )
        # its own discovery URL is preserved untouched (02 §31)
        assert aggregator_presence["raw_source_url"].startswith(
            f"http://127.0.0.1:{server.server_address[1]}/aggregator/feed"
        )

    # ---- the employer's presentation was not churned by the weaker arrival
    backend = db.conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (jobs_before["Backend Engineer"],)
    ).fetchone()
    assert backend["description_text"] and "local-first" in backend["description_text"]
    locations = db.conn.execute(
        "SELECT city, country, remote FROM job_locations WHERE job_id = ?",
        (backend["id"],),
    ).fetchall()
    assert [(r["city"], r["country"], r["remote"]) for r in locations] == [
        ("Berlin", "DE", 0)
    ]

    # ---- one company still, with the aggregator attached by strong evidence
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1

    # ---- tracking cleanup: the aggregator's utm-tagged link resolved cleanly
    tracked = db.conn.execute(
        "SELECT origin_resolution_evidence_json FROM job_sources"
        " WHERE source_id = ? AND source_job_id = 'AGG-GH-4001'",
        (aggregator.source_id,),
    ).fetchone()
    tracked_resolution = json.loads(tracked["origin_resolution_evidence_json"])
    assert tracked_resolution["status"] == "RESOLVED"
    assert "utm_" not in tracked_resolution["origin_url"]
    assert tracked_resolution["origin_url"] == (
        "https://boards.greenhouse.io/acme/jobs/4001"
    )

    # ---- the hostile apply candidate is evidence, never a product link
    hostile = db.conn.execute(
        "SELECT * FROM job_sources WHERE source_id = ? AND source_job_id = 'AGG-GH-4003'",
        (aggregator.source_id,),
    ).fetchone()
    assert hostile["application_url"] == "javascript:alert(1)"  # raw claim kept
    assert safe_external_url(hostile["application_url"]) is None
    data_job_id = jobs_before["Senior Data Engineer"]
    # the product shows the winner's derived link and nothing else
    assert best_application_url(db.conn, data_job_id) == (
        "https://boards.greenhouse.io/acme/jobs/4003"
    )
    # no durable presence row can yield an unsafe product link (PROD-05)
    for presence in db.conn.execute("SELECT application_url FROM job_sources").fetchall():
        emitted = safe_external_url(presence["application_url"])
        assert emitted is None or emitted.lower().startswith(("http://", "https://"))


# ---------------------------------------------------------------------------
# Scenario 3 — a generic careers URL falls back honestly
# ---------------------------------------------------------------------------


def _record_fallback_source(db, server, *, slug, source_family="EMPLOYER_CAREERS"):
    """Record the operator's source identity plus the fallback evidence.

    With no runnable route there is no binding to provision: the honest slice-2
    state for this source is the recorded fingerprint + fallback decision and
    nothing runnable.  ``record_fingerprint``/``record_route_decision`` are the
    host's own evidence writers (the same ones provisioning uses).
    """
    conn = db.conn
    port = server.server_address[1]
    entry_url = f"http://127.0.0.1:{port}/careers/{slug}"
    probe_url, result = _probe(server, path=f"/careers/{slug}")
    assert probe_url == entry_url and result.failure is None
    fingerprint, decision = _fingerprint_and_route(probe_url, result)
    conn.execute(
        "INSERT INTO sources (id, display_name, source_family, entry_url,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"src-{slug}", f"Acme careers ({slug})", source_family, entry_url, NOW, NOW),
    )
    conn.commit()
    fingerprint_id = record_fingerprint(
        conn, source_id=f"src-{slug}", url=probe_url, fingerprint=fingerprint, now=NOW
    )
    decision_id = record_route_decision(
        conn,
        source_id=f"src-{slug}",
        fingerprint=fingerprint,
        decision=decision,
        now=NOW,
    )
    return fingerprint, decision, fingerprint_id, decision_id


def test_a_generic_careers_url_falls_back_honestly_without_a_fabricated_route(
    db, server
):
    """No ATS evidence → GENERIC_DISCOVERY_FALLBACK, no runnable candidate.

    The fallback must be honest in both directions: no specialized route is
    forced (ARC-08, 02 §12.1), and no fictitious generic adapter is advertised
    either — the missing generic route is durable unsupported evidence
    (``GENERIC_DISCOVERY_NOT_IMPLEMENTED``), exactly the implemented Slice-2
    truth."""
    adapters_before = dict(BUILTIN_ADAPTERS)
    fingerprint, decision, fingerprint_id, decision_id = _record_fallback_source(
        db, server, slug="generic"
    )

    # the fingerprint honestly says "no family"
    assert fingerprint.family is None
    assert fingerprint.confidence == 0.0
    assert fingerprint.recommended_adapter_id is None

    # the router refuses both fabrications: no specialized candidate and no
    # invented generic adapter — the fallback reason is durable and readable
    assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK
    assert decision.candidates == ()
    assert decision.fallback_reason
    unsupported = [(u.strategy, u.execution_class, u.reason) for u in decision.unsupported_candidates]
    assert ("HTTP_HTML", "HTTP", "GENERIC_DISCOVERY_NOT_IMPLEMENTED") in unsupported

    # the evidence is durable against the operator's source identity
    row = db.conn.execute(
        "SELECT * FROM ats_fingerprints WHERE id = ?", (fingerprint_id,)
    ).fetchone()
    assert row["family"] is None and row["confidence"] == 0.0
    assert row["url"].endswith("/careers/generic")
    decision_row = db.conn.execute(
        "SELECT * FROM source_route_decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    assert decision_row["outcome"] == "GENERIC_DISCOVERY_FALLBACK"
    assert json.loads(decision_row["candidates_json"]) == []
    assert decision_row["fallback_reason"]

    # nothing runnable was invented: no binding, no revision, no new adapter
    assert db.conn.execute(
        "SELECT COUNT(*) FROM source_adapter_bindings b"
        " JOIN sources s ON s.id = b.source_id WHERE s.entry_url LIKE '%/careers/generic'"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM source_adapter_binding_revisions"
    ).fetchone()[0] == 0
    assert dict(BUILTIN_ADAPTERS) == adapters_before
    assert "generic" not in BUILTIN_ADAPTERS


def test_a_low_confidence_provider_hunch_never_forces_a_specialized_route(
    db, server
):
    """One weak Greenhouse marker (0.60 < 0.70) is evidence, not a route.

    02 §12.1: low-confidence fingerprinting MUST fall back to generic
    discovery rather than silently forcing a specialized adapter.  The family
    hunch is recorded as evidence; the route decision emits no candidate; and
    no specialized binding is provisioned from the hunch."""
    fingerprint, decision, fingerprint_id, decision_id = _record_fallback_source(
        db, server, slug="weak-greenhouse"
    )

    assert fingerprint.family == "GREENHOUSE"
    assert fingerprint.confidence < 0.70
    assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK
    assert decision.fingerprint_family == "GREENHOUSE"
    assert decision.candidates == ()
    assert "below threshold" in (decision.fallback_reason or "")

    row = db.conn.execute(
        "SELECT * FROM ats_fingerprints WHERE id = ?", (fingerprint_id,)
    ).fetchone()
    assert row["family"] == "GREENHOUSE"
    decision_row = db.conn.execute(
        "SELECT * FROM source_route_decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    assert decision_row["outcome"] == "GENERIC_DISCOVERY_FALLBACK"
    assert json.loads(decision_row["candidates_json"]) == []
    assert db.conn.execute(
        "SELECT COUNT(*) FROM source_adapter_binding_revisions"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Scenario 5 — FTS mode honesty in both capability states (01 §45)
# ---------------------------------------------------------------------------


def test_search_capability_is_reported_truthfully_in_both_modes(
    db, server, tmp_path, monkeypatch
):
    """FTS5_ACTIVE when FTS5 is real; SUBSTRING_FALLBACK with the explicit
    warning when it is not — same provisioning and query surface, so the
    reported mode cannot drift from what is actually serving the query."""
    # ---- state 1: this host really has FTS5 → FTS5_ACTIVE, no warning
    fts_run, _ = _run_provider_chain(db, server, "ashby")
    assert execute_run(db.conn, fts_run) == "SUCCEEDED"
    capability = read_capability(db.conn)
    assert capability["mode"] == SEARCH_MODE_FTS5
    assert capability["fts5_detected"] == 1
    assert capability["warning"] is None
    fts_result = search_jobs(db.conn, query="engineer")
    assert fts_result.mode == SEARCH_MODE_FTS5
    assert fts_result.warning is None
    assert fts_result.total == 3
    assert fts_table_present(db.conn)

    # ---- state 2: a host whose SQLite reports no FTS5 → honest fallback
    monkeypatch.setattr(
        "jobscraper.search.provision.fts5_available", lambda _conn: False
    )
    fallback_db = Database(tmp_path / "slice2-acceptance-substring.db")
    try:
        migrate_schema(fallback_db.conn, LATEST_SCHEMA_VERSION)
        provisioned = provision_search(fallback_db.conn, now=NOW)
        assert provisioned["mode"] == SEARCH_MODE_SUBSTRING
        assert provisioned["fts5_detected"] is False
        assert provisioned["warning"]
        capability = read_capability(fallback_db.conn)
        assert capability["mode"] == SEARCH_MODE_SUBSTRING
        assert capability["fts5_detected"] == 0
        assert "BM25" in capability["warning"]
        assert not fts_table_present(fallback_db.conn)

        # the same full chain delivers the same jobs to the same query surface
        fallback_run, _ = _run_provider_chain(fallback_db, server, "ashby")
        assert execute_run(fallback_db.conn, fallback_run) == "SUCCEEDED"
        substring_result = search_jobs(fallback_db.conn, query="engineer")
        assert substring_result.mode == SEARCH_MODE_SUBSTRING
        assert substring_result.warning
        assert substring_result.total == 3
        assert {hit.title for hit in substring_result.hits} == {
            "Backend Engineer", "Platform Engineer", "Senior Data Engineer",
        }
        # the fallback never claims BM25/FTS
        assert substring_result.mode != SEARCH_MODE_FTS5
    finally:
        fallback_db.close()

    # ---- state 3: drift — a recorded FTS5_ACTIVE whose index objects are
    # gone must degrade the *answer*, not claim BM25 over nothing (01 §45)
    record_capability(
        db.conn, mode=SEARCH_MODE_FTS5, fts5_detected=True, warning=None, now=LATER
    )
    db.conn.execute(f"DROP TABLE {FTS_TABLE}")
    db.conn.commit()
    assert not fts_table_present(db.conn) or not triggers_exist(db.conn)
    drift_result = search_jobs(db.conn, query="engineer")
    assert drift_result.mode == SEARCH_MODE_SUBSTRING
    assert drift_result.warning and "missing or incomplete" in drift_result.warning


# ---------------------------------------------------------------------------
# Scenario 6 — security negatives stay denied (04 §5.1, SEC-02/03/09)
# ---------------------------------------------------------------------------


def _run_greenhouse_board(db, server, *, board, now=NOW):
    """Run one Greenhouse listing by direct provisioning (negative path)."""
    conn = db.conn
    port = server.server_address[1]
    _seed_permission_profiles(db)
    definition = ensure_builtin_adapter_definition(conn, "greenhouse", now=now)
    provisioned = provision_source_and_binding(
        conn,
        display_name=f"{board} board",
        source_family="ATS_BOARD",
        entry_url=f"http://127.0.0.1:{port}/{board}",
        canonical_host="boards.greenhouse.io",
        adapter_id="greenhouse",
        adapter_version=definition.adapter_version,
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        config={
            "board": board,
            "api_base_url": f"http://127.0.0.1:{port}",
            "company_name": "Acme Fixtures",
        },
        now=now,
    )
    run_id = _start_run(
        db, provisioned, list_path=f"/v1/boards/{board}/jobs", now=now
    )
    return run_id, provisioned


def _assert_denied_and_degraded(db, run_id):
    """The shared durable shape of a denied fetch: evidence, no parse, no
    observations, and no absence authority from a fetch that never completed."""
    assert execute_run(db.conn, run_id) == "PARTIAL"
    requests = _requests(db, run_id)
    assert [r["request_type"] for r in requests] == ["LIST_FETCH"]
    assert requests[0]["page_class"] == "UNKNOWN"
    # a denial never reaches the parser (ACQ-02) and never yields a job
    assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0
    assert _jobs(db) == []
    # the denial itself is durable evidence at both hops it has (04 §5.1)
    attempts = db.conn.execute("SELECT * FROM fetch_attempts").fetchall()
    assert len(attempts) == 1
    assert attempts[0]["failure_kind"] == "POLICY_REJECTED"
    failure = json.loads(attempts[0]["failure_json"])
    assert failure["kind"] == "POLICY_REJECTED"
    security = _evidence(db, kind="SECURITY_POLICY")
    assert len(security) == 1
    assert security[0]["ref"].startswith("DENIED:")
    policy_json = json.loads(attempts[0]["security_policy_json"])
    assert policy_json["result"].startswith("DENIED:")
    # no absence authority from a denied generation (ACQ-03, RUN-13): the
    # generation finalizes PARTIAL with no proven terminal enumeration, and
    # finalize_coverage applies absence evidence only to COMPLETE generations
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0


def test_a_redirect_to_a_private_destination_is_denied_with_durable_evidence(
    db, server
):
    """A run whose listing redirects to a private address (SSRF shape) is
    denied at the redirect hop — per-hop revalidation (04 §5.1) — and the
    denial degrades the run instead of being read as an empty board.

    The first shield here is the source allow-host policy: the redirect
    target is not the source's host, so the hop is ``HOST_NOT_ALLOWED``
    before any address is resolved.  The address-classification net beneath
    it (a private literal, or a public name resolving private) is pinned by
    ``test_a_private_destination_is_denied_by_address_classification``."""
    run_id, _ = _run_greenhouse_board(db, server, board="redirector")
    _assert_denied_and_degraded(db, run_id)
    security = _evidence(db, kind="SECURITY_POLICY")[0]
    assert security["ref"] == "DENIED:HOST_NOT_ALLOWED"
    attempt = db.conn.execute("SELECT failure_json FROM fetch_attempts").fetchone()
    assert json.loads(attempt["failure_json"])["details_redacted"]["reason_code"] == (
        "HOST_NOT_ALLOWED"
    )


def test_a_private_destination_is_denied_by_address_classification(server):
    """SEC-02: a private-range destination is denied by address
    classification before any connection exists — the net beneath the
    allow-host policy that also catches public names resolving private."""
    _, result = _probe(
        server,
        path="/careers/generic",
        url_override="http://10.255.255.5/careers",
    )
    assert result.failure is not None
    assert result.failure.kind.value == "POLICY_REJECTED"
    assert result.failure.details_redacted["reason_code"] == "PRIVATE_ADDRESS"
    assert result.body == b""


def test_a_redirect_to_another_loopback_identity_is_denied(db, server):
    """The narrow host rule grants the exact loopback literal the source entry
    names — a redirect to ``localhost`` (a different loopback identity) is
    HOST_NOT_ALLOWED, not silently same-host (04 §5.1, SEC-09)."""
    run_id, _ = _run_greenhouse_board(db, server, board="loopbacker")
    _assert_denied_and_degraded(db, run_id)
    attempt = db.conn.execute("SELECT failure_json FROM fetch_attempts").fetchone()
    failure = json.loads(attempt["failure_json"])
    assert failure["details_redacted"]["reason_code"] == "HOST_NOT_ALLOWED"


def test_an_oversized_body_is_capped_and_typed_never_parsed(db, server):
    """A body over the cap is cut off at the cap and typed BODY_TOO_LARGE —
    never parsed, never read as an empty board, never stored whole."""
    run_id, _ = _run_greenhouse_board(db, server, board="oversized")
    _assert_denied_and_degraded(db, run_id)
    attempt = db.conn.execute(
        "SELECT bytes_downloaded, failure_json FROM fetch_attempts"
    ).fetchone()
    failure = json.loads(attempt["failure_json"])
    assert failure["details_redacted"]["reason_code"] == "BODY_TOO_LARGE"
    # the cap is the adapter's 2_000_000 bound — the body was hard-stopped at
    # the cap (one 64 KiB read chunk of overshoot is the read-loop granularity)
    assert attempt["bytes_downloaded"] <= 2_000_000 + 64 * 1024


def test_a_javascript_probe_url_is_refused_before_any_connection(db, server):
    """A ``javascript:`` fetch target is refused by the scheme gate at the
    acquisition boundary (04 §5.1, SEC-03): typed POLICY_REJECTED with no
    network I/O and no body to parse."""
    _, result = _probe(
        server,
        path="/careers/generic",
        url_override="javascript:alert(1)",
    )
    assert result.failure is not None
    assert result.failure.kind.value == "POLICY_REJECTED"
    assert result.failure.details_redacted["reason_code"] == "SCHEME_FORBIDDEN"
    assert result.body == b""
    assert result.status_code is None
