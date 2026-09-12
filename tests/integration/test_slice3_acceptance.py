"""S3.13 coherent Slice-3 runtime acceptance vertical.

A deterministic loopback multi-page feed is interrupted immediately after
page 1 has committed through the normal service-owned fence.  The test then
opens a fresh service epoch, runs S3.12 startup recovery, and resumes the
same durable run from its cursor checkpoint.

The scenario intentionally uses the real driver, adapter, HTTP dispatch,
claim/capacity/envelope/fence, coverage, presence, obligation, group and run
owners.  The only injected behavior is the process-death exception at the
ordinary claim seam.
"""

from __future__ import annotations

import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.driver import execute_run
from jobscraper.profiles.core import create_profile
from jobscraper.runtime.clock import begin_service_epoch
from jobscraper.runtime.recovery import recover_startup_state
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-12T12:00:00.000000Z"
RESTART = "2026-09-12T12:01:00.000000Z"
REPLAY = "2026-09-12T12:02:00.000000Z"


class _IntentionalCrash(RuntimeError):
    pass


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    request_log: list[str] = []

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/jobs":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        type(self).request_log.append(f"/jobs?page={page}")

        jobs_by_page = {
            1: [
                {
                    "id": "s313-1",
                    "title": "Backend Engineer",
                    "company": "Slice Three Fixture",
                    "description": "<p>Python</p>",
                    "url": "https://jobs.example.test/jobs/s313-1",
                    "apply_url": "https://jobs.example.test/jobs/s313-1/apply",
                    "locations": ["Remote"],
                    "created_at": NOW,
                },
                {
                    "id": "s313-2",
                    "title": "Platform Engineer",
                    "company": "Slice Three Fixture",
                    "description": "<p>Go</p>",
                    "url": "https://jobs.example.test/jobs/s313-2",
                    "apply_url": "https://jobs.example.test/jobs/s313-2/apply",
                    "locations": ["Berlin"],
                    "created_at": NOW,
                },
            ],
            2: [
                {
                    "id": "s313-3",
                    "title": "Data Engineer",
                    "company": "Slice Three Fixture",
                    "description": "<p>SQL</p>",
                    "url": "https://jobs.example.test/jobs/s313-3",
                    "apply_url": "https://jobs.example.test/jobs/s313-3/apply",
                    "locations": ["Remote"],
                    "created_at": NOW,
                }
            ],
            3: [],
        }
        body = json.dumps({"jobs": jobs_by_page.get(page, [])}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def feed_server():
    _FeedHandler.request_log = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _install_authority(conn, port: int) -> None:
    config = json.dumps(
        {
            "url_template": f"http://127.0.0.1:{port}/jobs?page={{page}}",
            "items_path": "jobs",
            "fields": {
                "source_job_id": {"path": "id", "required": True},
                "title": {"path": "title", "required": True},
                "company": {"path": "company"},
                "description": {"path": "description"},
                "job_url": {"path": "url"},
                "apply_url": {"path": "apply_url"},
                "locations": {"path": "locations", "many": True},
                "posted_at": {"path": "created_at"},
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    conn.execute(
        """
        INSERT INTO sources
          (id,display_name,source_family,entry_url,created_at,updated_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            "s313-src",
            "S3.13 Fixture",
            "PUBLIC_FEED",
            f"http://127.0.0.1:{port}/jobs",
            NOW,
            NOW,
        ),
    )
    conn.execute(
        """
        INSERT INTO adapter_definitions
          (adapter_id,adapter_version,adapter_api_version,manifest_json,created_at)
        VALUES (?,?,?,?,?)
        """,
        ("json_api_feed", "1.0.0", "1", "{}", NOW),
    )
    conn.execute(
        """
        INSERT INTO adapter_permission_profiles(id,display_name,created_at)
        VALUES (?,?,?)
        """,
        ("s313-perm", "fixture", NOW),
    )
    conn.execute(
        """
        INSERT INTO adapter_permission_profile_revisions
          (id,permission_profile_id,revision,policy_json,created_at)
        VALUES (?,?,?,?,?)
        """,
        ("s313-permrev", "s313-perm", 1, "{}", NOW),
    )
    conn.execute(
        """
        INSERT INTO source_adapter_bindings
          (id,source_id,display_name,created_at)
        VALUES (?,?,?,?)
        """,
        ("s313-bnd", "s313-src", "feed", NOW),
    )
    conn.execute(
        """
        INSERT INTO source_adapter_binding_revisions
          (id,binding_id,revision,adapter_id,adapter_version,strategy,
           execution_class,permission_profile_id,permission_profile_revision,
           config_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "s313-bndrev",
            "s313-bnd",
            1,
            "json_api_feed",
            "1.0.0",
            "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            "HTTP",
            "s313-perm",
            1,
            config,
            NOW,
        ),
    )
    conn.commit()


def _start_run(conn, port: int) -> tuple[str, str]:
    profile_id, _ = create_profile(
        conn,
        snapshot={
            "name": "S3.13 acceptance",
            "keywords": ["engineer"],
            "eligible_countries": ["DE"],
            "remote_rules": {"remote_ok": True},
            "min_score_inbox": 0,
        },
        now=NOW,
    )
    run_id, plan_ids = create_run(
        conn,
        profile_id=profile_id,
        plans=[
            {
                "source_id": "s313-src",
                "source_plan_group_id": "s313-group",
                "fallback_rank": 0,
                "binding_id": "s313-bnd",
                "binding_revision_id": "s313-bndrev",
                "adapter_id": "json_api_feed",
                "adapter_version": "1.0.0",
                "adapter_api_version": "1",
                "strategy": "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
                "execution_class": "HTTP",
                "permission_profile_id": "s313-perm",
                "permission_profile_revision": 1,
                "cursor_schema_version": 1,
            }
        ],
        now=NOW,
    )
    plan_id = plan_ids[0]
    enqueue_request(
        conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="s313-src",
        binding_id="s313-bnd",
        request_type="LIST_FETCH",
        target_identity=f"http://127.0.0.1:{port}/jobs?page=1",
        logical_key='{"page":1}',
        now=NOW,
    )
    return run_id, plan_id


def _cursor_rows(conn):
    return conn.execute(
        """
        SELECT state_json, checkpoint_run_source_plan_id, cursor_schema_version
          FROM crawl_cursors
         WHERE binding_revision_id='s313-bndrev'
           AND adapter_id='json_api_feed'
           AND adapter_version='1.0.0'
         ORDER BY id
        """
    ).fetchall()


def _cursor_row(conn):
    rows = _cursor_rows(conn)
    assert len(rows) == 1, "compatible run pins must share exactly one cursor row"
    return rows[0]


def _source_counts(conn, run_id: str) -> dict[str, int]:
    return {
        "requests": conn.execute(
            "SELECT COUNT(*) FROM scrape_requests "
            "WHERE run_id=? AND request_type='LIST_FETCH'",
            (run_id,),
        ).fetchone()[0],
        "fetches": conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts f "
            "JOIN scrape_requests r ON r.id=f.request_id "
            "WHERE r.run_id=? AND r.request_type='LIST_FETCH'",
            (run_id,),
        ).fetchone()[0],
        "parses": conn.execute(
            "SELECT COUNT(*) FROM parse_attempts p "
            "JOIN scrape_requests r ON r.id=p.request_id "
            "WHERE r.run_id=? AND r.request_type='LIST_FETCH'",
            (run_id,),
        ).fetchone()[0],
        "observations": conn.execute(
            "SELECT COUNT(*) FROM job_observations WHERE run_id=?",
            (run_id,),
        ).fetchone()[0],
        "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "job_sources": conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0],
    }


def test_slice3_restart_resume_vertical_is_deterministic(
    tmp_path, feed_server, monkeypatch
):
    db = Database(tmp_path / "slice3-acceptance.db")
    try:
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
        port = feed_server.server_address[1]
        _install_authority(db.conn, port)
        run_id, plan_id = _start_run(db.conn, port)
        begin_service_epoch(db.conn, now=NOW)

        # Crash only after page 1 has committed through the ordinary fence.
        # The second source request must remain durable and unclaimed.
        import jobscraper.pipeline.driver as driver

        real_claim = driver.claim_next_request

        def crash_after_first_source_commit(*args, **kwargs):
            completed = db.conn.execute(
                "SELECT COUNT(*) FROM scrape_requests "
                "WHERE run_id=? AND request_type='LIST_FETCH' "
                "AND status='SUCCEEDED'",
                (run_id,),
            ).fetchone()[0]
            if completed >= 1:
                raise _IntentionalCrash(
                    "simulated process death after page-1 commit"
                )
            return real_claim(*args, **kwargs)

        monkeypatch.setattr(
            driver, "claim_next_request", crash_after_first_source_commit
        )
        with pytest.raises(_IntentionalCrash, match="page-1 commit"):
            execute_run(db.conn, run_id)

        assert _FeedHandler.request_log == ["/jobs?page=1"]
        pending = db.conn.execute(
            "SELECT status FROM scrape_requests "
            "WHERE run_id=? AND request_type='LIST_FETCH' "
            "ORDER BY created_at,id",
            (run_id,),
        ).fetchall()
        assert [row["status"] for row in pending] == ["SUCCEEDED", "PENDING"]

        # The continuation is durably checkpointed by the real cursor owner.
        cursor = _cursor_row(db.conn)
        assert cursor is not None
        assert json.loads(cursor["state_json"]) == {"page": 2}
        assert cursor["checkpoint_run_source_plan_id"] == plan_id
        assert cursor["cursor_schema_version"] == 1

        monkeypatch.setattr(driver, "claim_next_request", real_claim)
        begin_service_epoch(db.conn, now=RESTART)
        recovery_report = recover_startup_state(
            db.conn, now=RESTART, worker_id="s313-startup"
        )
        assert isinstance(recovery_report, dict)

        # Startup recovery may validate/reconcile the plan, but it must not
        # mutate the accepted continuation or replay network work.
        cursor_after_recovery = _cursor_row(db.conn)
        assert cursor_after_recovery is not None
        assert json.loads(cursor_after_recovery["state_json"]) == {"page": 2}
        assert _FeedHandler.request_log == ["/jobs?page=1"]

        assert execute_run(db.conn, run_id) == "SUCCEEDED"

        # Resume starts at page 2. Page 3 is the recognized terminal empty
        # page. Page 1 is never replayed.
        assert _FeedHandler.request_log == [
            "/jobs?page=1",
            "/jobs?page=2",
            "/jobs?page=3",
        ]

        requests = db.conn.execute(
            "SELECT id,request_unique_key,status FROM scrape_requests "
            "WHERE run_id=? AND request_type='LIST_FETCH' "
            "ORDER BY created_at,id",
            (run_id,),
        ).fetchall()
        assert len(requests) == 3
        assert len({row["request_unique_key"] for row in requests}) == 3
        assert {row["status"] for row in requests} == {"SUCCEEDED"}

        attempt_counts = db.conn.execute(
            "SELECT r.id,COUNT(a.attempt_id) AS attempts "
            "FROM scrape_requests r "
            "LEFT JOIN request_attempts a ON a.request_id=r.id "
            "WHERE r.run_id=? AND r.request_type='LIST_FETCH' "
            "GROUP BY r.id ORDER BY r.created_at,r.id",
            (run_id,),
        ).fetchall()
        assert [row["attempts"] for row in attempt_counts] == [1, 1, 1]

        # Page 2 advances the durable cursor to page 3. Terminal page 3 does
        # not fabricate a later cursor.
        final_cursor = _cursor_row(db.conn)
        assert final_cursor is not None
        assert json.loads(final_cursor["state_json"]) == {"page": 3}
        assert final_cursor["checkpoint_run_source_plan_id"] == plan_id

        coverage = db.conn.execute(
            "SELECT completion_state,coverage_authority,"
            " terminal_enumeration_proven "
            "FROM enumeration_coverage WHERE run_source_plan_id=?",
            (plan_id,),
        ).fetchone()
        assert coverage is not None
        assert coverage["completion_state"] == "COMPLETE"
        assert coverage["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
        assert coverage["terminal_enumeration_proven"] == 1

        assert [
            row["title"]
            for row in db.conn.execute(
                "SELECT title FROM jobs ORDER BY title"
            ).fetchall()
        ] == [
            "Backend Engineer",
            "Data Engineer",
            "Platform Engineer",
        ]
        availability = db.conn.execute(
            "SELECT presence_state,availability_evidence_kind,"
            " availability_effective_at,availability_received_at,"
            " availability_revision FROM job_sources ORDER BY id"
        ).fetchall()
        assert len(availability) == 3
        assert {row["presence_state"] for row in availability} == {"ACTIVE"}
        assert {row["availability_evidence_kind"] for row in availability} == {
            "ACTIVE_OBSERVATION"
        }
        assert all(row["availability_effective_at"] for row in availability)
        assert all(row["availability_received_at"] for row in availability)
        assert all(row["availability_revision"] >= 1 for row in availability)

        canonical = db.conn.execute(
            "SELECT listing_status,evaluation_revision FROM jobs ORDER BY id"
        ).fetchall()
        assert len(canonical) == 3
        assert {row["listing_status"] for row in canonical} == {"ACTIVE"}
        assert all(row["evaluation_revision"] >= 1 for row in canonical)

        # Host-native obligations finish before group/run terminal truth.
        assert (
            db.conn.execute("SELECT COUNT(*) FROM job_eligibility").fetchone()[0]
            == 3
        )
        assert db.conn.execute("SELECT COUNT(*) FROM job_scores").fetchone()[0] == 3
        obligation_rows = db.conn.execute(
            "SELECT request_type,status,COUNT(*) AS n FROM scrape_requests "
            "WHERE run_id=? AND request_type IN ('RECONCILE','ELIGIBILITY','SCORE') "
            "GROUP BY request_type,status ORDER BY request_type,status",
            (run_id,),
        ).fetchall()
        assert {(row["request_type"], row["status"], row["n"]) for row in obligation_rows} == {
            ("RECONCILE", "SUCCEEDED", 3),
            ("ELIGIBILITY", "SUCCEEDED", 3),
            ("SCORE", "SUCCEEDED", 3),
        }
        assert db.conn.execute(
            "SELECT COUNT(*) FROM scrape_requests "
            "WHERE run_id=? AND status IN ('PENDING','RUNNING','RETRY_WAIT')",
            (run_id,),
        ).fetchone()[0] == 0

        group = db.conn.execute(
            "SELECT group_outcome FROM source_plan_group_state "
            "WHERE run_id=? AND source_plan_group_id='s313-group'",
            (run_id,),
        ).fetchone()
        assert group is not None
        assert group["group_outcome"] == "SATISFIED"
        assert (
            db.conn.execute(
                "SELECT status FROM scrape_runs WHERE id=?", (run_id,)
            ).fetchone()["status"]
            == "SUCCEEDED"
        )

        assert _source_counts(db.conn, run_id) == {
            "requests": 3,
            "fetches": 3,
            "parses": 3,
            "observations": 3,
            "jobs": 3,
            "job_sources": 3,
        }

        # Prove a second real startup pass is replay-safe by opening another
        # service epoch before rerunning recovery over the terminal run.
        before = _source_counts(db.conn, run_id)
        begin_service_epoch(db.conn, now=REPLAY)
        replay_report = recover_startup_state(
            db.conn, now=REPLAY, worker_id="s313-startup-replay"
        )
        assert isinstance(replay_report, dict)
        assert _source_counts(db.conn, run_id) == before
        assert json.loads(_cursor_row(db.conn)["state_json"]) == {"page": 3}
        assert _FeedHandler.request_log == [
            "/jobs?page=1",
            "/jobs?page=2",
            "/jobs?page=3",
        ]
    finally:
        db.close()
