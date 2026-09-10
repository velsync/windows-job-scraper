"""S2.8 corrective acceptance for the §12.1 durable discovery boundary.

These tests intentionally exercise the production discovery primitive rather
than manufacturing an ExecutionPlanEnvelope in test code.  Authority:
02 §12.1 (Source + durable SOURCE_DISCOVERY identity before first probe),
03 RUN-02/RUN-06/RUN-08 (pinned plan, claim/lease, fenced output commit), and
04 §5.1 (host-owned destination policy).
"""

from __future__ import annotations

from contextlib import contextmanager
import http.server
import json
import threading

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
import jobscraper.runtime.discovery as discovery_runtime
from jobscraper.runtime.discovery import (
    execute_source_discovery,
    load_queued_discovery,
    queue_source_discovery,
)

NOW = "2026-09-10T06:30:00.000000Z"

_GREENHOUSE_PAGE = b"""<!doctype html><html><head>
<script src='https://boards.greenhouse.io/embed/job_board?for=acme'></script>
<link rel='canonical' href='https://boards.greenhouse.io/acme'>
</head><body><div id='grnhse_app'></div></body></html>"""
_GENERIC_PAGE = b"<!doctype html><html><body><h1>Careers</h1><p>Join us.</p></body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802 - stdlib handler interface
        type(self).hits += 1
        if self.path == "/careers/greenhouse":
            body = _GREENHOUSE_PAGE
        elif self.path == "/careers/generic":
            body = _GENERIC_PAGE
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    _Handler.hits = 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _database(path):
    db = Database(path)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    db.conn.executescript(
        f"""
        INSERT INTO adapter_permission_profiles (id, display_name, created_at)
        VALUES ('perm-discovery', 'discovery', '{NOW}');
        INSERT INTO adapter_permission_profile_revisions
            (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-discovery', 'perm-discovery', 1, '{{}}', '{NOW}');
        """
    )
    db.conn.commit()
    return db


def test_discovery_identity_is_durable_before_first_network_probe(tmp_path, server):
    db = _database(tmp_path / "discovery.db")
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/careers/greenhouse"
        queued = queue_source_discovery(
            db.conn,
            display_name="Acme careers",
            entry_url=url,
            source_family="EMPLOYER_CAREERS",
            now=NOW,
        )

        # §12.1: all authority exists *before* the first byte of network I/O.
        assert _Handler.hits == 0
        source = db.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (queued.source_id,)
        ).fetchone()
        assert source is not None and source["entry_url"] == url
        revision = db.conn.execute(
            "SELECT * FROM source_adapter_binding_revisions WHERE id = ?",
            (queued.binding_revision_id,),
        ).fetchone()
        assert revision["adapter_id"] == "generic_discovery"
        assert revision["strategy"] == "GENERIC_DISCOVERY"
        assert revision["execution_class"] == "HTTP"
        request = db.conn.execute(
            "SELECT * FROM scrape_requests WHERE id = ?", (queued.request_id,)
        ).fetchone()
        assert request["request_type"] == "SOURCE_DISCOVERY"
        assert request["status"] == "PENDING"
        assert request["run_id"] == queued.run_id
        assert request["run_source_plan_id"] == queued.run_source_plan_id
        assert db.conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM ats_fingerprints").fetchone()[0] == 0

        outcome = execute_source_discovery(db.conn, queued, worker_id="acceptance", now=NOW)
        assert _Handler.hits == 1
        assert outcome.run_status == "SUCCEEDED"
        assert outcome.page_class.value == "VALID_LIST"
        assert outcome.fingerprint is not None
        assert outcome.fingerprint.family == "GREENHOUSE"
        assert outcome.decision is not None
        assert outcome.decision.outcome.value == "SPECIALIZED"

        request = db.conn.execute(
            "SELECT * FROM scrape_requests WHERE id = ?", (queued.request_id,)
        ).fetchone()
        assert request["status"] == "SUCCEEDED"
        assert request["page_class"] == "VALID_LIST"
        attempts = db.conn.execute(
            "SELECT * FROM request_attempts WHERE request_id = ?", (queued.request_id,)
        ).fetchall()
        assert len(attempts) == 1 and attempts[0]["outcome"] == "SUCCEEDED"
        fetch = db.conn.execute(
            "SELECT * FROM fetch_attempts WHERE request_id = ?", (queued.request_id,)
        ).fetchone()
        assert fetch is not None and fetch["status_code"] == 200
        assert fetch["attempt_id"] == attempts[0]["attempt_id"]

        kinds = {
            row["kind"]
            for row in db.conn.execute(
                "SELECT kind FROM acquisition_evidence WHERE request_id = ?",
                (queued.request_id,),
            ).fetchall()
        }
        assert {"RESULT_ENVELOPE", "PAGE_VALIDITY"} <= kinds
        fingerprint = db.conn.execute(
            "SELECT * FROM ats_fingerprints WHERE id = ?", (outcome.fingerprint_id,)
        ).fetchone()
        assert fingerprint["source_id"] == queued.source_id
        assert fingerprint["family"] == "GREENHOUSE"
        route = db.conn.execute(
            "SELECT * FROM source_route_decisions WHERE id = ?",
            (outcome.route_decision_id,),
        ).fetchone()
        assert route["source_id"] == queued.source_id
        assert route["outcome"] == "SPECIALIZED"
        candidates = json.loads(route["candidates_json"])
        assert candidates[0]["adapter_id"] == "greenhouse"

        # Discovery is classification, not job enumeration: no false coverage,
        # parse attempt or canonical observation is manufactured.
        assert db.conn.execute("SELECT COUNT(*) FROM enumeration_coverage").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM parse_attempts").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0
    finally:
        db.close()


def test_queued_discovery_survives_process_restart_before_claim(tmp_path, server):
    """A real restart: the continuation is re-derived from durable rows only.

    02 §12.1 requires that "crash/restart resumes from that durable identity".
    The in-memory ``QueuedDiscovery`` value is therefore deliberately dropped
    (it cannot survive a process death) and the continuation is loaded back
    from the committed Source/plan/request rows.
    """
    path = tmp_path / "restart.db"
    db = _database(path)
    url = f"http://127.0.0.1:{server.server_address[1]}/careers/generic"
    queued = queue_source_discovery(
        db.conn,
        display_name="Restart careers",
        entry_url=url,
        source_family="EMPLOYER_CAREERS",
        now=NOW,
    )
    assert _Handler.hits == 0
    request_id = queued.request_id
    db.close()
    del queued  # the dead process's Python objects are gone

    reopened = Database(path)
    try:
        resumed = load_queued_discovery(reopened.conn, request_id=request_id)
        assert resumed.request_id == request_id
        assert resumed.source_id == load_queued_discovery(
            reopened.conn, run_id=resumed.run_id
        ).source_id
        outcome = execute_source_discovery(
            reopened.conn, resumed, worker_id="restart-worker", now=NOW
        )
        assert _Handler.hits == 1
        assert outcome.run_status == "SUCCEEDED"
        assert outcome.fingerprint is not None
        assert outcome.fingerprint.family is None
        assert outcome.decision is not None
        assert outcome.decision.outcome.value == "GENERIC_DISCOVERY_FALLBACK"
        assert reopened.conn.execute(
            "SELECT status FROM scrape_requests WHERE id = ?", (request_id,)
        ).fetchone()["status"] == "SUCCEEDED"
    finally:
        reopened.close()


def test_discovery_terminal_state_is_atomic_with_fenced_request_commit(
    tmp_path, server, monkeypatch
):
    """A crash after the ownership fence must not strand a terminal request.

    The injected exception fires immediately after the real fenced transaction
    commits and before any caller-side statement can run. Therefore the durable
    request, plan outcome, run counters and aggregate run status must already be
    committed together. Otherwise restart sees a SUCCEEDED request that is no
    longer claimable while its run remains RUNNING.
    """

    class SimulatedCrash(RuntimeError):
        pass

    real_fenced_commit = discovery_runtime.fenced_commit

    @contextmanager
    def crash_immediately_after_real_commit(*args, **kwargs):
        with real_fenced_commit(*args, **kwargs):
            yield
        raise SimulatedCrash("process died immediately after fenced commit")

    monkeypatch.setattr(
        discovery_runtime, "fenced_commit", crash_immediately_after_real_commit
    )

    db = _database(tmp_path / "post-fence-crash.db")
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/careers/greenhouse"
        queued = queue_source_discovery(
            db.conn,
            display_name="Crash-after-commit careers",
            entry_url=url,
            source_family="EMPLOYER_CAREERS",
            now=NOW,
        )

        with pytest.raises(SimulatedCrash):
            execute_source_discovery(
                db.conn, queued, worker_id="crash-worker", now=NOW
            )

        request = db.conn.execute(
            "SELECT status FROM scrape_requests WHERE id = ?", (queued.request_id,)
        ).fetchone()
        plan = db.conn.execute(
            "SELECT group_outcome FROM run_source_plans WHERE id = ?",
            (queued.run_source_plan_id,),
        ).fetchone()
        run = db.conn.execute(
            "SELECT status, requests_total, requests_failed FROM scrape_runs WHERE id = ?",
            (queued.run_id,),
        ).fetchone()

        assert request["status"] == "SUCCEEDED"
        assert plan["group_outcome"] == "SATISFIED"
        assert run["status"] == "SUCCEEDED"
        assert run["requests_total"] == 1
        assert run["requests_failed"] == 0
    finally:
        db.close()


def test_private_discovery_target_is_denied_with_durable_attempt_evidence(tmp_path):
    db = _database(tmp_path / "private.db")
    try:
        queued = queue_source_discovery(
            db.conn,
            display_name="Private target",
            entry_url="http://10.255.255.5/careers",
            source_family="EMPLOYER_CAREERS",
            now=NOW,
        )
        outcome = execute_source_discovery(db.conn, queued, worker_id="security", now=NOW)
        assert outcome.run_status == "PARTIAL"
        assert outcome.page_class.value == "UNKNOWN"
        assert outcome.fingerprint is None
        assert outcome.decision is None
        fetch = db.conn.execute(
            "SELECT * FROM fetch_attempts WHERE request_id = ?", (queued.request_id,)
        ).fetchone()
        assert fetch["failure_kind"] == "POLICY_REJECTED"
        failure = json.loads(fetch["failure_json"])
        assert failure["details_redacted"]["reason_code"] == "PRIVATE_ADDRESS"
        security = db.conn.execute(
            "SELECT * FROM acquisition_evidence"
            " WHERE request_id = ? AND kind = 'SECURITY_POLICY'",
            (queued.request_id,),
        ).fetchall()
        assert len(security) == 1
        assert security[0]["ref"] == "DENIED:PRIVATE_ADDRESS"
        assert db.conn.execute("SELECT COUNT(*) FROM ats_fingerprints").fetchone()[0] == 0
    finally:
        db.close()
