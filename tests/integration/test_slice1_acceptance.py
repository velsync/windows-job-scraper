"""Slice 1 automated acceptance (S1.11).

End-to-end against the REAL launcher + service subprocesses on an
isolated data root, driven through the public HTTP surface.  The only
non-HTTP touches are (a) seeding source/binding rows the way a local
operator does (Slice 1 has no source-management API), (b) reading
persisted evidence directly from the database, and (c) the durable
mid-run cancellation write, which uses the exact runtime primitive the
cancel route applies (runs execute synchronously in the service, so a
second HTTP request cannot be served while a run holds the loop).

Vertical path (plan S1.11):

    launch → ticket bootstrap → session + CSRF → create profile
    → run against a fixture feed on loopback → observations ingested
    → canonical jobs + provenance → eligibility/scores → inbox events
    → shortlist one job, dismiss another → application create/update
    → restart the service → all state preserved (PROD-08)
    → a second run resumes from the durable cursor and is idempotent
      (no duplicate jobs / observations / inbox events)

Cancellation + crash recovery (plan S1.11; 03 §18, RUN-07, RUN-09):

    durable cancellation mid-run through the live service → the fence
    refuses the late acquisition commit, the driver cooperatively
    abandons, the run aggregates CANCELLED, nothing is committed after
    cancellation;

    hard service kill mid-run → the launcher restarts the service →
    restart recovery reclaims the orphaned claim → cancelling the
    interrupted run finalizes it → a new run resumes collection → no
    duplicate observations.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jobscraper.config import AppConfig
from jobscraper.db.connection import connect_db
from jobscraper.launcher.runtime_descriptor import load_runtime_descriptor
from jobscraper.paths import build_app_paths, ensure_app_directories
from jobscraper.procutils import (
    child_process_env,
    child_python_executable,
    graceful_process_group_kwargs,
    request_graceful_stop,
)
from jobscraper.runtime.cancellation import request_run_cancellation

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NOW = "2026-09-08T09:00:00.000000Z"

JOBS_PAGE_1 = [
    {
        "id": "fx-1",
        "title": "Backend Engineer",
        "company": "Fixture Corp",
        "description": "<p>Python services</p>",
        "url": "https://jobs.example.test/jobs/fx-1",
        "apply_url": "https://jobs.example.test/jobs/fx-1/apply",
        "locations": ["Berlin"],
        "created_at": NOW,
    },
    {
        "id": "fx-2",
        "title": "Platform Engineer",
        "company": "Fixture Corp",
        "description": "<p>Go infrastructure</p>",
        "url": "https://jobs.example.test/jobs/fx-2",
        "apply_url": "https://jobs.example.test/jobs/fx-2/apply",
        "locations": ["Remote"],
        "created_at": NOW,
    },
]

# fixture-feed behavior flags (per-test-process, reset by the fixture)
_FEED_STATE: dict = {}


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path
        # ---- slow feeds used by the cancellation / crash scenarios ----
        if path.startswith("/always-slow"):
            if "page=1" in path:
                time.sleep(_FEED_STATE["slow_s"])
                self._send({"jobs": JOBS_PAGE_1})
            else:
                self._send({"jobs": []})
            return
        if path.startswith("/once-slow"):
            if "page=1" in path and not _FEED_STATE["once_slow_used"]:
                _FEED_STATE["once_slow_used"] = True
                time.sleep(_FEED_STATE["slow_s"])
                self._send({"jobs": JOBS_PAGE_1})
            elif "page=1" in path:
                self._send({"jobs": JOBS_PAGE_1})
            else:
                self._send({"jobs": []})
            return
        # ---- standard fixture feed ----
        if path.startswith("/jobs?page=1") or path == "/jobs":
            self._send({"jobs": JOBS_PAGE_1})
        elif path.startswith("/jobs?page=2") or path.startswith("/empty"):
            self._send({"jobs": []})
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


def _feed_config(port: int, path: str = "/jobs") -> str:
    return json.dumps(
        {
            "url_template": f"http://127.0.0.1:{port}{path}?page={{page}}",
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
        }
    )


@pytest.fixture()
def acceptance(tmp_path):
    """Real launcher + service on an isolated root with a seeded feed."""
    _FEED_STATE.update({"slow_s": 8.0, "once_slow_used": False})
    root = tmp_path / "acceptance-root"
    ensure_app_directories(build_app_paths(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state = {
        "root": root,
        "config": AppConfig(data_root=root),
        "feed_port": srv.server_address[1],
        "launcher": None,
        "db": None,
    }

    def _launch() -> str:
        env = child_process_env()
        env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("WJS_DATA_ROOT", None)
        launcher = subprocess.Popen(
            [
                child_python_executable(),
                "-m",
                "jobscraper",
                "--data-root",
                str(root),
                "--print-url",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **graceful_process_group_kwargs(),
        )
        state["launcher"] = launcher
        return _next_dashboard_url(launcher, timeout=90)

    def _wait_db(timeout: float = 30.0) -> None:
        db_path = state["config"].paths.database_file
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                conn = connect_db(db_path)
                row = conn.execute(
                    "SELECT COUNT(*) FROM sources"
                ).fetchone()
                if row is not None:
                    state["db"] = conn
                    return
            except Exception as exc:  # DB not created/migrated yet
                last_error = exc
            time.sleep(0.2)
        raise AssertionError(f"service database never became ready: {last_error}")

    def _seed() -> None:
        port = state["feed_port"]
        state["db"].executescript(
            f"""
            INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
            VALUES ('src-1','Fixture Feed','PUBLIC_FEED','http://127.0.0.1:{port}/jobs','{NOW}','{NOW}');
            INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
            VALUES ('json_api_feed','1.0.0','1','{{}}','{NOW}');
            INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
            INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
            VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
            INSERT INTO source_adapter_bindings (id, source_id, display_name, current_revision_id, created_at)
            VALUES ('bnd-1','src-1','api','bndrev-1','{NOW}');
            INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
                strategy, execution_class, permission_profile_id, permission_profile_revision, config_json, created_at)
            VALUES ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,
                '{_feed_config(port)}','{NOW}');
            """
        )
        state["db"].commit()

    try:
        state["url"] = _launch()
        _wait_db()
        _seed()
        yield state
    finally:
        if state["db"] is not None:
            state["db"].close()
        launcher = state["launcher"]
        if launcher is not None and launcher.poll() is None:
            request_graceful_stop(launcher)
            try:
                launcher.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                launcher.kill()
                launcher.wait()
        srv.shutdown()
        srv.server_close()


def _next_dashboard_url(launcher, *, timeout: float) -> str:
    """Read the next `Dashboard: <url>` line from the launcher output."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = launcher.stdout.readline()
        if not line:
            break
        if line.startswith("Dashboard:"):
            return line.strip().split("Dashboard: ", 1)[1]
    raise AssertionError("launcher never produced a dashboard URL")


def _request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except urllib.error.URLError:
        raise


def _bootstrap(state) -> dict:
    """Exchange the one-time ticket for a session + CSRF token."""
    url = state["url"]
    base, ticket = url.split("#bootstrap=", 1)
    host_port = base.split("//", 1)[1].rstrip("/")
    status, headers, body = _request(
        "POST",
        f"http://{host_port}/api/bootstrap",
        headers={
            "Host": host_port,
            "Origin": f"http://{host_port}",
            "Content-Type": "application/json",
        },
        body=json.dumps({"ticket": ticket}).encode(),
    )
    assert status == 200, body
    csrf_token = json.loads(body)["csrf_token"]
    set_cookies = ", ".join(headers.get_all("Set-Cookie", []))
    session_id = None
    for part in set_cookies.split(","):
        if part.strip().startswith("wjs_session="):
            session_id = part.strip().split("=", 1)[1].split(";", 1)[0]
    assert session_id, set_cookies
    return {"host_port": host_port, "session": session_id, "csrf": csrf_token}


def _api(state, auth, method: str, path: str, *, json_body=None):
    headers = {
        "Host": auth["host_port"],
        "Origin": f"http://{auth['host_port']}",
        "Cookie": f"wjs_session={auth['session']}; wjs_csrf={auth['csrf']}",
        "X-CSRF-Token": auth["csrf"],
    }
    body = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(json_body).encode()
    status, _, out = _request(
        method, f"http://{auth['host_port']}{path}", headers=headers, body=body
    )
    return status, (json.loads(out) if out else None)


def _count(state, sql: str, params: tuple = ()) -> int:
    return state["db"].execute(sql, params).fetchone()[0]


def _repoint_binding(state, path: str) -> None:
    """Operator action: point the binding revision at another feed path."""
    state["db"].execute(
        "UPDATE source_adapter_binding_revisions SET config_json = ? WHERE id = 'bndrev-1'",
        (_feed_config(state["feed_port"], path),),
    )
    state["db"].commit()


def _wait_for(state, sql: str, params: tuple = (), *, timeout: float = 60.0):
    """Poll the database until a row exists; return it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = state["db"].execute(sql, params).fetchone()
        if row is not None:
            return row
        time.sleep(0.1)
    raise AssertionError(f"condition never became true: {sql}")


class TestVerticalPath:
    def test_full_vertical_path_restart_persistence_and_idempotent_resume(
        self, acceptance
    ):
        state = acceptance
        auth = _bootstrap(state)

        # ---- create the profile through the API
        status, profile = _api(
            state,
            auth,
            "POST",
            "/api/profiles",
            json_body={
                "name": "Acceptance",
                "keywords": ["engineer"],
                "eligible_countries": ["DE"],
                "remote_rules": {"remote_ok": True},
                "min_score_inbox": 0,
            },
        )
        assert status == 200, profile
        profile_id = profile["id"]

        # ---- run against the fixture feed (vertical collection path)
        status, run = _api(
            state, auth, "POST", "/api/runs", json_body={"profile_id": profile_id}
        )
        assert status == 200, run
        assert run["status"] == "SUCCEEDED", run
        assert run["jobs_saved"] == 2, run
        run_id = run["run_id"]

        # ---- inbox surfaced the two eligible jobs with scores
        status, inbox = _api(
            state, auth, "GET", f"/api/inbox?profile_id={profile_id}"
        )
        assert status == 200
        items = inbox["inbox"]
        assert len(items) == 2
        by_title = {i["title"]: i for i in items}
        assert {i["event_kind"] for i in items} == {"NEW_ELIGIBLE_APPEARANCE"}
        # deterministic evidence-derived verdicts: Berlin is a proven DE
        # match (ELIGIBLE); "Remote" alone proves no country, so the job
        # surfaces with honest uncertainty (LIKELY, never ELIGIBLE).
        assert by_title["Backend Engineer"]["eligibility"] == "ELIGIBLE"
        assert by_title["Platform Engineer"]["eligibility"] == "LIKELY"
        assert all(isinstance(i["score"], (int, float)) and i["score"] >= 0 for i in items)
        assert all(isinstance(i["breakdown"], list) and i["breakdown"] for i in items)
        job_backend = by_title["Backend Engineer"]["job_id"]
        job_platform = by_title["Platform Engineer"]["job_id"]

        # ---- provenance: source links stay inspectable and safe (PROD-05)
        status, detail = _api(state, auth, "GET", f"/api/jobs/{job_backend}")
        assert status == 200
        assert detail["job"]["title"] == "Backend Engineer"
        assert detail["job"]["listing_status"] == "ACTIVE"
        assert len(detail["sources"]) == 1
        presence = detail["sources"][0]
        assert presence["source_job_id"] == "fx-1"
        # provenance: where it was found (the feed page) stays distinct
        # from the job's own canonical URL and the apply URL
        assert presence["discovery_url"] == (
            f"http://127.0.0.1:{state['feed_port']}/jobs?page=1"
        )
        assert presence["canonical_job_url"] == "https://jobs.example.test/jobs/fx-1"
        assert presence["application_url"] == "https://jobs.example.test/jobs/fx-1/apply"
        assert detail["apply_url"] == "https://jobs.example.test/jobs/fx-1/apply"

        # ---- dispositions: shortlist one, dismiss the other (sticky)
        status, out = _api(
            state,
            auth,
            "POST",
            f"/api/profiles/{profile_id}/jobs/{job_backend}/disposition",
            json_body={"disposition": "SHORTLISTED"},
        )
        assert status == 200 and out["row_revision"] >= 1
        assert out["disposition"] == "SHORTLISTED"
        status, out = _api(
            state,
            auth,
            "POST",
            f"/api/profiles/{profile_id}/jobs/{job_platform}/disposition",
            json_body={"disposition": "DISMISSED", "reason": "not a fit"},
        )
        assert status == 200

        # ---- application create + status update through the API
        status, application = _api(
            state,
            auth,
            "POST",
            "/api/applications",
            json_body={"job_id": job_backend, "profile_id": profile_id},
        )
        assert status == 200, application
        assert application["status"] == "PREPARING"
        application_id = application["id"]
        status, application = _api(
            state,
            auth,
            "PATCH",
            f"/api/applications/{application_id}",
            json_body={
                "status": "APPLIED",
                "applied_via_url": "https://jobs.example.test/jobs/fx-1/apply",
                "notes_md": "applied through the fixture portal",
            },
        )
        assert status == 200, application
        assert application["status"] == "APPLIED"

        # ---- persisted evidence down the whole spine
        db = state["db"]
        assert _count(state, "SELECT COUNT(*) FROM jobs") == 2
        assert _count(state, "SELECT COUNT(*) FROM job_sources") == 2
        assert _count(state, "SELECT COUNT(*) FROM job_observations") == 2
        assert _count(state, "SELECT COUNT(*) FROM fetch_attempts") == 2
        assert _count(state, "SELECT COUNT(*) FROM parse_attempts") == 2
        coverage = db.execute("SELECT * FROM enumeration_coverage").fetchone()
        assert coverage["completion_state"] == "COMPLETE"
        assert coverage["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
        assert _count(state, "SELECT COUNT(*) FROM job_eligibility") == 2
        assert _count(state, "SELECT COUNT(*) FROM job_scores") == 2
        assert (
            _count(
                state,
                "SELECT COUNT(*) FROM job_profile_inbox_events"
                " WHERE event_kind = 'NEW_ELIGIBLE_APPEARANCE'",
            )
            == 2
        )
        assert _count(state, "SELECT COUNT(*) FROM applications") == 1
        assert (
            _count(
                state,
                "SELECT COUNT(*) FROM application_events WHERE application_id = ?",
                (application_id,),
            )
            >= 2
        )

        # ---- restart the service; every piece of state must survive
        launcher = state["launcher"]
        request_graceful_stop(launcher)
        assert launcher.wait(timeout=30) == 0
        state["url"] = _next_dashboard_url(
            _relaunch(state), timeout=90
        )
        auth = _bootstrap(state)

        status, profiles = _api(state, auth, "GET", "/api/profiles")
        assert status == 200 and len(profiles["profiles"]) == 1
        assert profiles["profiles"][0]["snapshot"]["name"] == "Acceptance"

        status, runs = _api(state, auth, "GET", "/api/runs")
        assert status == 200
        persisted_run = next(r for r in runs["runs"] if r["id"] == run_id)
        assert persisted_run["status"] == "SUCCEEDED"
        assert persisted_run["jobs_saved"] == 2

        # inbox after restart: the triage feed shows the shortlisted job;
        # the dismissed one stays out (PROD-02 sticky dismissal survives)
        status, inbox = _api(state, auth, "GET", f"/api/inbox?profile_id={profile_id}")
        assert status == 200 and len(inbox["inbox"]) == 1
        assert inbox["inbox"][0]["title"] == "Backend Engineer"
        assert inbox["inbox"][0]["disposition"] == "SHORTLISTED"

        status, applications = _api(
            state, auth, "GET", f"/api/applications?profile_id={profile_id}"
        )
        assert status == 200 and len(applications["applications"]) == 1
        assert applications["applications"][0]["status"] == "APPLIED"

        status, detail = _api(state, auth, "GET", f"/api/jobs/{job_backend}")
        assert status == 200 and detail["job"]["title"] == "Backend Engineer"

        # ---- a second run resumes from the durable cursor and is
        # idempotent: page 1 is not re-fetched, nothing is duplicated
        status, run2 = _api(
            state, auth, "POST", "/api/runs", json_body={"profile_id": profile_id}
        )
        assert status == 200 and run2["status"] == "SUCCEEDED", run2
        assert run2["jobs_saved"] == 0 and run2["jobs_updated"] == 0, run2
        assert (
            _count(
                state,
                "SELECT COUNT(*) FROM scrape_requests WHERE run_id = ?",
                (run2["run_id"],),
            )
            == 1
        )
        assert _count(state, "SELECT COUNT(*) FROM jobs") == 2
        assert _count(state, "SELECT COUNT(*) FROM job_observations") == 2
        assert (
            _count(
                state,
                "SELECT COUNT(*) FROM job_profile_inbox_events"
                " WHERE event_kind = 'NEW_ELIGIBLE_APPEARANCE'",
            )
            == 2
        )
        assert (
            _count(
                state,
                "SELECT COUNT(*) FROM job_sources WHERE source_job_id = 'fx-1'",
            )
            == 1
        )


def _relaunch(state):
    env = child_process_env()
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("WJS_DATA_ROOT", None)
    launcher = subprocess.Popen(
        [
            child_python_executable(),
            "-m",
            "jobscraper",
            "--data-root",
            str(state["root"]),
            "--print-url",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **graceful_process_group_kwargs(),
    )
    state["launcher"] = launcher
    return launcher


class TestCancellationAndCrashRecovery:
    def test_durable_cancellation_mid_run_through_the_live_service(self, acceptance):
        """§18 with the real service in flight: cancellation becomes
        durable while the run is blocked in a slow fetch.  The fence must
        refuse the late acquisition commit (no observations, no fetch
        evidence), the driver cooperatively abandons, and the run
        aggregates to CANCELLED."""
        state = acceptance
        _repoint_binding(state, "/always-slow")
        auth = _bootstrap(state)
        status, profile = _api(
            state, auth, "POST", "/api/profiles",
            json_body={
                "name": "Cancel", "keywords": ["engineer"],
                "eligible_countries": ["DE"], "remote_rules": {"remote_ok": True},
                "min_score_inbox": 0,
            },
        )
        assert status == 200
        profile_id = profile["id"]

        result: dict = {}

        def _drive():
            try:
                result["response"] = _api(
                    state, auth, "POST", "/api/runs",
                    json_body={"profile_id": profile_id},
                )
            except Exception as exc:  # pragma: no cover - only on failure
                result["error"] = exc

        worker = threading.Thread(target=_drive)
        worker.start()
        try:
            # wait until the service claimed the page-1 request, then let
            # the durable cancellation land while the fetch is hanging
            row = _wait_for(
                state,
                "SELECT id, run_id FROM scrape_requests WHERE status = 'RUNNING'",
                timeout=60.0,
            )
            time.sleep(0.5)  # firmly inside the slow fetch window
            request_run_cancellation(state["db"], row["run_id"])
        finally:
            worker.join(timeout=90)

        assert "error" not in result, result.get("error")
        status, run = result["response"]
        assert status == 200, run
        assert run["status"] == "CANCELLED", run

        db = state["db"]
        run_row = db.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (run["run_id"],)
        ).fetchone()
        assert run_row["status"] == "CANCELLED"
        requests = db.execute(
            "SELECT status FROM scrape_requests WHERE run_id = ?", (run["run_id"],)
        ).fetchall()
        assert all(r["status"] == "CANCELLED" for r in requests)
        # nothing committed after cancellation: no observations, no fetch
        # evidence, no jobs, coverage cannot claim completeness
        assert _count(state, "SELECT COUNT(*) FROM job_observations") == 0
        assert _count(state, "SELECT COUNT(*) FROM fetch_attempts") == 0
        assert _count(state, "SELECT COUNT(*) FROM jobs") == 0
        coverage = db.execute("SELECT * FROM enumeration_coverage").fetchone()
        # S3.8 records an explicit CANCELLED coverage state instead of the
        # old PARTIAL blur (S3.8 corrective review #9); like PARTIAL it
        # never applies absence because absence requires COMPLETE.
        assert coverage["completion_state"] == "CANCELLED"

        # the run list shows the cancelled run through the API
        status, runs = _api(state, auth, "GET", "/api/runs")
        assert status == 200
        assert any(r["status"] == "CANCELLED" for r in runs["runs"])

    def test_crash_kill_restart_recovery_cancel_and_resume(self, acceptance):
        """Kill the service hard mid-run; the launcher restarts it;
        restart recovery reclaims the orphaned claim; the interrupted run
        cancels to its terminal state; a new run resumes collection with
        no duplicate observations."""
        state = acceptance
        _repoint_binding(state, "/once-slow")
        auth = _bootstrap(state)
        status, profile = _api(
            state, auth, "POST", "/api/profiles",
            json_body={
                "name": "Crash", "keywords": ["engineer"],
                "eligible_countries": ["DE"], "remote_rules": {"remote_ok": True},
                "min_score_inbox": 0,
            },
        )
        assert status == 200
        profile_id = profile["id"]

        result: dict = {}

        def _drive():
            try:
                result["response"] = _api(
                    state, auth, "POST", "/api/runs",
                    json_body={"profile_id": profile_id},
                )
            except Exception as exc:  # expected: the service dies mid-response
                result["error"] = exc

        worker = threading.Thread(target=_drive)
        worker.start()
        try:
            claimed = _wait_for(
                state,
                "SELECT id, run_id FROM scrape_requests WHERE status = 'RUNNING'",
                timeout=60.0,
            )
            run_id = claimed["run_id"]
            # hard-kill the service child while its fetch hangs
            desc = load_runtime_descriptor(state["config"].paths.runtime)
            assert desc is not None
            os.kill(desc.pid, 9)
        finally:
            worker.join(timeout=90)
        assert "error" in result  # the in-flight API call died with the service

        # the launcher supervises: it restarts the service and prints a
        # fresh one-time ticket
        state["url"] = _next_dashboard_url(state["launcher"], timeout=90)
        auth = _bootstrap(state)

        db = state["db"]
        # restart recovery reclaimed the orphaned claim (RUN-07/RUN-09)
        request_row = _wait_for(
            state,
            "SELECT status FROM scrape_requests WHERE id = ? AND status != 'RUNNING'",
            (claimed["id"],),
            timeout=30,
        )
        assert request_row["status"] == "RETRY_WAIT"
        attempt = db.execute(
            "SELECT outcome, abandoned_reason FROM request_attempts"
            " WHERE request_id = ? ORDER BY started_at DESC",
            (claimed["id"],),
        ).fetchone()
        assert attempt["outcome"] == "ABANDONED"
        assert attempt["abandoned_reason"] == "SERVICE_RESTART"
        recovery_event = db.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'SERVICE_RECOVERY'"
        ).fetchone()[0]
        assert recovery_event >= 1
        # S3.1 (03 §50): the restart opened a fresh service epoch and the
        # crashed lifetime's epoch is durably recorded as ended
        epochs = db.execute(
            "SELECT ended_at, end_reason FROM service_clock_epochs"
            " ORDER BY started_at, id"
        ).fetchall()
        assert len(epochs) >= 2
        assert epochs[-1]["ended_at"] is None  # the fresh epoch is open
        assert any(
            row["ended_at"] is not None and row["end_reason"] == "SERVICE_RESTART"
            for row in epochs[:-1]
        )

        # profiles survived the crash+restart (PROD-08)
        status, profiles = _api(state, auth, "GET", "/api/profiles")
        assert status == 200 and len(profiles["profiles"]) == 1

        # the interrupted run is visible and non-terminal; cancelling it
        # through the API finalizes it to CANCELLED
        status, runs = _api(state, auth, "GET", "/api/runs")
        assert status == 200
        interrupted = next(r for r in runs["runs"] if r["id"] == run_id)
        assert interrupted["status"] in ("RUNNING", "QUEUED")
        status, out = _api(state, auth, "POST", f"/api/runs/{run_id}/cancel")
        assert status == 200 and out["status"] == "CANCELLED", out
        run_row = db.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run_row["status"] == "CANCELLED"

        # resume collection with a new run: the once-slow page now answers
        # immediately; the same two jobs appear exactly once
        status, resumed = _api(
            state, auth, "POST", "/api/runs", json_body={"profile_id": profile_id}
        )
        assert status == 200 and resumed["status"] == "SUCCEEDED", resumed
        assert resumed["jobs_saved"] == 2, resumed
        assert _count(state, "SELECT COUNT(*) FROM jobs") == 2
        assert _count(state, "SELECT COUNT(*) FROM job_sources") == 2
        # the crashed run committed nothing; the resumed run committed the
        # only two observations (no duplicates from the retry)
        assert _count(state, "SELECT COUNT(*) FROM job_observations") == 2
        assert (
            _count(
                state,
                "SELECT COUNT(*) FROM job_observations WHERE run_id = ?",
                (run_id,),
            )
            == 0
        )
        assert (
            _count(
                state,
                "SELECT COUNT(*) FROM job_profile_inbox_events"
                " WHERE event_kind = 'NEW_ELIGIBLE_APPEARANCE'",
            )
            == 2
        )
