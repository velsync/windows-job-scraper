"""Pre-S2.7 corrective A: the host enforces ``PARTIAL ⇒ coverage degraded``.

Authority: 02 ACQ-03 (a PARTIAL unit "did not complete sufficiently to claim
authoritative coverage … absence inference is forbidden for that coverage
record"), 03 §40 (absence evidence only from ``completion_state = COMPLETE``
with ``absence_inference_allowed``), 03 RUN-13 ("A PARTIAL … enumeration MUST
NOT generate absence evidence").

The S2.6 corrective review found that the driver marked a generation degraded
only on the ``PARTIAL`` *signal path*, which a proposed continuation cursor
pre-empted (``CONTINUE``).  An adapter that returned ``PARTIAL`` **and** a
cursor could therefore have a later clean short page finalize the *same*
generation ``COMPLETE`` — laundering a broken membership proof into absence
authority.  Lever closed that at the adapter (F1); this suite pins the
invariant at the **host authority boundary** so no adapter can reopen it.

The adapter under test is a deliberately minimal, host-owned adversarial
fixture: it is *not* Lever, *not* Greenhouse, and exists only to emit the
exact ``PARTIAL + cursor → clean terminal`` sequence through the real driver,
the real fence and the real durable coverage pipeline.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.adapters import registry as registry_module
from jobscraper.adapters.contract import (
    AdapterTaskKind,
    CrawlCursor,
    ObservationRecord,
    ParseOutcome,
    ParseOutcomeKind,
    validate_manifest,
)
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline import driver as driver_module
from jobscraper.pipeline.driver import execute_run
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-09T09:00:00.000000Z"
ADAPTER_ID = "host_partial_probe"
ADAPTER_VERSION = "0.0.1"

_MANIFEST = validate_manifest(
    {
        "id": ADAPTER_ID,
        "version": ADAPTER_VERSION,
        "adapter_api_version": "1",
        "capabilities": ["listing_parse"],
        "supported_execution_classes": ["HTTP"],
        "supported_auth_modes": ["NONE"],
        "cost_class": "LIGHT",
        "cursor_schema_version": 1,
    }
)


class _ProbeConfig:
    """``pages`` is a list of page *scripts*; each script is one of:

    * ``{"kind": "PARTIAL", "ids": [...], "cursor": True}`` — a recognized page
      with good observations *and* a rejected member, proposing continuation;
    * ``{"kind": "OK", "ids": [...], "cursor": bool}`` — a clean page;
    * ``{"kind": "EMPTY"}`` — a recognized empty (terminal) page.
    """

    def __init__(self, config):
        self.base_url = config["base_url"]
        self.pages = list(config["pages"])


class _ProbeAdapter:
    """Minimal ACQ-02 adapter: plans page N, parses the scripted outcome."""

    manifest = _MANIFEST
    listing_identity_sufficient = True

    def __init__(self, config):
        self.config = config

    @classmethod
    def from_config(cls, config):
        return cls(_ProbeConfig(config))

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _page_index(cursor, ctx) -> int:
        if cursor is None:
            return 0
        try:
            state = json.loads(cursor.state_json)
        except ValueError:
            return 0
        if state.get("plan") != getattr(ctx, "run_source_plan_id", None):
            return 0
        return int(state.get("page", 0))

    def _script(self, index: int) -> dict:
        if index < len(self.config.pages):
            return self.config.pages[index]
        return {"kind": "EMPTY"}

    # -- contract -----------------------------------------------------------
    def plan(self, task, cursor, ctx=None) -> RequestPlan:
        page = self._page_index(cursor, ctx)
        return RequestPlan(
            method="GET",
            url=f"{self.config.base_url}/probe?page={page}",
            headers={"Accept": "application/json"},
            expected_content_types=("application/json",),
            timeout_s=5.0,
            max_bytes=100_000,
            purpose=task.kind.value,
        )

    def parse(self, task, result, ctx=None) -> ParseOutcome:
        body = json.loads(result.envelope.body.decode("utf-8"))
        page = int(body["page"])
        script = self._script(page)
        if script["kind"] == "EMPTY":
            return ParseOutcome(kind=ParseOutcomeKind.SUCCESS_EMPTY)
        observations = tuple(
            ObservationRecord(
                source_job_id=job_id,
                raw_url=result.envelope.final_url,
                canonical_url_candidate=f"https://jobs.example.test/{job_id}",
                application_url_candidate=f"https://jobs.example.test/{job_id}/apply",
                fields={"source_job_id": job_id, "title": f"Role {job_id}", "company": "Probe Co"},
                source_rank_or_order=order,
            )
            for order, job_id in enumerate(script["ids"])
        )
        partial = script["kind"] == "PARTIAL"
        return ParseOutcome(
            kind=ParseOutcomeKind.PARTIAL if partial else ParseOutcomeKind.SUCCESS_WITH_JOBS,
            observations=observations,
            review_evidence=(
                ({"reason": "REQUIRED_FIELD_MISSING", "order": len(script["ids"])},)
                if partial
                else ()
            ),
            continuation_required=bool(script.get("cursor")),
            coverage_proposal={"page": page, "rejected_members": 1 if partial else 0},
        )

    def next_cursor(self, task, outcome, current_cursor, ctx=None):
        # The adversarial behaviour under test: propose a cursor whenever the
        # script says so — *including after PARTIAL*.  A correct adapter would
        # not; the host must not depend on that.
        if outcome.kind in (ParseOutcomeKind.FAILURE, ParseOutcomeKind.SUCCESS_EMPTY):
            return None
        if not outcome.continuation_required:
            return None
        page = self._page_index(current_cursor, ctx) + 1
        return CrawlCursor(
            source_id=current_cursor.source_id if current_cursor else "",
            binding_id=current_cursor.binding_id if current_cursor else "",
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            cursor_schema_version=1,
            state_json=json.dumps(
                {"plan": getattr(ctx, "run_source_plan_id", None), "page": page},
                sort_keys=True,
            ),
            checkpoint_at="",
        )


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    """Echoes the page number back; all semantics live in the adapter."""

    def do_GET(self):  # noqa: N802
        page = 0
        if "page=" in self.path:
            page = int(self.path.rsplit("page=", 1)[1].split("&", 1)[0])
        body = json.dumps({"page": page}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def probe_registry(monkeypatch):
    """Register the adversarial adapter for this test only (no production
    registry change — the probe must never be provisionable for real)."""
    monkeypatch.setitem(registry_module.BUILTIN_ADAPTERS, ADAPTER_ID, _ProbeAdapter)
    yield


@pytest.fixture()
def db(tmp_path, server, probe_registry):
    database = Database(tmp_path / "probe.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    port = server.server_address[1]
    database.conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-1','Probe','PUBLIC_FEED','http://127.0.0.1:{port}/probe','{NOW}','{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('{ADAPTER_ID}','{ADAPTER_VERSION}','1','{{}}','{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-1','src-1','probe','{NOW}');
        """
    )
    # Model the service lifetime: the coordinator opens its service epoch
    # before any claiming (03 §50; claims fail closed without one).
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(database.conn)
    yield database
    database.close()


def _bind(db, server, pages: list[dict]) -> None:
    config = json.dumps({"base_url": f"http://127.0.0.1:{server.server_address[1]}", "pages": pages})
    db.conn.execute(
        """
        INSERT OR REPLACE INTO source_adapter_binding_revisions
            (id, binding_id, revision, adapter_id, adapter_version, strategy, execution_class,
             permission_profile_id, permission_profile_revision, config_json, created_at)
        VALUES ('bndrev-1','bnd-1',1,?,?,'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?,?)
        """,
        (ADAPTER_ID, ADAPTER_VERSION, config, NOW),
    )
    db.conn.commit()


def _start_run(db) -> str:
    plan = dict(
        source_id="src-1", source_plan_group_id="grp-1", fallback_rank=0,
        binding_id="bnd-1", binding_revision_id="bndrev-1",
        adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION, adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", execution_class="HTTP",
        permission_profile_id="perm-1", permission_profile_revision=1,
    )
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=NOW)
    enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="http://127.0.0.1/probe?page=0", logical_key='{"page": 0}',
    )
    return run_id


def _coverage(db):
    return db.conn.execute(
        "SELECT * FROM enumeration_coverage ORDER BY created_at, id"
    ).fetchall()


def _requests(db, run_id):
    return db.conn.execute(
        "SELECT * FROM scrape_requests WHERE run_id = ? AND request_type = 'LIST_FETCH'"
        " ORDER BY created_at, id",
        (run_id,),
    ).fetchall()


def _outcome(db, run_id):
    return db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)
    ).fetchone()["group_outcome"]


def _parses(db):
    return db.conn.execute(
        "SELECT outcome_kind, continuation_required FROM parse_attempts ORDER BY parsed_at, id"
    ).fetchall()


# ---------------------------------------------------------------------------
# The laundering sequence
# ---------------------------------------------------------------------------

LAUNDER = [
    {"kind": "PARTIAL", "ids": ["p-1", "p-2"], "cursor": True},   # page 0: degraded + continue
    {"kind": "OK", "ids": ["p-3"], "cursor": False},              # page 1: clean, terminal
]


def test_partial_with_cursor_cannot_be_laundered_into_complete_by_a_clean_terminal_page(db, server):
    """The key red test.  Sequence: PARTIAL+cursor → clean terminal page.

    Required: the generation is PARTIAL, never COMPLETE; terminal enumeration
    is not proven; absence inference is not granted; the run is
    SATISFIED_PARTIAL; the good observations from the PARTIAL page persist.
    """
    _bind(db, server, LAUNDER)
    run_id = _start_run(db)

    assert execute_run(db.conn, run_id) == "PARTIAL"

    # both pages really ran through the driver (the cursor was honoured).
    # S3.4 normative transition: the PARTIAL page (no retryable typed
    # failure) is a terminally failed/degraded unit, not a success; its
    # accepted observations below still persist atomically.
    requests = _requests(db, run_id)
    assert [r["status"] for r in requests] == ["FAILED", "SUCCEEDED"]
    assert [(p["outcome_kind"], p["continuation_required"]) for p in _parses(db)] == [
        ("PARTIAL", 1), ("SUCCESS_WITH_JOBS", 0),
    ]

    generations = _coverage(db)
    assert len(generations) == 1
    cov = generations[0]
    assert cov["completion_state"] == "PARTIAL", cov["stop_reason"]
    assert cov["terminal_enumeration_proven"] == 0
    assert cov["cursor_terminal"] == 0
    assert cov["finalized_at"] is not None
    assert cov["pages_completed"] == 2
    # the durable flag the absence pipeline reads is off for this generation
    assert cov["absence_inference_allowed"] == 0
    # no absence-driven presence transition was authorised by this generation
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_sources WHERE last_absence_coverage_id = ?", (cov["id"],)
    ).fetchone()[0] == 0
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"

    # valid observations from the PARTIAL page were persisted (ACQ-03)
    titles = [r[0] for r in db.conn.execute("SELECT title FROM jobs ORDER BY title")]
    assert titles == ["Role p-1", "Role p-2", "Role p-3"]
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 3
    seen = {r[0] for r in db.conn.execute(
        "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id = ?", (cov["id"],)
    )}
    assert seen == {"p-1", "p-2", "p-3"}


def test_the_degradation_is_durable_the_moment_the_partial_page_commits(db, server):
    """Invariant (1): degradation is recorded *before* any continuation or
    finalization decision — visible in durable state right after page 0's
    fenced commit, not only at the end of the pass."""
    _bind(db, server, LAUNDER)
    run_id = _start_run(db)

    real_claim = driver_module.claim_next_request
    claims = {"n": 0}

    def die_after_first_page(*args, **kwargs):
        claims["n"] += 1
        if claims["n"] == 2:
            raise RuntimeError("simulated process death after the PARTIAL page")
        return real_claim(*args, **kwargs)

    driver_module.claim_next_request = die_after_first_page
    try:
        with pytest.raises(RuntimeError):
            execute_run(db.conn, run_id)
    finally:
        driver_module.claim_next_request = real_claim

    # page 0 committed PARTIAL and enqueued page 1; the generation is still open.
    # S3.4 normative transition: page 0 is FAILED/degraded (not SUCCEEDED);
    # the durability assertions below are unchanged.
    requests = _requests(db, run_id)
    assert [r["status"] for r in requests] == ["FAILED", "PENDING"]
    cov = _coverage(db)[0]
    assert cov["finalized_at"] is None
    # …and the degradation is already durable on the open generation
    assert cov["absence_inference_allowed"] == 0


def test_restart_between_the_partial_page_and_the_continuation_cannot_erase_degradation(db, server):
    """Invariant (6): a resumed pass, which never saw the PARTIAL outcome in
    memory, still finalizes the resumed generation PARTIAL."""
    _bind(db, server, LAUNDER)
    run_id = _start_run(db)

    real_claim = driver_module.claim_next_request
    claims = {"n": 0}

    def die_after_first_page(*args, **kwargs):
        claims["n"] += 1
        if claims["n"] == 2:
            raise RuntimeError("simulated process death after the PARTIAL page")
        return real_claim(*args, **kwargs)

    driver_module.claim_next_request = die_after_first_page
    try:
        with pytest.raises(RuntimeError):
            execute_run(db.conn, run_id)
    finally:
        driver_module.claim_next_request = real_claim
    open_generation = _coverage(db)[0]["id"]

    # a fresh pass: in-memory `coverage_degraded` starts False here
    assert execute_run(db.conn, run_id) == "PARTIAL"

    generations = _coverage(db)
    assert len(generations) == 1  # the open generation was resumed, not replaced
    cov = generations[0]
    assert cov["id"] == open_generation
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert cov["absence_inference_allowed"] == 0
    assert cov["pages_completed"] >= 1
    # S3.4 normative transition (see above): the PARTIAL page is FAILED.
    assert [r["status"] for r in _requests(db, run_id)] == ["FAILED", "SUCCEEDED"]
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_a_partial_page_followed_by_a_recognized_empty_page_is_still_not_complete(db, server):
    """The EMPTY signal path is a second terminal route; it must not launder
    either."""
    _bind(db, server, [
        {"kind": "PARTIAL", "ids": ["p-1"], "cursor": True},
        {"kind": "EMPTY"},
    ])
    run_id = _start_run(db)
    assert execute_run(db.conn, run_id) == "PARTIAL"
    assert [p["outcome_kind"] for p in _parses(db)] == ["PARTIAL", "SUCCESS_EMPTY"]
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["terminal_enumeration_proven"] == 0
    assert cov["absence_inference_allowed"] == 0
    assert _outcome(db, run_id) == "SATISFIED_PARTIAL"


def test_a_partial_page_in_the_middle_of_a_walk_degrades_the_whole_generation(db, server):
    _bind(db, server, [
        {"kind": "OK", "ids": ["p-1"], "cursor": True},
        {"kind": "PARTIAL", "ids": ["p-2"], "cursor": True},
        {"kind": "OK", "ids": ["p-3"], "cursor": False},
    ])
    run_id = _start_run(db)
    assert execute_run(db.conn, run_id) == "PARTIAL"
    assert [p["outcome_kind"] for p in _parses(db)] == ["SUCCESS_WITH_JOBS", "PARTIAL", "SUCCESS_WITH_JOBS"]
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "PARTIAL"
    assert cov["absence_inference_allowed"] == 0
    assert cov["pages_completed"] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


# ---------------------------------------------------------------------------
# Control: a clean paginated generation is unaffected
# ---------------------------------------------------------------------------


def test_a_clean_paginated_generation_still_completes_with_absence_authority(db, server):
    """Invariant (7): the hardening only bites on PARTIAL."""
    _bind(db, server, [
        {"kind": "OK", "ids": ["p-1", "p-2"], "cursor": True},
        {"kind": "OK", "ids": ["p-3"], "cursor": True},
        {"kind": "EMPTY"},
    ])
    run_id = _start_run(db)
    assert execute_run(db.conn, run_id) == "SUCCEEDED"
    assert [p["outcome_kind"] for p in _parses(db)] == [
        "SUCCESS_WITH_JOBS", "SUCCESS_WITH_JOBS", "SUCCESS_EMPTY",
    ]
    cov = _coverage(db)[0]
    assert cov["completion_state"] == "COMPLETE"
    assert cov["terminal_enumeration_proven"] == 1
    assert cov["absence_inference_allowed"] == 1
    assert cov["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
    assert cov["pages_completed"] == 3
    assert _outcome(db, run_id) == "SATISFIED"
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_a_clean_generation_after_a_degraded_one_regains_absence_authority(db, server):
    """Degradation is per generation, not per binding: the next run's
    generation starts clean (a later COMPLETE run is what eventually ages
    the presences the degraded run could not judge)."""
    _bind(db, server, LAUNDER)
    first = _start_run(db)
    assert execute_run(db.conn, first) == "PARTIAL"

    _bind(db, server, [
        {"kind": "OK", "ids": ["p-1", "p-3"], "cursor": False},
    ])
    second = _start_run(db)
    assert execute_run(db.conn, second) == "SUCCEEDED"

    generations = _coverage(db)
    assert [g["completion_state"] for g in generations] == ["PARTIAL", "COMPLETE"]
    assert [g["absence_inference_allowed"] for g in generations] == [0, 1]
    # and the COMPLETE generation — not the PARTIAL one — is what judged p-2
    p2 = db.conn.execute(
        "SELECT presence_state, last_absence_coverage_id FROM job_sources WHERE source_job_id = 'p-2'"
    ).fetchone()
    assert p2["presence_state"] == "UNCERTAIN"
    assert p2["last_absence_coverage_id"] == generations[1]["id"]
