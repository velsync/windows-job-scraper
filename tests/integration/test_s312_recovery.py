"""S3.12 deterministic startup recovery and crash/restart integration.

These tests intentionally exercise durable boundaries rather than pretending a
Python process can resume an interrupted call stack.  Each case creates the
database shape that would survive the named crash point, opens a fresh service
epoch, runs startup recovery, and proves the only legal durable continuation.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from jobscraper.acquisition.crawler.cursor import load_cursor, save_cursor
from jobscraper.acquisition.crawler.pagination import PaginationGuardState
from jobscraper.adapters.contract import CrawlCursor
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.coverage import (
    finalize_coverage,
    open_or_resume_coverage,
    record_contributing_request,
)
from jobscraper.runtime.claims import StaleOwnership, claim_next_request
from jobscraper.runtime.clock import NoActiveServiceEpoch, begin_service_epoch
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.rate import RateKey, dispatch_gate, record_failure
from jobscraper.runtime.recovery import recover_interrupted_requests, recover_startup_state
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run, set_group_outcome

NOW = "2026-09-12T11:00:00.000000Z"
LATER = "2026-09-12T11:05:00.000000Z"
FEED_CONFIG_JSON = (
    '{"url_template":"https://example.test/jobs?page={page}",'
    '"items_path":"jobs","fields":{'
    '"source_job_id":{"path":"id","required":true},'
    '"title":{"path":"title","required":true}}}'
)


def _authority_rows(conn) -> None:
    conn.execute(
        "INSERT INTO sources (id, display_name, source_family, entry_url,"
        " desired_state, administrative_state, created_at, updated_at)"
        " VALUES ('src-1','Recovery fixture','PUBLIC_FEED',"
        " 'https://example.test/jobs','ENABLED','NORMAL',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO adapter_definitions"
        " (adapter_id,adapter_version,adapter_api_version,manifest_json,created_at)"
        " VALUES ('json_api_feed','1.0.0','1','{}',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles (id,display_name,created_at)"
        " VALUES ('perm-1','default',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions"
        " (id,permission_profile_id,revision,policy_json,created_at)"
        " VALUES ('permrev-1','perm-1',1,'{}',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO source_adapter_bindings"
        " (id,source_id,display_name,created_at)"
        " VALUES ('bnd-1','src-1','api',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id,binding_id,revision,adapter_id,adapter_version,strategy,"
        " execution_class,permission_profile_id,permission_profile_revision,"
        " config_json,created_at)"
        " VALUES ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0',"
        " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?,?)",
        (FEED_CONFIG_JSON, NOW),
    )
    conn.commit()


def _plan(*, rank: int = 0, group: str = "grp-1") -> dict:
    return dict(
        source_id="src-1",
        source_plan_group_id=group,
        fallback_rank=rank,
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


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "s312.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    _authority_rows(database.conn)
    begin_service_epoch(database.conn, now=NOW)
    yield database
    database.close()


def _run_with_request(
    db: Database,
    *,
    request_type: str = "LIST_FETCH",
    max_attempts: int = 3,
):
    run_id, plan_ids = create_run(
        db.conn, profile_id=None, plans=[_plan()], now=NOW
    )
    request_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type=request_type,
        target_identity="https://example.test/jobs?page=1",
        logical_key="page-1",
        max_attempts=max_attempts,
        now=NOW,
    )
    return run_id, plan_ids[0], request_id


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_claim_before_dispatch",
        "during_http_io",
        "after_fetch_before_output_commit",
    ],
)
def test_s312_prior_epoch_running_states_reclaim_to_retry(
    db: Database, crash_point: str
):
    """All three pre-fence crash points have the same durable shape: RUNNING
    ownership with no accepted output.  Recovery must abandon that exact
    attempt and preserve retry semantics, never synthesize success."""
    _run_id, _plan_id, request_id = _run_with_request(db)
    claim = claim_next_request(db.conn, "dead-worker", now=NOW)
    assert claim is not None and claim.request_id == request_id

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)

    assert report["reclaimed"] == [request_id], crash_point
    row = db.conn.execute(
        "SELECT status,current_worker_id,current_attempt_id,lease_until,"
        " next_retry_at,attempt_count FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    assert row["status"] == "RETRY_WAIT"
    assert row["current_worker_id"] is None
    assert row["current_attempt_id"] is None
    assert row["lease_until"] is None
    assert row["next_retry_at"] > LATER
    assert row["attempt_count"] == 1
    attempt = db.conn.execute(
        "SELECT outcome,abandoned_reason FROM request_attempts"
        " WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()
    assert attempt["outcome"] == "ABANDONED"
    assert attempt["abandoned_reason"] == "SERVICE_RESTART"

    # The dead prior-epoch worker can never commit after recovery.
    with pytest.raises(StaleOwnership):
        with fenced_commit(
            db.conn, request_id, claim.attempt_id, now=LATER
        ):
            pass


@pytest.mark.parametrize(
    "crash_stage",
    ["after_fetch_evidence", "after_observation", "after_child_enqueue"],
)
def test_s312_crash_inside_output_fence_rolls_back_outputs_then_retries_parent(
    db: Database, crash_stage: str
):
    """Fault-inject at three points inside the request output fence.

    Fetch evidence, observation/local obligation output, and the parent terminal
    request transition share one SQLite transaction.  None may survive a crash
    unless the whole fence commits.
    """
    run_id, plan_id, request_id = _run_with_request(db)
    claim = claim_next_request(db.conn, "dead-worker", now=NOW)
    assert claim is not None

    class SimulatedCrash(RuntimeError):
        pass

    with pytest.raises(SimulatedCrash):
        with fenced_commit(
            db.conn, request_id, claim.attempt_id, now=NOW
        ):
            db.conn.execute(
                "INSERT INTO fetch_attempts"
                " (id,attempt_id,request_id,requested_url,fetched_at)"
                " VALUES ('fa-crash',?,?,?,?)",
                (
                    claim.attempt_id,
                    request_id,
                    "https://example.test/jobs?page=1",
                    NOW,
                ),
            )
            if crash_stage == "after_fetch_evidence":
                raise SimulatedCrash(crash_stage)

            db.conn.execute(
                "INSERT INTO job_observations"
                " (id,run_id,request_id,attempt_id,source_id,binding_id,"
                " adapter_id,adapter_version,strategy,execution_class,"
                " observed_at,observation_unique_key)"
                " VALUES ('obs-crash',?,?,?,?,?,'json_api_feed','1.0.0',"
                " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP',?,'obs-crash')",
                (
                    run_id, request_id, claim.attempt_id, "src-1", "bnd-1", NOW
                ),
            )
            if crash_stage == "after_observation":
                raise SimulatedCrash(crash_stage)

            enqueue_request(
                db.conn,
                run_id=run_id,
                run_source_plan_id=plan_id,
                source_id="src-1",
                binding_id="bnd-1",
                request_type="RECONCILE",
                target_identity="job-crash-window",
                logical_key="reconcile-crash-window",
                payload={},
                parent_request_id=request_id,
                now=NOW,
                commit=False,
            )
            raise SimulatedCrash(crash_stage)

    parent = db.conn.execute(
        "SELECT status,current_attempt_id FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    assert parent["status"] == "RUNNING"
    assert parent["current_attempt_id"] == claim.attempt_id
    assert db.conn.execute(
        "SELECT COUNT(*) FROM fetch_attempts WHERE id='fa-crash'"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_observations WHERE id='obs-crash'"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_requests"
        " WHERE parent_request_id=? AND request_type='RECONCILE'",
        (request_id,),
    ).fetchone()[0] == 0

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)
    assert report["reclaimed"] == [request_id]
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id=?", (request_id,)
    ).fetchone()["status"] == "RETRY_WAIT"


def test_s312_succeeded_request_is_immutable_and_never_replayed(
    db: Database,
):
    """Crash immediately after the SUCCEEDED fence: startup recovery must not
    turn accepted network work back into PENDING/RETRY_WAIT."""
    _run_id, _plan_id, request_id = _run_with_request(db)
    claim = claim_next_request(db.conn, "worker", now=NOW)
    assert claim is not None
    with fenced_commit(db.conn, request_id, claim.attempt_id, now=NOW):
        pass

    before = db.conn.execute(
        "SELECT status,attempt_count,finished_at FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    assert before["status"] == "SUCCEEDED"

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)

    after = db.conn.execute(
        "SELECT status,attempt_count,finished_at FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    assert report["reclaimed"] == []
    assert tuple(after) == tuple(before)


def test_s312_retry_wait_and_retry_after_cooldown_survive_restart(
    db: Database,
):
    """Recovery must preserve durable retry/cooldown state instead of making
    work immediately claimable after a process restart."""
    _run_id, _plan_id, request_id = _run_with_request(db)
    claim = claim_next_request(db.conn, "worker", now=NOW)
    assert claim is not None
    with fenced_commit(
        db.conn,
        request_id,
        claim.attempt_id,
        now=NOW,
        outcome="RETRY_WAIT",
        retry_delay_s=600.0,
        failure_kind="RATE_LIMIT",
    ):
        pass

    key = RateKey(binding_id="bnd-1", host="example.test")
    record_failure(
        db.conn,
        key,
        failure_kind="RATE_LIMIT",
        delay_s=600.0,
        retry_after_raw="600",
        now=NOW,
    )
    before = db.conn.execute(
        "SELECT status,next_retry_at FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    gate_before = dispatch_gate(db.conn, key, now=LATER)
    assert before["status"] == "RETRY_WAIT"
    assert not gate_before.allowed

    begin_service_epoch(db.conn, now=LATER)
    recover_startup_state(db.conn, now=LATER)

    after = db.conn.execute(
        "SELECT status,next_retry_at FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    gate_after = dispatch_gate(db.conn, key, now=LATER)
    assert tuple(after) == tuple(before)
    assert gate_after == gate_before


def test_s312_cancel_flag_without_running_owner_is_swept_and_local_work_drains(
    db: Database,
):
    """Corrective S3.12 case: crash after cancel_requested_at commits but
    before the cancellation sweep, with no RUNNING owner to discover through
    legacy restart reclamation.  Acquisition is cancelled; accepted local
    work still drains; the run then becomes CANCELLED."""
    run_id, plan_id, acquisition_id = _run_with_request(db)
    local_id, created = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-1",
        binding_id="bnd-1",
        request_type="RECONCILE",
        target_identity="accepted-local-evidence",
        logical_key="accepted-local-evidence",
        payload={},
        now=NOW,
    )
    assert created

    # Durable cancellation flag only: this is the crash window before
    # request_run_cancellation gets to perform its sweep.
    db.conn.execute(
        "UPDATE scrape_runs SET cancel_requested_at=? WHERE id=?",
        (NOW, run_id),
    )
    db.conn.commit()

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)

    statuses = {
        row["id"]: row["status"]
        for row in db.conn.execute(
            "SELECT id,status FROM scrape_requests WHERE id IN (?,?)",
            (acquisition_id, local_id),
        ).fetchall()
    }
    assert statuses[acquisition_id] == "CANCELLED"
    assert statuses[local_id] == "SUCCEEDED"
    assert report["drained_local_obligations"] == 1
    assert run_id in report["finalized_cancelled_runs"]
    assert db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id=?", (run_id,)
    ).fetchone()["status"] == "CANCELLED"


def test_s312_running_local_obligation_is_reclaimed_due_and_drained(
    db: Database,
):
    """A service crash can also happen while a host-native obligation owns a
    lease.  The dead attempt is preserved as ABANDONED, but source-style retry
    backoff must not strand deterministic accepted local work after boot."""
    run_id, plan_ids = create_run(
        db.conn, profile_id=None, plans=[_plan()], now=NOW
    )
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=NOW)
    local_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="RECONCILE",
        target_identity="accepted-local-running",
        logical_key="accepted-local-running",
        payload={},
        now=NOW,
    )
    claim = claim_next_request(
        db.conn,
        "dead-local-worker",
        now=NOW,
        types=frozenset({"RECONCILE"}),
    )
    assert claim is not None and claim.request_id == local_id

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)

    assert report["reclaimed"] == [local_id]
    assert report["released_local_retries"] == [local_id]
    assert report["drained_local_obligations"] == 1
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id=?", (local_id,)
    ).fetchone()["status"] == "SUCCEEDED"
    prior = db.conn.execute(
        "SELECT outcome,abandoned_reason FROM request_attempts WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()
    assert tuple(prior) == ("ABANDONED", "SERVICE_RESTART")
    assert db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id=?", (run_id,)
    ).fetchone()["status"] == "SUCCEEDED"



def test_s312_second_crash_after_local_reclaim_cannot_strand_obligation(
    db: Database,
):
    """Recovery itself is restartable.  Simulate a second process death after
    the RUNNING local obligation has been reclaimed but before the rest of
    recover_startup_state executes. The reclaim transaction must already have
    made deterministic local work due, so the next startup drains it without
    waiting for source-style backoff or a user-driven run."""
    run_id, plan_ids = create_run(
        db.conn, profile_id=None, plans=[_plan()], now=NOW
    )
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=NOW)
    local_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="RECONCILE",
        target_identity="accepted-local-second-crash",
        logical_key="accepted-local-second-crash",
        payload={},
        now=NOW,
    )
    claim = claim_next_request(
        db.conn,
        "dead-local-worker",
        now=NOW,
        types=frozenset({"RECONCILE"}),
    )
    assert claim is not None and claim.request_id == local_id

    # First restart begins, then "crashes" immediately after the request-level
    # reclaim primitive. No _release_reclaimed_local_retries/drain call happens.
    begin_service_epoch(db.conn, now=LATER)
    first = recover_interrupted_requests(db.conn, now=LATER)
    assert first["reclaimed"] == [local_id]
    stranded_window = db.conn.execute(
        "SELECT status,next_retry_at FROM scrape_requests WHERE id=?",
        (local_id,),
    ).fetchone()
    assert tuple(stranded_window) == ("RETRY_WAIT", LATER)

    # A second service lifetime must be able to drain from durable state alone;
    # the in-memory `reclaimed` list from the dead recovery process is gone.
    begin_service_epoch(db.conn, now=LATER)
    second = recover_startup_state(db.conn, now=LATER)
    assert second["reclaimed"] == []
    assert second["drained_local_obligations"] == 1
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id=?", (local_id,)
    ).fetchone()["status"] == "SUCCEEDED"
    assert db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id=?", (run_id,)
    ).fetchone()["status"] == "SUCCEEDED"

def test_s312_crash_after_coverage_finalize_repairs_exact_fallback_rank_once(
    db: Database,
):
    """Model the real S3.8→S3.9 crash boundary: the active rank's request and
    coverage terminal state are durable, but the following set_group_outcome
    transaction never ran.  Startup must feed that durable result through the
    existing S3.9 owner exactly once, activating rank 1 and never skipping it."""
    run_id, plan_ids = create_run(
        db.conn,
        profile_id=None,
        plans=[_plan(rank=0), _plan(rank=1)],
        now=NOW,
    )
    coverage_id, _ = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        scope_key="full-source",
        generation_key="rank-0",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        listing_identity_sufficient=True,
        now=NOW,
    )
    request_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?rank=0",
        logical_key="rank-0",
        now=NOW,
    )
    record_contributing_request(db.conn, coverage_id, request_id)
    claim = claim_next_request(
        db.conn, "worker", now=NOW, run_source_plan_id=plan_ids[0]
    )
    assert claim is not None and claim.request_id == request_id
    # Pre-dispatch/policy-style failure: no fetch/parse output was accepted.
    with fenced_commit(
        db.conn,
        request_id,
        claim.attempt_id,
        now=NOW,
        outcome="FAILED",
        failure_kind="POLICY_REJECTED",
    ):
        pass
    finalize_coverage(
        db.conn,
        coverage_id,
        completion_state="PARTIAL",
        stop_reason="driver stop",
        terminal_enumeration_proven=False,
        now=NOW,
    )

    def group_rank():
        return db.conn.execute(
            "SELECT active_fallback_rank FROM source_plan_group_state"
            " WHERE run_id=? AND source_plan_group_id='grp-1'",
            (run_id,),
        ).fetchone()["active_fallback_rank"]

    assert group_rank() == 0
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE id=?", (plan_ids[0],)
    ).fetchone()["group_outcome"] is None

    # Fault injection *inside* S3.9 fallback activation. SQLite ABORT forces
    # set_group_outcome's transaction to roll back, so restart sees the exact
    # pre-activation durable shape rather than a half-written rank transition.
    db.conn.execute(
        "CREATE TRIGGER s312_fail_fallback BEFORE UPDATE OF active_fallback_rank"
        " ON source_plan_group_state"
        " WHEN NEW.active_fallback_rank <> OLD.active_fallback_rank"
        " BEGIN SELECT RAISE(ABORT, 's312 injected fallback crash'); END"
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected fallback crash"):
        set_group_outcome(db.conn, plan_ids[0], "FAILED", now=NOW)
    assert group_rank() == 0
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE id=?", (plan_ids[0],)
    ).fetchone()["group_outcome"] is None
    db.conn.execute("DROP TRIGGER s312_fail_fallback")
    db.conn.commit()

    begin_service_epoch(db.conn, now=LATER)
    first = recover_startup_state(db.conn, now=LATER)
    second = recover_startup_state(db.conn, now=LATER)

    assert first["repaired_plan_outcomes"] == [
        {"plan_id": plan_ids[0], "outcome": "FAILED"}
    ]
    assert group_rank() == 1
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE id=?", (plan_ids[0],)
    ).fetchone()["group_outcome"] == "FAILED"
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE id=?", (plan_ids[1],)
    ).fetchone()["group_outcome"] is None
    assert second["repaired_plan_outcomes"] == []
    assert group_rank() == 1


def test_s312_unfinished_coverage_with_open_work_resumes_same_generation(
    db: Database,
):
    """When durable acquisition work remains open, startup validates the one
    unfinished generation against immutable plan identity and leaves it open
    for the normal driver rather than inventing a new generation."""
    run_id, plan_ids = create_run(
        db.conn, profile_id=None, plans=[_plan()], now=NOW
    )
    coverage_id, resumed = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        scope_key="full-source",
        generation_key="run-recovery",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        listing_identity_sufficient=True,
        now=NOW,
    )
    assert not resumed
    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?page=1",
        logical_key="coverage-open-page-1",
        now=NOW,
    )

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)
    assert report["repaired_coverages"] == []

    recovered_id, resumed = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        scope_key="full-source",
        generation_key="ignored-on-resume",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        listing_identity_sufficient=True,
        now=LATER,
    )
    assert resumed and recovered_id == coverage_id
    row = db.conn.execute(
        "SELECT finalized_at,completion_state FROM enumeration_coverage WHERE id=?",
        (coverage_id,),
    ).fetchone()
    assert tuple(row) == (None, None)


def test_s312_crash_before_coverage_finalize_repairs_complete_then_group(
    db: Database,
):
    """Crash after the fenced terminal page commit but before the driver's
    separate coverage-finalization call.  Durable SUCCESS_EMPTY + no cursor /
    continuation/child is enough to retry COMPLETE through the S3.8 barrier;
    only after that succeeds may S3.9 close the group/run as SATISFIED."""
    run_id, plan_id, request_id = _run_with_request(db)
    coverage_id, _ = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan_id,
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        scope_key="full-source",
        generation_key="terminal-crash",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        listing_identity_sufficient=True,
        now=NOW,
    )
    record_contributing_request(db.conn, coverage_id, request_id)
    claim = claim_next_request(db.conn, "worker", now=NOW)
    assert claim is not None and claim.request_id == request_id

    def persist_terminal_parse(conn):
        conn.execute(
            "INSERT INTO parse_attempts"
            " (id,attempt_id,request_id,parser_kind,parser_version,outcome_kind,"
            " observation_count,child_task_count,parsed_at,validated_page_class,"
            " continuation_required)"
            " VALUES ('pa-terminal',?,?, 'json_api_feed','1.0.0','SUCCESS_EMPTY',"
            " 0,0,?,'EMPTY',0)",
            (claim.attempt_id, request_id, NOW),
        )

    with fenced_commit(
        db.conn,
        request_id,
        claim.attempt_id,
        now=NOW,
        mutate=persist_terminal_parse,
    ):
        pass
    assert db.conn.execute(
        "SELECT finalized_at FROM enumeration_coverage WHERE id=?", (coverage_id,)
    ).fetchone()[0] is None

    begin_service_epoch(db.conn, now=LATER)
    first = recover_startup_state(db.conn, now=LATER)
    second = recover_startup_state(db.conn, now=LATER)

    assert first["repaired_coverages"] == [
        {"coverage_id": coverage_id, "state": "COMPLETE"}
    ]
    assert first["repaired_plan_outcomes"] == [
        {"plan_id": plan_id, "outcome": "SATISFIED"}
    ]
    coverage = db.conn.execute(
        "SELECT completion_state,terminal_enumeration_proven,finalized_at"
        " FROM enumeration_coverage WHERE id=?",
        (coverage_id,),
    ).fetchone()
    assert coverage["completion_state"] == "COMPLETE"
    assert coverage["terminal_enumeration_proven"] == 1
    assert coverage["finalized_at"] is not None
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE id=?", (plan_id,)
    ).fetchone()["group_outcome"] == "SATISFIED"
    assert db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id=?", (run_id,)
    ).fetchone()["status"] == "SUCCEEDED"
    assert second["repaired_coverages"] == []
    assert second["repaired_plan_outcomes"] == []




def test_s312_old_failed_attempt_does_not_poison_later_terminal_success(
    db: Database,
):
    """Degradation belongs to the attempt that produced it, not forever to the
    logical request.  A retry can legitimately supersede an earlier local/
    transport failure and prove terminal enumeration on a later SUCCEEDED
    attempt. Recovery must inspect degradation evidence for that successful
    attempt only."""
    run_id, plan_id, request_id = _run_with_request(db)
    coverage_id, _ = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan_id,
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        scope_key="full-source",
        generation_key="retry-then-terminal",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        listing_identity_sufficient=True,
        now=NOW,
    )
    record_contributing_request(db.conn, coverage_id, request_id)

    first = claim_next_request(db.conn, "worker", now=NOW)
    assert first is not None and first.request_id == request_id

    def persist_old_failure(conn):
        conn.execute(
            "INSERT INTO acquisition_evidence"
            " (id,request_id,attempt_id,kind,ref,detail_json,observed_at,created_at)"
            " VALUES ('ev-old-failure',?,?,'FAILURE','DISPATCH_EXECUTOR_ERROR',"
            " '{}',?,?)",
            (request_id, first.attempt_id, NOW, NOW),
        )

    with fenced_commit(
        db.conn,
        request_id,
        first.attempt_id,
        now=NOW,
        outcome="RETRY_WAIT",
        retry_delay_s=0.0,
        failure_kind="WORKER_CRASH",
        mutate=persist_old_failure,
    ):
        pass

    second = claim_next_request(db.conn, "worker", now=NOW)
    assert second is not None and second.request_id == request_id

    def persist_terminal_retry(conn):
        conn.execute(
            "INSERT INTO parse_attempts"
            " (id,attempt_id,request_id,parser_kind,parser_version,outcome_kind,"
            " observation_count,child_task_count,parsed_at,validated_page_class,"
            " continuation_required)"
            " VALUES ('pa-retry-terminal',?,?, 'json_api_feed','1.0.0',"
            " 'SUCCESS_EMPTY',0,0,?,'EMPTY',0)",
            (second.attempt_id, request_id, NOW),
        )

    with fenced_commit(
        db.conn,
        request_id,
        second.attempt_id,
        now=NOW,
        mutate=persist_terminal_retry,
    ):
        pass

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)

    assert report["repaired_coverages"] == [
        {"coverage_id": coverage_id, "state": "COMPLETE"}
    ]
    assert report["repaired_plan_outcomes"] == [
        {"plan_id": plan_id, "outcome": "SATISFIED"}
    ]
    assert db.conn.execute(
        "SELECT completion_state FROM enumeration_coverage WHERE id=?",
        (coverage_id,),
    ).fetchone()["completion_state"] == "COMPLETE"
    assert db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id=?", (run_id,)
    ).fetchone()["status"] == "SUCCEEDED"

def test_s312_budget_stopped_success_never_repairs_false_complete(
    db: Database,
):
    """A fenced successful parse can still be non-terminal when host budget
    policy prevented its continuation. The durable budget evidence must defeat
    COMPLETE reconstruction even though the request/attempt themselves are
    SUCCEEDED and no continuation child exists."""
    run_id, plan_id, request_id = _run_with_request(db)
    coverage_id, _ = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan_id,
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        scope_key="full-source",
        generation_key="budget-stop-crash",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        listing_identity_sufficient=True,
        now=NOW,
    )
    record_contributing_request(db.conn, coverage_id, request_id)
    claim = claim_next_request(db.conn, "worker", now=NOW)
    assert claim is not None and claim.request_id == request_id

    def persist_budget_stopped_parse(conn):
        conn.execute(
            "INSERT INTO parse_attempts"
            " (id,attempt_id,request_id,parser_kind,parser_version,outcome_kind,"
            " observation_count,child_task_count,parsed_at,validated_page_class,"
            " continuation_required)"
            " VALUES ('pa-budget',?,?, 'json_api_feed','1.0.0','SUCCESS_WITH_JOBS',"
            " 1,0,?,'VALID_LIST',0)",
            (claim.attempt_id, request_id, NOW),
        )
        conn.execute(
            "INSERT INTO job_observations"
            " (id,run_id,request_id,attempt_id,source_id,binding_id,adapter_id,"
            " adapter_version,strategy,execution_class,source_job_id,observed_at,"
            " observation_unique_key)"
            " VALUES ('obs-budget',?,?,?,?,?,'json_api_feed','1.0.0',"
            " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','budget-job',?,"
            " 'budget-job')",
            (run_id, request_id, claim.attempt_id, "src-1", "bnd-1", NOW),
        )
        conn.execute(
            "INSERT INTO acquisition_evidence"
            " (id,request_id,attempt_id,kind,ref,detail_json,observed_at,created_at)"
            " VALUES ('ev-budget',?,?,'REVIEW','crawler://BUDGET_EXHAUSTED',"
            " '{}',?,?)",
            (request_id, claim.attempt_id, NOW, NOW),
        )
        # This is the same durable degradation the driver records under the
        # fence for an authoritative generation when continuation is blocked.
        conn.execute(
            "UPDATE enumeration_coverage"
            " SET absence_inference_allowed=0,stop_reason='crawler budget exhausted'"
            " WHERE id=?",
            (coverage_id,),
        )

    with fenced_commit(
        db.conn,
        request_id,
        claim.attempt_id,
        now=NOW,
        mutate=persist_budget_stopped_parse,
    ):
        pass

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)

    assert report["repaired_coverages"] == [
        {"coverage_id": coverage_id, "state": "PARTIAL"}
    ]
    coverage = db.conn.execute(
        "SELECT completion_state,terminal_enumeration_proven,"
        " absence_inference_allowed FROM enumeration_coverage WHERE id=?",
        (coverage_id,),
    ).fetchone()
    assert tuple(coverage) == ("PARTIAL", 0, 0)
    assert report["repaired_plan_outcomes"] == [
        {"plan_id": plan_id, "outcome": "SATISFIED_PARTIAL"}
    ]
    assert db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id=?", (run_id,)
    ).fetchone()["status"] == "PARTIAL"


def test_s312_failed_fetch_finalization_repairs_visible_partial_not_fallback(
    db: Database,
):
    """A fenced provider/network result can be useful durable evidence even
    when its request ends FAILED.  Recovery must reconstruct the driver's
    visible SATISFIED_PARTIAL result, not silently treat it like a pre-dispatch
    refusal and activate fallback."""
    run_id, plan_ids = create_run(
        db.conn,
        profile_id=None,
        plans=[_plan(rank=0), _plan(rank=1)],
        now=NOW,
    )
    coverage_id, _ = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        scope_key="full-source",
        generation_key="failed-fetch",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        listing_identity_sufficient=True,
        now=NOW,
    )
    request_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?failed-fetch=1",
        logical_key="failed-fetch",
        now=NOW,
    )
    record_contributing_request(db.conn, coverage_id, request_id)
    claim = claim_next_request(
        db.conn, "worker", now=NOW, run_source_plan_id=plan_ids[0]
    )
    assert claim is not None

    def persist_failed_fetch(conn):
        conn.execute(
            "INSERT INTO fetch_attempts"
            " (id,attempt_id,request_id,requested_url,failure_kind,fetched_at)"
            " VALUES ('fa-failed',?,?,?,'HTTP_5XX',?)",
            (claim.attempt_id, request_id, "https://example.test/jobs", NOW),
        )

    with fenced_commit(
        db.conn,
        request_id,
        claim.attempt_id,
        now=NOW,
        outcome="FAILED",
        failure_kind="HTTP_5XX",
        mutate=persist_failed_fetch,
    ):
        pass

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)

    assert report["repaired_coverages"] == [
        {"coverage_id": coverage_id, "state": "PARTIAL"}
    ]
    assert report["repaired_plan_outcomes"] == [
        {"plan_id": plan_ids[0], "outcome": "SATISFIED_PARTIAL"}
    ]
    group = db.conn.execute(
        "SELECT active_fallback_rank,group_outcome FROM source_plan_group_state"
        " WHERE run_id=? AND source_plan_group_id='grp-1'",
        (run_id,),
    ).fetchone()
    assert tuple(group) == (0, "SATISFIED_PARTIAL")
    assert db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE id=?", (plan_ids[1],)
    ).fetchone()["group_outcome"] == "SKIPPED_NOT_NEEDED"



def test_s312_metadata_dependency_does_not_turn_predispatch_failure_into_partial(
    db: Database,
):
    """A host-owned ROBOTS/SITEMAP dependency is not accepted provider/page
    evidence for its parent coverage contributor.  If the parent itself fails
    before dispatch, restart may still activate the declared fallback; metadata
    history must not manufacture SATISFIED_PARTIAL."""
    run_id, plan_ids = create_run(
        db.conn,
        profile_id=None,
        plans=[_plan(rank=0), _plan(rank=1)],
        now=NOW,
    )
    coverage_id, _ = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        scope_key="full-source",
        generation_key="predispatch-with-metadata",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        listing_identity_sufficient=True,
        now=NOW,
    )
    parent_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="SOURCE_CRAWL",
        target_identity="https://example.test/jobs",
        logical_key="page-parent",
        payload={"role": "PAGE"},
        now=NOW,
    )
    record_contributing_request(db.conn, coverage_id, parent_id)
    parent_claim = claim_next_request(
        db.conn, "worker", now=NOW, run_source_plan_id=plan_ids[0]
    )
    assert parent_claim is not None and parent_claim.request_id == parent_id
    with fenced_commit(
        db.conn,
        parent_id,
        parent_claim.attempt_id,
        now=NOW,
        outcome="FAILED",
        failure_kind="POLICY_REJECTED",
    ):
        pass

    metadata_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="SOURCE_CRAWL",
        target_identity="https://example.test/robots.txt",
        logical_key="robots-dependency",
        payload={"role": "ROBOTS"},
        parent_request_id=parent_id,
        now=NOW,
    )
    metadata_claim = claim_next_request(
        db.conn, "worker", now=NOW, run_source_plan_id=plan_ids[0]
    )
    assert metadata_claim is not None and metadata_claim.request_id == metadata_id
    with fenced_commit(
        db.conn, metadata_id, metadata_claim.attempt_id, now=NOW
    ):
        pass

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)

    assert report["repaired_coverages"] == [
        {"coverage_id": coverage_id, "state": "FAILED"}
    ]
    assert report["repaired_plan_outcomes"] == [
        {"plan_id": plan_ids[0], "outcome": "FAILED"}
    ]
    group = db.conn.execute(
        "SELECT active_fallback_rank,group_outcome FROM source_plan_group_state"
        " WHERE run_id=? AND source_plan_group_id='grp-1'",
        (run_id,),
    ).fetchone()
    assert tuple(group) == (1, None)

def test_s312_pagination_loop_terminal_failure_is_not_resurrected(
    db: Database,
):
    """A proven pagination loop is a durable terminal failure for that
    request/binding revision. Startup recovery must not turn it back into
    source-network work."""
    _run_id, _plan_id, request_id = _run_with_request(db)
    claim = claim_next_request(db.conn, "worker", now=NOW)
    assert claim is not None
    with fenced_commit(
        db.conn,
        request_id,
        claim.attempt_id,
        now=NOW,
        outcome="FAILED",
        failure_kind="PAGINATION_LOOP",
        failure_json='{"reason":"repeated cursor"}',
    ):
        pass

    before = db.conn.execute(
        "SELECT status,last_failure_kind,last_failure_json,attempt_count"
        " FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    assert before["status"] == "FAILED"
    assert before["last_failure_kind"] == "PAGINATION_LOOP"

    begin_service_epoch(db.conn, now=LATER)
    recover_startup_state(db.conn, now=LATER)

    after = db.conn.execute(
        "SELECT status,last_failure_kind,last_failure_json,attempt_count"
        " FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    assert tuple(after) == tuple(before)


def test_s312_adapter_promotion_does_not_repin_historical_run(
    db: Database,
):
    """An old run remains bound to its immutable historical binding
    revision across restart even when mutable current authority is promoted.
    Cursor compatibility can therefore resolve against the run's old pins
    instead of silently substituting newer parser code."""
    run_id, plan_ids = create_run(
        db.conn, profile_id=None, plans=[_plan()], now=NOW
    )
    old_plan = db.conn.execute(
        "SELECT * FROM run_source_plans WHERE id=?", (plan_ids[0],)
    ).fetchone()
    old_guard = PaginationGuardState(
        consecutive_no_new_jobs_pages=1,
        seen_url_identities=("https://example.test/jobs?page=1",),
    )
    save_cursor(
        db.conn,
        run_source_plan_id=plan_ids[0],
        plan_row=old_plan,
        cursor=CrawlCursor(
            source_id="src-1",
            binding_id="bnd-1",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            cursor_schema_version=1,
            state_json='{"page": 2}',
            checkpoint_at=NOW,
        ),
        guard_state=old_guard,
        now=NOW,
    )
    db.conn.commit()
    db.conn.execute(
        "INSERT INTO adapter_definitions"
        " (adapter_id,adapter_version,adapter_api_version,manifest_json,created_at)"
        " VALUES ('json_api_feed','2.0.0','1','{}',?)",
        (LATER,),
    )
    db.conn.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id,binding_id,revision,adapter_id,adapter_version,strategy,"
        " execution_class,permission_profile_id,permission_profile_revision,"
        " config_json,created_at)"
        " VALUES ('bndrev-2','bnd-1',2,'json_api_feed','2.0.0',"
        " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?,?)",
        (FEED_CONFIG_JSON, LATER),
    )
    db.conn.execute(
        "UPDATE source_adapter_binding_revisions SET superseded_at=?"
        " WHERE id='bndrev-1'",
        (LATER,),
    )
    db.conn.execute(
        "UPDATE source_adapter_bindings SET current_revision_id='bndrev-2'"
        " WHERE id='bnd-1'"
    )
    db.conn.commit()
    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?page=2",
        logical_key="historical-resume-page-2",
        now=NOW,
    )

    begin_service_epoch(db.conn, now=LATER)
    report = recover_startup_state(db.conn, now=LATER)
    assert report["validated_resumable_plans"] == [plan_ids[0]]

    pinned = db.conn.execute(
        "SELECT binding_revision_id,adapter_id,adapter_version"
        " FROM run_source_plans WHERE id=?",
        (plan_ids[0],),
    ).fetchone()
    assert tuple(pinned) == ("bndrev-1", "json_api_feed", "1.0.0")
    reloaded_plan = db.conn.execute(
        "SELECT * FROM run_source_plans WHERE id=?", (plan_ids[0],)
    ).fetchone()
    loaded = load_cursor(
        db.conn, run_source_plan_id=plan_ids[0], plan_row=reloaded_plan
    )
    assert loaded is not None
    assert loaded.cursor.state_json == '{"page": 2}'
    assert loaded.guard_state == old_guard
    historical = db.conn.execute(
        "SELECT superseded_at FROM source_adapter_binding_revisions"
        " WHERE id='bndrev-1'"
    ).fetchone()
    assert historical is not None and historical["superseded_at"] == LATER


def test_s312_incompatible_pinned_adapter_fails_recovery_explicitly(
    db: Database,
):
    """A resumable run may never be silently executed by a different installed
    adapter version.  The historical revision stays resolvable as data, but
    startup fails explicitly until a compatible implementation/migration exists."""
    db.conn.execute(
        "INSERT INTO adapter_definitions"
        " (adapter_id,adapter_version,adapter_api_version,manifest_json,created_at)"
        " VALUES ('json_api_feed','9.9.9','1','{}',?)",
        (NOW,),
    )
    db.conn.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id,binding_id,revision,adapter_id,adapter_version,strategy,"
        " execution_class,permission_profile_id,permission_profile_revision,"
        " config_json,created_at)"
        " VALUES ('bndrev-bad','bnd-1',99,'json_api_feed','9.9.9',"
        " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?,?)",
        (FEED_CONFIG_JSON, NOW),
    )
    db.conn.commit()
    bad = _plan()
    bad["binding_revision_id"] = "bndrev-bad"
    bad["adapter_version"] = "9.9.9"
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=[bad], now=NOW)
    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?page=1",
        logical_key="incompatible-pinned-adapter",
        now=NOW,
    )

    begin_service_epoch(db.conn, now=LATER)
    with pytest.raises(RuntimeError, match="pinned adapter implementation incompatible"):
        recover_startup_state(db.conn, now=LATER)



def test_s312_incompatible_legacy_cursor_fails_recovery_explicitly(
    db: Database,
):
    """A historical pre-v16 cursor that cannot be bound to the immutable
    binding revision is preserved as evidence but may not be silently consumed
    by a resumable run. Startup must surface the cursor-compatibility refusal."""
    run_id, plan_ids = create_run(
        db.conn, profile_id=None, plans=[_plan()], now=NOW
    )
    enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?page=2",
        logical_key="legacy-cursor-resume",
        now=NOW,
    )
    db.conn.execute(
        "INSERT INTO crawl_cursors"
        " (id,source_id,binding_id,binding_revision_id,adapter_id,adapter_version,"
        " cursor_schema_version,state_json,guard_state_json,"
        " checkpoint_run_source_plan_id,checkpoint_at)"
        " VALUES ('cur-legacy','src-1','bnd-1',NULL,'json_api_feed','1.0.0',1,"
        " '{\"page\":2}','{}',NULL,?)",
        (NOW,),
    )
    db.conn.commit()

    begin_service_epoch(db.conn, now=LATER)
    with pytest.raises(RuntimeError, match="pinned cursor incompatible"):
        recover_startup_state(db.conn, now=LATER)

    # Recovery refusal is diagnostic only: the historical cursor is preserved
    # and the durable request remains open rather than being silently reset.
    assert db.conn.execute(
        "SELECT binding_revision_id,state_json FROM crawl_cursors"
        " WHERE id='cur-legacy'"
    ).fetchone()["binding_revision_id"] is None
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE run_id=?", (run_id,)
    ).fetchone()["status"] == "PENDING"


def test_s312_requires_fresh_service_epoch(tmp_path):
    database = Database(tmp_path / "no-epoch.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    _authority_rows(database.conn)
    try:
        with pytest.raises(NoActiveServiceEpoch):
            recover_startup_state(database.conn, now=NOW)
    finally:
        database.close()


def test_s312_runner_orders_recovery_before_coordinator_and_listener():
    """The service integration ordering itself is part of RUN-19."""
    from jobscraper.service.runner import run_service

    source = inspect.getsource(run_service)
    order = [
        source.index("db = open_service_database(config)"),
        source.index("service_epoch = begin_service_epoch(db.conn)"),
        source.index("recovered = recover_startup_state(db.conn)"),
        source.index("configure_service_capacity(config)"),
        source.index("sock = _bind_loopback_socket(config.loopback_host)"),
        source.index("app, state = create_service_app"),
        source.index("await lifespan.start_background()"),
        source.index("await server.serve"),
    ]
    assert order == sorted(order)
