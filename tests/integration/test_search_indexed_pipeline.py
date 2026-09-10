"""S2.3 integration tests: cleaned content -> FTS5 index -> /api/search.

Authority: docs/plans/slice-2-worker-implementation-plan-v0313.md S2.3;
docs/spec/v0.3.1.3/01_product_and_workflow.md §34/§45; 03 §30 evidence.

Proves end to end that a job entering through the canonical pipeline:

* is cleaned by the versioned content cleaner, with the cleaning version
  recorded on the canonical row and a CONTENT_CLEANING evidence row on its
  observation;
* is indexed exactly once into ``job_search_docs``/``job_search_state``
  (re-observation and stale re-sync cannot duplicate or lose rows);
* is searchable over ``GET /api/search`` behind the session gate with the
  active mode reported honestly (no unauthenticated access, no fake BM25);
* structured filters (listing status, source, company, remote) apply outside
  FTS.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    FieldEvidenceRecord,
    ObservationRecord,
)
from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.contentclean import CONTENT_CLEANING_VERSION
from jobscraper.pipeline.ingest import ingest_observation
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run
from jobscraper.search.capability import SEARCH_MODE_FTS5
from jobscraper.search.provision import FTS_TABLE, provision_search
from jobscraper.search.query import search_jobs

NOW = "2026-09-08T09:00:00.000000Z"
LATER = "2026-09-08T11:00:00.000000Z"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "s23.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-feed','Fixture Feed','PUBLIC_FEED','https://jobs.example.test/api/jobs', '{NOW}', '{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}', '{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','default','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-feed','src-feed','api','{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, created_at)
        VALUES ('bndrev-feed','bnd-feed',1,'json_api_feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,'{NOW}');
        """
    )
    provision_search(database.conn, now=NOW)
    # Model the service lifetime: the coordinator opens its service epoch
    # before any claiming (03 §50; claims fail closed without one).
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    yield database
    database.close()


def _run_and_request(db, now=NOW):
    plan = dict(
        source_id="src-feed",
        source_plan_group_id="grp-src-feed",
        fallback_rank=0,
        binding_id="bnd-feed",
        binding_revision_id="bndrev-feed",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
    )
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=now)
    rid, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plans[0],
        source_id="src-feed",
        binding_id="bnd-feed",
        request_type="LIST_FETCH",
        target_identity="https://jobs.example.test/api/jobs?page=1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        now=now,
    )
    claim = claim_next_request(db.conn, "worker-s23", now=now)
    assert claim is not None and claim.request_id == rid
    return run_id, plans[0], rid, claim.attempt_id


def _obs(source_job_id="fx-200"):
    return ObservationRecord(
        source_job_id=source_job_id,
        raw_url="https://jobs.example.test/api/jobs?page=1",
        canonical_url_candidate=f"https://jobs.example.test/jobs/{source_job_id}",
        application_url_candidate=f"https://jobs.example.test/jobs/{source_job_id}/apply",
        fields={
            "source_job_id": source_job_id,
            "title": "Senior Backend Engineer",
            "company": "Fixture Corp",
            "description": (
                "<h1>Senior Backend Engineer</h1><p>Build <strong>reliable</strong> python"
                ' services in Berlin.</p><p>Apply <a href="https://jobs.example.test/apply?'
                "utm_source=feed\">here</a>.</p>"
            ),
            "job_url": f"https://jobs.example.test/jobs/{source_job_id}",
            "apply_url": f"https://jobs.example.test/jobs/{source_job_id}/apply",
            "locations": ["Berlin, Germany"],
            "posted_at": "2026-09-01T00:00:00.000000Z",
        },
        field_evidence=(FieldEvidenceRecord("title", "json_path", "title", "h1", "Senior Backend Engineer"),),
    )


def _ingest(db, observation, request_id, attempt_id, now=NOW):
    def mutate(conn):
        ingest_observation(
            conn,
            request_id=request_id,
            attempt_id=attempt_id,
            observation=observation,
            source_id="src-feed",
            binding_id="bnd-feed",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            observed_at=now,
            now=now,
        )

    with fenced_commit(db.conn, request_id, attempt_id, now=now, outcome="SUCCEEDED", mutate=mutate):
        pass


# ------------------------------------------------ canonical -> search spine


def test_ingested_job_is_cleaned_indexed_and_evidence_marked(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att)

    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    assert job["content_cleaning_version"] == CONTENT_CLEANING_VERSION
    assert job["description_text"] == "Senior Backend Engineer\n\nBuild reliable python services in Berlin.\n\nApply here."
    assert "utm_source" not in job["description_text"]

    # cleaning is durable evidence on the observation (03 §30, append-only)
    ev = db.conn.execute(
        "SELECT * FROM acquisition_evidence WHERE kind = 'CONTENT_CLEANING'"
    ).fetchone()
    assert ev is not None
    assert ev["ref"] == CONTENT_CLEANING_VERSION
    detail = json.loads(ev["detail_json"])
    assert detail["description_hash"] == job["description_hash"]

    # the search document exists exactly once with its revision state
    docs = db.conn.execute("SELECT * FROM job_search_docs").fetchall()
    assert len(docs) == 1
    state = db.conn.execute("SELECT * FROM job_search_state").fetchall()
    assert len(state) == 1
    assert docs[0]["description_text"] == job["description_text"]
    assert "Senior Backend Engineer" in docs[0]["title"]
    assert "Berlin, Germany" in docs[0]["locations_text"]

    result = search_jobs(db.conn, query="reliable python")
    assert result.mode == SEARCH_MODE_FTS5
    assert result.total == 1
    assert result.hits[0].job_id == job["id"]
    assert "Berlin, Germany" in result.hits[0].locations


def test_reobservation_cannot_duplicate_docs_and_resync_is_idempotent(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att)
    docs_before = db.conn.execute("SELECT * FROM job_search_docs").fetchall()
    assert len(docs_before) == 1

    # a second run re-observes the same native identity at a later time
    run_id2, plan_id2, rid2, att2 = _run_and_request(db, now=LATER)
    _ingest(db, _obs(), rid2, att2, now=LATER)
    docs_after = db.conn.execute("SELECT * FROM job_search_docs").fetchall()
    assert len(docs_after) == 1
    assert docs_after[0]["description_text"] == docs_before[0]["description_text"]
    fts_count = db.conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0]
    assert fts_count == 1
    # a manual re-sync of unchanged content is a no-op
    from jobscraper.search.index import sync_search_doc

    assert sync_search_doc(db.conn, job_id=docs_after[0]["job_id"], now=LATER) is False


def test_company_source_and_status_filters_over_indexed_job(db):
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att)
    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    company = db.conn.execute("SELECT * FROM companies WHERE id = ?", (job["company_id"],)).fetchone()
    assert company is not None

    by_company = search_jobs(db.conn, query="engineer", company_id=job["company_id"])
    assert by_company.total == 1
    by_source = search_jobs(db.conn, query="engineer", source_id="src-feed")
    assert by_source.total == 1
    other_source = search_jobs(db.conn, query="engineer", source_id="src-other")
    assert other_source.total == 0
    active = search_jobs(db.conn, query="engineer", listing_status="ACTIVE")
    assert active.total == 1
    expired = search_jobs(db.conn, query="engineer", listing_status="EXPIRED")
    assert expired.total == 0


def test_cleaning_version_is_recorded_for_later_cleaner_revisions(db):
    """The canonical row records which cleaner produced the description."""
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att)
    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    # the version is durable on the row so a newer cleaner revision can never
    # silently rewrite history (RUN-21 + S2.3 versioning rule)
    assert job["content_cleaning_version"] == CONTENT_CLEANING_VERSION


# -------------------------------------------------------- /api/search route


@pytest.fixture()
def service(db, tmp_path):
    from jobscraper.paths import build_app_paths, ensure_app_directories
    from jobscraper.service.app import create_service_app

    root = tmp_path / "svc-root"
    ensure_app_directories(build_app_paths(root))
    secret = os.urandom(32)
    app, state = create_service_app(
        AppConfig(data_root=root), db, port=8766, secret=secret
    )
    yield {"app": app, "state": state, "db": db}
    # no close here: the `db` fixture owns the Database


def _client(service):
    from fastapi.testclient import TestClient

    client = TestClient(service["app"], base_url="http://127.0.0.1:8766")
    state = service["state"]
    session_id, csrf = state.sessions.create(state.instance_id)
    client.cookies.set("wjs_session", session_id)
    client.cookies.set("wjs_csrf", csrf)
    client.headers.update(
        {
            "host": "127.0.0.1:8766",
            "origin": "http://127.0.0.1:8766",
        }
    )
    return client


def test_search_route_requires_an_authenticated_session(service):
    from fastapi.testclient import TestClient

    client = TestClient(service["app"], base_url="http://127.0.0.1:8766")
    client.headers.update({"host": "127.0.0.1:8766"})
    assert client.get("/api/search", params={"q": "python"}).status_code == 401


def test_search_route_returns_hits_mode_and_filters(service):
    db = service["db"]
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att)
    job = db.conn.execute("SELECT * FROM jobs").fetchone()

    client = _client(service)
    response = client.get("/api/search", params={"q": "reliable python"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "reliable python"
    assert payload["mode"] == SEARCH_MODE_FTS5
    assert payload["warning"] is None
    assert payload["total"] == 1
    assert payload["limit"] == 20 and payload["offset"] == 0
    hit = payload["hits"][0]
    assert hit["job_id"] == job["id"]
    assert hit["company"] == "Fixture Corp"
    assert hit["score"] is not None
    assert payload["filters"]["listing_status"] == "ACTIVE"


def test_search_route_applies_filters(service):
    db = service["db"]
    run_id, plan_id, rid, att = _run_and_request(db)
    _ingest(db, _obs(), rid, att)
    job = db.conn.execute("SELECT * FROM jobs").fetchone()
    client = _client(service)
    none = client.get(
        "/api/search", params={"q": "engineer", "listing_status": "EXPIRED"}
    )
    assert none.json()["total"] == 0
    one = client.get(
        "/api/search", params={"q": "engineer", "listing_status": "ANY"}
    )
    assert one.json()["total"] == 1
    by_source = client.get(
        "/api/search", params={"q": "engineer", "source_id": "src-feed"}
    )
    assert by_source.json()["total"] == 1
    bad_source = client.get(
        "/api/search", params={"q": "engineer", "source_id": "missing"}
    )
    assert bad_source.json()["total"] == 0


def test_search_route_validates_query_and_paging(service):
    client = _client(service)
    assert client.get("/api/search", params={"q": ""}).status_code == 422
    assert client.get("/api/search").status_code == 422
    assert client.get("/api/search", params={"q": "x", "limit": 0}).status_code == 422
    assert client.get("/api/search", params={"q": "x", "limit": 1000}).status_code == 422
    assert client.get("/api/search", params={"q": "x", "limit": -1}).status_code == 422
    invalid_date = client.get(
        "/api/search", params={"q": "x", "discovered_after": "not-a-date"}
    )
    assert invalid_date.status_code == 400
