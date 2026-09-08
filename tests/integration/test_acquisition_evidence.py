"""S2.1 integration: richer ResultEnvelope, durable evidence, origin resolution.

Proves the Slice-2 evidence contract end-to-end through the real chain
(``Source → BindingRevision → RunSourcePlan → host-owned execution →
ResultEnvelope → Observation → canonical pipeline``):

* the fetch attempt persists the complete §11.3 envelope projection —
  content hash *and* content-class-normalized hash, body/structured
  references, transport, validators, robots decision, security-policy result —
  with secret-bearing response headers excluded;
* the parse attempt is recorded under the ACQ-09 contract, including the
  page class the validity gate produced and the result-envelope reference;
* every accepted observation links to its fetch and parse attempts, and the
  ``acquisition_evidence`` spine carries the result/validity/origin rows;
* the origin resolver runs host-side over already-recorded evidence and
  persists per-presence origin identity + confidence, resolving ATS-shaped
  links and honestly recording NULL (with evidence) for employer-direct ones.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.driver import execute_run
from jobscraper.pipeline.evidence import EVIDENCE_DETAIL_LIMIT
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T09:00:00.000000Z"

GREENHOUSE_URL = "https://boards.greenhouse.io/acme/jobs/1234567"
GREENHOUSE_APPLY = (
    "https://job-boards.greenhouse.io/acme/jobs/1234567"
    "?gh_jid=1234567&utm_source=jobsboard"
)
EMPLOYER_URL = "https://jobs.example.test/postings/fx-direct-9"


def _page(one: bool) -> bytes:
    jobs = [
        {
            "id": "gh-1234567",
            "title": "Staff Backend Engineer",
            "company": "Acme Data",
            "location": ["Berlin, DE"],
            "url": GREENHOUSE_URL,
            "application_url": GREENHOUSE_APPLY,
            "updated_at": NOW,
        },
        {
            "id": "fx-direct-9",
            "title": "Platform Engineer",
            "company": "Fixture Corp",
            "location": ["Remote"],
            "url": EMPLOYER_URL,
            "application_url": EMPLOYER_URL + "/apply",
            "updated_at": NOW,
        },
    ]
    if not one:
        jobs = []
    return json.dumps({"jobs": jobs}).encode()


class _EvidenceHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/ghfeed"):
            body = _page(one="page=1" in self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            # must never reach the durable envelope (04 SEC-08)
            self.send_header("Set-Cookie", "session=super-secret; Path=/")
            self.send_header("ETag", '"repr-1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):  # pragma: no cover
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EvidenceHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def db(tmp_path, server):
    port = server.server_address[1]
    # the destination policy pins the fetch host to the source's own entry
    # host (04 §5.1), so the entry URL is the loopback fixture feed while the
    # *collected links* are the ATS URLs the origin resolver must recognize
    entry_url = f"http://127.0.0.1:{port}/ghfeed"
    feed_config = json.dumps(
        {
            "url_template": entry_url + "?page={page}",
            "items_path": "jobs",
            "fields": {
                "source_job_id": {"path": "id", "required": True},
                "title": {"path": "title", "required": True},
                "company": {"path": "company"},
                "locations": {"path": "location", "many": True},
                "job_url": {"path": "url"},
                "apply_url": {"path": "application_url"},
                "posted_at": {"path": "updated_at"},
            },
        }
    )
    database = Database(tmp_path / "evidence.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-1','Evidence Feed','PUBLIC_FEED',
                '{entry_url}', '{NOW}', '{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}','{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-1','src-1','api','{NOW}');
        INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id, adapter_version,
            strategy, execution_class, permission_profile_id, permission_profile_revision, config_json, created_at)
        VALUES ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,
            '{feed_config}','{NOW}');
        """
    )
    conn.commit()
    yield database
    database.close()


def _run_once(db, run_tag: str) -> str:
    plan = dict(
        source_id="src-1",
        source_plan_group_id=f"grp-{run_tag}",
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
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=NOW)
    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plans[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity=f"http://127.0.0.1/jobs?page=1#{run_tag}",
        logical_key=f'{{"page": 1, "run": "{run_tag}"}}',
    )
    return execute_run(db.conn, run_id)


def test_fetch_attempt_persists_the_full_envelope_projection(db):
    assert _run_once(db, "r1") == "SUCCEEDED"
    row = db.conn.execute(
        "SELECT * FROM fetch_attempts WHERE status_code = 200"
    ).fetchone()
    assert row is not None
    # §11.3 completeness under the ACQ-09 contract
    assert row["contract_version"] >= 2
    assert row["transport"] == "http"
    assert row["browser_used"] == 0
    assert row["robots_decision"] == "NOT_EVALUATED"
    assert row["resource_blocking_applied"] == 0
    assert row["execution_plan_id"]
    assert row["was_304"] == 0
    # content hash over raw bytes and a *class-normalized* hash, both present
    assert len(row["body_hash"]) == 64
    assert len(row["normalized_content_hash"]) == 64
    assert row["body_hash"] != row["normalized_content_hash"]
    # consumers pass references, never copies of the body
    assert row["body_ref"].startswith("result://")
    assert "/body/" in row["body_ref"]
    assert "/structured/" in row["structured_payload_ref"]
    assert "body" not in row.keys()  # the envelope body never lands here
    # redaction boundary holds in the durable projection
    headers = {k.lower(): v for k, v in json.loads(row["headers_redacted_json"]).items()}
    assert "set-cookie" not in headers
    assert "www-authenticate" not in headers
    assert "proxy-authenticate" not in headers
    assert headers["etag"] == '"repr-1"'
    assert db.conn.execute(
        "SELECT COUNT(*) FROM acquisition_evidence WHERE kind='RESULT_ENVELOPE'"
    ).fetchone()[0] >= 1


def test_parse_attempt_records_the_validity_gate_and_refs(db):
    _run_once(db, "r1")
    row = db.conn.execute(
        "SELECT * FROM parse_attempts WHERE outcome_kind = 'SUCCESS_WITH_JOBS'"
    ).fetchone()
    assert row is not None
    assert row["contract_version"] >= 2
    assert row["validated_page_class"] == "VALID_LIST"
    assert row["result_envelope_ref"] == db.conn.execute(
        "SELECT body_ref FROM fetch_attempts WHERE status_code = 200"
    ).fetchone()[0]
    evidence = json.loads(row["validation_evidence_json"])
    assert evidence["status_code"] == 200
    # the ACQ-09 evidence channels are durable even when this adapter has
    # nothing to report through them (S2.5's Greenhouse parser populates them)
    assert row["closure_evidence_json"] == "[]"
    assert row["review_evidence_json"] == "[]"
    assert row["evidence_refs_json"] == "[]"
    assert row["continuation_required"] == 0


def test_observation_links_and_evidence_spine(db):
    _run_once(db, "r1")
    obs = db.conn.execute(
        "SELECT * FROM job_observations ORDER BY observed_at, source_job_id"
    ).fetchall()
    assert len(obs) == 2
    for row in obs:
        assert row["fetch_attempt_id"] is not None
        assert row["parse_attempt_id"] is not None
        assert row["contract_version"] >= 2
        assert db.conn.execute(
            "SELECT 1 FROM fetch_attempts WHERE id = ?", (row["fetch_attempt_id"],)
        ).fetchone()
        assert db.conn.execute(
            "SELECT 1 FROM parse_attempts WHERE id = ?", (row["parse_attempt_id"],)
        ).fetchone()
    # field evidence carries its source reference and excerpt hash
    fe = db.conn.execute(
        "SELECT * FROM field_evidence WHERE field_name = 'title'"
    ).fetchall()
    assert fe
    assert all(row["excerpt_hash"] and len(row["excerpt_hash"]) == 64 for row in fe)
    kinds = {
        r[0]
        for r in db.conn.execute("SELECT DISTINCT kind FROM acquisition_evidence")
    }
    assert {"RESULT_ENVELOPE", "PAGE_VALIDITY", "ORIGIN_RESOLUTION"} <= kinds


def test_origin_resolver_persists_ats_identity_for_the_ats_shaped_observation(db):
    _run_once(db, "r1")
    presence = db.conn.execute(
        "SELECT * FROM job_sources WHERE source_job_id = 'gh-1234567'"
    ).fetchone()
    assert presence["origin_provider"] == "GREENHOUSE"
    assert presence["origin_board"] == "acme"
    assert presence["origin_job_id"] == "1234567"
    assert presence["origin_resolution_confidence"] >= 0.75
    detail = json.loads(presence["origin_resolution_evidence_json"])
    assert detail["status"] == "RESOLVED"
    assert detail["origin_url"] == GREENHOUSE_URL
    assert detail["direct_application_url"].startswith(
        "https://boards.greenhouse.io/acme/jobs/1234567"
    )
    assert detail["preserved_source_url"].endswith("/ghfeed")
    # the ATS host is *not* the fetch host here: recorded honestly
    assert detail["same_host_as_source"] is False
    assert {"ATS_ENDPOINT", "TRACKING_CLEANED"} <= {
        e["kind"] for e in detail["evidence"]
    }
    # tracking parameters are cleaned; the semantic gh_jid survives
    assert "utm_source" not in detail["origin_url"]
    # the observation's own recorded URLs stay exactly as collected
    obs = db.conn.execute(
        "SELECT * FROM job_observations WHERE source_job_id = 'gh-1234567'"
    ).fetchone()
    assert obs["application_url_candidate"] == GREENHOUSE_APPLY
    assert obs["raw_url"].startswith("http://127.0.0.1")


def test_employer_direct_observation_is_honestly_unresolved_not_guessed(db):
    _run_once(db, "r1")
    presence = db.conn.execute(
        "SELECT * FROM job_sources WHERE source_job_id = 'fx-direct-9'"
    ).fetchone()
    for column in ("origin_provider", "origin_board", "origin_job_id",
                   "origin_resolution_confidence"):
        assert presence[column] is None, column
    # unresolved is evidence too, attached to the observation
    evidence = db.conn.execute(
        "SELECT * FROM acquisition_evidence WHERE kind = 'ORIGIN_RESOLUTION'"
        " AND observation_id = (SELECT id FROM job_observations"
        " WHERE source_job_id = 'fx-direct-9')"
    ).fetchone()
    detail = json.loads(evidence["detail_json"])
    assert detail["status"] == "UNRESOLVED"
    assert any(e["kind"] == "NO_ATS_PATTERN" for e in detail["evidence"])
    assert presence["origin_resolution_evidence_json"] == "{}"


def test_evidence_is_append_only_and_origin_resolution_is_one_per_observation(db):
    _run_once(db, "r1")
    snapshot = {
        row["id"]: (row["kind"], row["ref"], row["detail_json"], row["content_hash"])
        for row in db.conn.execute(
            "SELECT id, kind, ref, detail_json, content_hash FROM acquisition_evidence"
        )
    }
    presences_before = db.conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0]
    assert _run_once(db, "r2") in ("SUCCEEDED", "PARTIAL")

    after = {
        row["id"]: (row["kind"], row["ref"], row["detail_json"], row["content_hash"])
        for row in db.conn.execute(
            "SELECT id, kind, ref, detail_json, content_hash FROM acquisition_evidence"
        )
    }
    # durable evidence is appended, never rewritten
    assert set(snapshot) <= set(after)
    assert all(after[evidence_id] == snapshot[evidence_id] for evidence_id in snapshot)
    assert len(after) > len(snapshot)
    # provenance is stable: the second run re-enumerates from the saved cursor
    # and must not duplicate presences or invent a second origin story
    assert db.conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == presences_before
    rows = db.conn.execute(
        "SELECT o.id, COUNT(e.id) FROM job_observations o"
        " LEFT JOIN acquisition_evidence e"
        "   ON e.kind = 'ORIGIN_RESOLUTION' AND e.observation_id = o.id"
        " GROUP BY o.id"
    ).fetchall()
    assert rows
    assert all(row[1] == 1 for row in rows)


def test_integrity_check_still_clean_after_evidence_writes(db):
    _run_once(db, "r1")
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_durable_evidence_blobs_are_parseable_and_within_the_bound(db):
    """Truncation must never leave unreadable provenance behind (03 §30).

    Review and export paths ``json.loads`` these columns.  Bounding the
    serialized text with a slice would store invalid JSON, so the writer shrinks
    the *document* and marks that it did.
    """
    _run_once(db, "bounded")
    rows = db.conn.execute("SELECT id, kind, detail_json FROM acquisition_evidence").fetchall()
    assert rows, "a completed run must leave durable evidence"
    for row in rows:
        payload = json.loads(row["detail_json"])  # must not raise
        assert isinstance(payload, dict)
        assert len(row["detail_json"]) <= EVIDENCE_DETAIL_LIMIT, (row["kind"], len(row["detail_json"]))


def test_field_evidence_hash_matches_the_stored_excerpt(db):
    """A hash a reader cannot reproduce is not evidence (03 §30)."""
    _run_once(db, "hashes")
    rows = db.conn.execute(
        "SELECT field_name, excerpt, excerpt_hash FROM field_evidence"
    ).fetchall()
    assert rows, "the fixture feed must produce field evidence"
    for row in rows:
        if row["excerpt"]:
            assert row["excerpt_hash"] == hashlib.sha256(row["excerpt"].encode()).hexdigest(), row[
                "field_name"
            ]
        else:
            assert row["excerpt_hash"] is None, row["field_name"]
