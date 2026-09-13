"""Post-S3.13 Corrective A3 — fresh-run revalidation composition proof (test-only).

Authority: docs/reviews/post-s3.13-corrective-a-enumeration-lifecycle-authority-2026-09-13.md
Plan: WJS_POST_S313_CORRECTIVE_A_REVISED_IMPLEMENTATION_PLAN_V2_2026-09-13.md §4.

Proves A1 exact-plan cursor ownership composes with the existing S3.7/S3.8
cache/coverage machinery through real Database/migrations, real create_run,
real seed enqueue_request, real execute_run, a real HTTP loopback fixture,
and the real cache/coverage/membership/presence rows. Authority-sensitive
tests use stable_full_source_enumeration=True: the default generic feed is
NO_ABSENCE_INFERENCE, which would make "no false absence" trivially true.
"""

from __future__ import annotations

import http.server
import json
import sqlite3
import threading

import pytest

from jobscraper.acquisition.crawler.revalidation import prune_representation
from jobscraper.db.connection import Database
from jobscraper.db.migrations import migrate_schema
from jobscraper.pipeline.driver import execute_run
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run
from jobscraper.version import SCHEMA_VERSION

NOW = "2026-09-13T12:00:00.000000Z"
ETAG_V1 = '"v1"'

_FIELDS = {
    "source_job_id": {"path": "id", "required": True},
    "title": {"path": "title", "required": True},
    "company": {"path": "company"},
}

# validators flag per loopback port (set by _install_stable_feed_binding).
_PORT_FLAGS: dict[int, bool] = {}


def _job(job_id: str, title: str) -> dict:
    return {"id": job_id, "title": title, "company": "Fixture Corp"}


class _FixtureState:
    def __init__(self) -> None:
        self.jobs_page1: list[dict] = [_job("A", "Role A"), _job("B", "Role B")]
        self.jobs_page2: list[dict] = []
        self.log: list[dict] = []
        self.lie_304_once: bool = False


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    state: _FixtureState = _FixtureState()  # replaced per test

    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        inm = self.headers.get("If-None-Match")
        ims = self.headers.get("If-Modified-Since")
        port = self.server.server_address[1]
        validators = _PORT_FLAGS.get(port, False)
        state = type(self).state
        if state.lie_304_once:
            state.lie_304_once = False
            state.log.append(
                {"path": self.path, "inm": inm, "ims": ims, "status": 304}
            )
            body = b""
            self.send_response(304)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.write(body)
            return
        if validators and page == 1 and inm == ETAG_V1:
            state.log.append(
                {"path": self.path, "inm": inm, "ims": ims, "status": 304}
            )
            self.send_response(304)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        jobs = state.jobs_page1 if page == 1 else state.jobs_page2
        body = json.dumps({"jobs": jobs}).encode()
        state.log.append({"path": self.path, "inm": inm, "ims": ims, "status": 200})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if validators and page == 1:
            self.send_header("ETag", ETAG_V1)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: ANN002, ANN202
        pass


@pytest.fixture()
def server():
    _FixtureHandler.state = _FixtureState()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def server_state(server):
    return _FixtureHandler.state


def _install_stable_feed_binding(conn: sqlite3.Connection, port: int, *, validators: bool) -> None:
    """Insert one source/binding stack with reviewed stable full-source config."""
    _PORT_FLAGS[port] = validators
    config = {
        "url_template": f"http://127.0.0.1:{port}/jobs?page={{page}}",
        "items_path": "jobs",
        "fields": _FIELDS,
        "stable_full_source_enumeration": True,
    }
    conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-1','A3 Feed','PUBLIC_FEED','http://127.0.0.1:{port}/jobs','{NOW}','{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}','{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-1','src-1','api','{NOW}');
        """
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id, binding_id, revision, adapter_id, adapter_version, strategy,"
        " execution_class, permission_profile_id, permission_profile_revision,"
        " config_json, created_at)"
        " VALUES ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0',"
        " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?,?)",
        (json.dumps(config), NOW),
    )
    conn.commit()


def _open_db(path, port: int, *, validators: bool) -> Database:
    database = Database(path)
    migrate_schema(database.conn, SCHEMA_VERSION)
    _install_stable_feed_binding(database.conn, port, validators=validators)
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    return database


def _create_seeded_run(conn: sqlite3.Connection, *, now: str) -> tuple[str, str]:
    """Create one run/plan, assert A2 pins, enqueue exactly one page-1 LIST_FETCH."""
    plan = dict(
        source_id="src-1",
        source_plan_group_id="grp-a3",
        fallback_rank=0,
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
    )
    run_id, plans = create_run(conn, profile_id=None, plans=[plan], now=now)
    plan_id = plans[0]
    pinned = conn.execute(
        "SELECT coverage_authority, pagination_stability FROM run_source_plans WHERE id = ?",
        (plan_id,),
    ).fetchone()
    assert pinned["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
    assert pinned["pagination_stability"] == "STABLE_SNAPSHOT"
    revision = conn.execute(
        "SELECT config_json FROM source_adapter_binding_revisions WHERE id = 'bndrev-1'"
    ).fetchone()
    seed_url = json.loads(revision["config_json"])["url_template"].format(page=1)
    enqueue_request(
        conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity=seed_url,
        logical_key='{"page": 1}',
    )
    return run_id, plan_id


def _coverage_for_plan(conn: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM enumeration_coverage WHERE run_source_plan_id = ?", (plan_id,)
    ).fetchall()
    assert len(rows) == 1, f"expected exactly one coverage for plan, got {len(rows)}"
    return rows[0]


def _cursor_for_plan(conn: sqlite3.Connection, plan_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM crawl_cursors WHERE checkpoint_run_source_plan_id = ?",
        (plan_id,),
    ).fetchone()


def _seen(conn: sqlite3.Connection, coverage_id: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id = ?",
            (coverage_id,),
        )
    }


def _reopen(path) -> Database:
    """Simulate a service boundary: fresh handle + fresh epoch over durable DB."""
    database = Database(path)
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    return database


# ---------------------------------------------------------------- A3.2


def test_fresh_run_starts_seed_page_and_sends_exact_conditional_validator(
    tmp_path, server, server_state
):
    port = server.server_address[1]
    path = tmp_path / "a3-seed.db"
    database = _open_db(path, port, validators=True)
    try:
        run1, plan_a = _create_seeded_run(database.conn, now=NOW)
        assert execute_run(database.conn, run1) == "SUCCEEDED"
        cov_a = _coverage_for_plan(database.conn, plan_a)
        assert cov_a["completion_state"] == "COMPLETE"
        cursor_a = _cursor_for_plan(database.conn, plan_a)
        assert cursor_a is not None
        assert json.loads(cursor_a["state_json"]) == {"page": 2}
        snapshot = (cursor_a["state_json"], cursor_a["guard_state_json"])
        database.close()

        database = _reopen(path)
        run2, plan_b = _create_seeded_run(database.conn, now=NOW)
        assert _cursor_for_plan(database.conn, plan_b) is None
        base = len(server_state.log)
        assert execute_run(database.conn, run2) == "SUCCEEDED"
        run2_hits = server_state.log[base:]
        assert run2_hits, "Run 2 performed no network requests"
        assert run2_hits[0]["path"] == "/jobs?page=1"
        assert run2_hits[0]["inm"] == ETAG_V1
        assert run2_hits[0]["ims"] is None
        after_a = _cursor_for_plan(database.conn, plan_a)
        assert (after_a["state_json"], after_a["guard_state_json"]) == snapshot
        cursor_b = _cursor_for_plan(database.conn, plan_b)
        assert cursor_b is not None
        assert cursor_b["checkpoint_run_source_plan_id"] == plan_b
        assert json.loads(cursor_b["state_json"]) == {"page": 2}
    finally:
        database.close()


# ---------------------------------------------------------------- A3.3


def test_accepted_304_restores_membership_into_new_coverage(tmp_path, server, server_state):
    port = server.server_address[1]
    path = tmp_path / "a3-304.db"
    database = _open_db(path, port, validators=True)
    try:
        run1, plan_a = _create_seeded_run(database.conn, now=NOW)
        assert execute_run(database.conn, run1) == "SUCCEEDED"
        reps = database.conn.execute(
            "SELECT id FROM cache_representation WHERE validated_page_class = 'VALID_LIST'"
        ).fetchall()
        assert len(reps) == 1
        rep_a = reps[0]["id"]
        database.close()

        database = _reopen(path)
        run2, plan_b = _create_seeded_run(database.conn, now=NOW)
        assert execute_run(database.conn, run2) == "SUCCEEDED"
        attempts = database.conn.execute(
            "SELECT f.cache_representation_id, f.was_304 FROM fetch_attempts f"
            " JOIN scrape_requests r ON r.id = f.request_id"
            " WHERE r.run_id = ? AND f.was_304 = 1",
            (run2,),
        ).fetchall()
        assert len(attempts) == 1
        assert attempts[0]["was_304"] == 1
        assert attempts[0]["cache_representation_id"] == rep_a
        cov_b = _coverage_for_plan(database.conn, plan_b)
        assert cov_b["completion_state"] == "COMPLETE"
        assert cov_b["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
        assert _seen(database.conn, cov_b["id"]) == {"A", "B"}
        contributors = database.conn.execute(
            "SELECT r.run_source_plan_id FROM coverage_contributing_request c"
            " JOIN scrape_requests r ON r.id = c.request_id"
            " WHERE c.coverage_id = ?",
            (cov_b["id"],),
        ).fetchall()
        assert contributors
        assert {row[0] for row in contributors} == {plan_b}
        presences = database.conn.execute(
            "SELECT source_job_id, presence_state, last_absence_coverage_id FROM job_sources"
            " ORDER BY source_job_id"
        ).fetchall()
        assert [(p["source_job_id"], p["presence_state"]) for p in presences] == [
            ("A", "ACTIVE"),
            ("B", "ACTIVE"),
        ]
        assert all(p["last_absence_coverage_id"] is None for p in presences)
        reps_after = database.conn.execute(
            "SELECT id FROM cache_representation WHERE validated_page_class = 'VALID_LIST'"
        ).fetchall()
        assert [row["id"] for row in reps_after] == [rep_a]
    finally:
        database.close()


# ---------------------------------------------------------------- A3.4


def test_accepted_304_does_not_increment_content_revision(tmp_path, server, server_state):
    port = server.server_address[1]
    path = tmp_path / "a3-rev.db"
    database = _open_db(path, port, validators=True)
    try:
        run1, plan_a = _create_seeded_run(database.conn, now=NOW)
        assert execute_run(database.conn, run1) == "SUCCEEDED"
        before_jobs = {
            row["id"] for row in database.conn.execute("SELECT id FROM jobs").fetchall()
        }
        assert len(before_jobs) == 2
        before_presence = {
            row["source_job_id"]: (row["id"], row["content_revision"])
            for row in database.conn.execute(
                "SELECT id, source_job_id, content_revision FROM job_sources"
            ).fetchall()
        }
        assert set(before_presence) == {"A", "B"}
        database.close()

        database = _reopen(path)
        run2, plan_b = _create_seeded_run(database.conn, now=NOW)
        assert execute_run(database.conn, run2) == "SUCCEEDED"
        after_jobs = {
            row["id"] for row in database.conn.execute("SELECT id FROM jobs").fetchall()
        }
        assert after_jobs == before_jobs
        after_presence = {
            row["source_job_id"]: (row["id"], row["content_revision"])
            for row in database.conn.execute(
                "SELECT id, source_job_id, content_revision FROM job_sources"
            ).fetchall()
        }
        assert set(after_presence) == {"A", "B"}
        for job_id in ("A", "B"):
            assert after_presence[job_id][0] == before_presence[job_id][0]
            assert after_presence[job_id][1] == before_presence[job_id][1]
        assert database.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert (
            database.conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 2
        )
    finally:
        database.close()


# ---------------------------------------------------------------- A3.5


def test_fresh_run_without_validator_discovers_new_first_page_job(
    tmp_path, server, server_state
):
    port = server.server_address[1]
    path = tmp_path / "a3-new.db"
    database = _open_db(path, port, validators=False)
    try:
        run1, plan_a = _create_seeded_run(database.conn, now=NOW)
        assert execute_run(database.conn, run1) == "SUCCEEDED"
        jobs_before = {
            row["id"] for row in database.conn.execute("SELECT id FROM jobs").fetchall()
        }
        assert len(jobs_before) == 2
        identity_before = {
            row["source_job_id"]: row["job_id"]
            for row in database.conn.execute(
                "SELECT source_job_id, job_id FROM job_sources"
                " WHERE source_job_id IN ('A', 'B')"
            ).fetchall()
        }
        assert set(identity_before) == {"A", "B"}
        cursor_a = _cursor_for_plan(database.conn, plan_a)
        assert cursor_a is not None
        snapshot_a = (cursor_a["state_json"], cursor_a["guard_state_json"])
        server_state.jobs_page1 = [_job("NEW", "Fresh Role"), _job("A", "Role A"), _job("B", "Role B")]
        database.close()

        database = _reopen(path)
        run2, plan_b = _create_seeded_run(database.conn, now=NOW)
        base = len(server_state.log)
        assert execute_run(database.conn, run2) == "SUCCEEDED"
        run2_hits = server_state.log[base:]
        assert run2_hits and run2_hits[0]["path"] == "/jobs?page=1"
        assert run2_hits[0]["inm"] is None
        assert run2_hits[0]["ims"] is None
        jobs_after = {
            row["id"] for row in database.conn.execute("SELECT id FROM jobs").fetchall()
        }
        assert len(jobs_after) == 3
        assert jobs_before < jobs_after
        identity_after = {
            row["source_job_id"]: row["job_id"]
            for row in database.conn.execute(
                "SELECT source_job_id, job_id FROM job_sources"
                " WHERE source_job_id IN ('A', 'B')"
            ).fetchall()
        }
        assert identity_after == identity_before
        new_presence = database.conn.execute(
            "SELECT COUNT(*) FROM job_sources WHERE source_job_id = 'NEW'"
        ).fetchone()[0]
        assert new_presence == 1
        after_a = _cursor_for_plan(database.conn, plan_a)
        assert (after_a["state_json"], after_a["guard_state_json"]) == snapshot_a
        cursor_b = _cursor_for_plan(database.conn, plan_b)
        assert cursor_b is not None
        assert cursor_b["checkpoint_run_source_plan_id"] == plan_b
    finally:
        database.close()


# ---------------------------------------------------------------- A3.6


def test_incompatible_304_never_authorizes_empty_or_absence(tmp_path, server, server_state):
    port = server.server_address[1]
    path = tmp_path / "a3-bad304.db"
    database = _open_db(path, port, validators=False)
    try:
        run1, plan_a = _create_seeded_run(database.conn, now=NOW)
        assert execute_run(database.conn, run1) == "SUCCEEDED"
        reps = database.conn.execute("SELECT id FROM cache_representation").fetchall()
        assert reps, "Run 1 stored no representation to invalidate"
        for rep in reps:
            assert prune_representation(database.conn, rep["id"], now=NOW) is True
        database.conn.commit()
        server_state.lie_304_once = True
        database.close()

        database = _reopen(path)
        run2, plan_b = _create_seeded_run(database.conn, now=NOW)
        base = len(server_state.log)
        assert execute_run(database.conn, run2) == "SUCCEEDED"
        run2_hits = server_state.log[base:]
        seed_hits = [hit for hit in run2_hits if hit["path"] == "/jobs?page=1"]
        assert [hit["status"] for hit in seed_hits] == [304, 200]
        assert seed_hits[0]["inm"] is None
        refetch = database.conn.execute(
            "SELECT id FROM acquisition_evidence WHERE kind = 'REVIEW'"
            " AND ref = 'cache://304_REFETCH_REQUIRED'"
        ).fetchall()
        assert len(refetch) == 1
        seed_request = database.conn.execute(
            "SELECT id FROM scrape_requests WHERE run_id = ? AND request_type = 'LIST_FETCH'"
            " AND parent_request_id IS NULL ORDER BY created_at, id",
            (run2,),
        ).fetchall()
        replacements = database.conn.execute(
            "SELECT id, parent_request_id, payload_json, status FROM scrape_requests"
            " WHERE run_id = ? AND parent_request_id IS NOT NULL"
            " AND payload_json LIKE '%_host_revalidation%'",
            (run2,),
        ).fetchall()
        assert len(replacements) == 1
        assert replacements[0]["parent_request_id"] == seed_request[0]["id"]
        assert (
            json.loads(replacements[0]["payload_json"]).get("_host_revalidation")
            == "UNCONDITIONAL"
        )
        assert replacements[0]["status"] == "SUCCEEDED"
        seed_attempts = database.conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts WHERE request_id = ?",
            (seed_request[0]["id"],),
        ).fetchone()[0]
        assert seed_attempts == 1
        attempts_304 = database.conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts f JOIN scrape_requests r ON r.id = f.request_id"
            " WHERE r.run_id = ? AND f.was_304 = 1",
            (run2,),
        ).fetchone()[0]
        assert attempts_304 == 1
        cov_b = _coverage_for_plan(database.conn, plan_b)
        assert cov_b["completion_state"] == "COMPLETE"
        assert cov_b["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
        assert _seen(database.conn, cov_b["id"]) == {"A", "B"}
        presences = database.conn.execute(
            "SELECT source_job_id, presence_state, last_absence_coverage_id FROM job_sources"
        ).fetchall()
        assert {p["source_job_id"] for p in presences} == {"A", "B"}
        assert all(p["presence_state"] == "ACTIVE" for p in presences)
        assert all(p["last_absence_coverage_id"] is None for p in presences)
        # The bare incompatible 304 must never have aged presence: Plan B's
        # coverage applied zero presence applications for A/B. Only A/B
        # presences exist in this DB, so the coverage-wide count is equally
        # zero; both assertions prove the 304 did not authorize absence
        # before the unconditional replacement 200 repaired the request.
        ab_applied = database.conn.execute(
            "SELECT COUNT(*) FROM coverage_presence_application a"
            " JOIN job_sources js ON js.id = a.job_source_id"
            " WHERE a.coverage_id = ? AND js.source_job_id IN ('A', 'B')",
            (cov_b["id"],),
        ).fetchone()[0]
        assert ab_applied == 0
        total_applied = database.conn.execute(
            "SELECT COUNT(*) FROM coverage_presence_application WHERE coverage_id = ?",
            (cov_b["id"],),
        ).fetchone()[0]
        assert total_applied == 0
    finally:
        database.close()
