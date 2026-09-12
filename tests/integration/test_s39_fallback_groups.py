"""S3.9 durable logical fallback-group orchestration.

Covers RUN-01/RUN-02 S3.9 authority: deterministic active rank, exactly-once
fallback advancement across restart/duplicate reports, successful fallback
satisfaction, unused-rank skipping, visible partial, pending-work finalization
barrier, and generic claim-path refusal of dormant acquisition ranks.
"""

from __future__ import annotations

import sqlite3
import threading

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.clock import begin_service_epoch
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import (
    RunStateConflict,
    aggregate_run,
    create_run,
    plan_is_active,
    set_group_outcome,
)

NOW = "2026-09-11T18:00:00.000000Z"
LATER = "2026-09-11T18:01:00.000000Z"


def _authority_rows(db: Database) -> None:
    c = db.conn
    c.execute(
        "INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)"
        " VALUES ('src','Fallback fixture','PUBLIC_FEED','https://example.test/jobs',?,?)",
        (NOW, NOW),
    )
    for adapter in ("feed-primary", "feed-fallback", "feed-last"):
        c.execute(
            "INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version,"
            " manifest_json, created_at) VALUES (?, '1.0.0', '1', '{}', ?)",
            (adapter, NOW),
        )
    c.execute(
        "INSERT INTO adapter_permission_profiles (id, display_name, created_at)"
        " VALUES ('perm','default',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO adapter_permission_profile_revisions"
        " (id, permission_profile_id, revision, policy_json, created_at)"
        " VALUES ('permrev','perm',1,'{}',?)",
        (NOW,),
    )
    strategies = (
        "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        "HTTP_HTML",
        "STRUCTURED_PAGE",
    )
    for rank, adapter in enumerate(("feed-primary", "feed-fallback", "feed-last")):
        binding = f"bnd-{rank}"
        revision = f"bndrev-{rank}"
        c.execute(
            "INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)"
            " VALUES (?, 'src', ?, ?)",
            (binding, adapter, NOW),
        )
        c.execute(
            "INSERT INTO source_adapter_binding_revisions"
            " (id, binding_id, revision, adapter_id, adapter_version, strategy,"
            " execution_class, permission_profile_id, permission_profile_revision, created_at)"
            " VALUES (?, ?, 1, ?, '1.0.0', ?, 'HTTP', 'perm', 1, ?)",
            (revision, binding, adapter, strategies[rank], NOW),
        )
    c.commit()


def _plans(count: int = 3):
    return [
        dict(
            source_id="src",
            source_plan_group_id="grp-src",
            fallback_rank=rank,
            binding_id=f"bnd-{rank}",
            binding_revision_id=f"bndrev-{rank}",
            adapter_id=("feed-primary", "feed-fallback", "feed-last")[rank],
            adapter_version="1.0.0",
            adapter_api_version="1",
            strategy=("FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", "HTTP_HTML", "STRUCTURED_PAGE")[rank],
            execution_class="HTTP",
            permission_profile_id="perm",
            permission_profile_revision=1,
        )
        for rank in range(count)
    ]


def _db(path) -> Database:
    db = Database(path)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    _authority_rows(db)
    begin_service_epoch(db.conn, now=NOW)
    return db


def _group(db: Database, run_id: str):
    return db.conn.execute(
        "SELECT * FROM source_plan_group_state WHERE run_id=? AND source_plan_group_id='grp-src'",
        (run_id,),
    ).fetchone()


def _outcomes(db: Database, run_id: str):
    return [
        row["group_outcome"]
        for row in db.conn.execute(
            "SELECT group_outcome FROM run_source_plans WHERE run_id=? ORDER BY fallback_rank",
            (run_id,),
        ).fetchall()
    ]


def test_only_rank_zero_is_initially_active_and_pins_stay_immutable(tmp_path):
    db = _db(tmp_path / "initial.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(), now=NOW)
    assert plan_is_active(db.conn, plan_ids[0])
    assert not plan_is_active(db.conn, plan_ids[1])
    assert _group(db, run_id)["active_fallback_rank"] == 0

    # Promotion/retirement of mutable binding authority cannot rewrite the run pins.
    db.conn.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id,binding_id,revision,adapter_id,adapter_version,strategy,execution_class,"
        " permission_profile_id,permission_profile_revision,created_at)"
        " VALUES ('bndrev-0-v2','bnd-0',2,'feed-primary','1.0.0','PROVIDER_NATIVE',"
        " 'HTTP','perm',1,?)",
        (LATER,),
    )
    db.conn.execute(
        "UPDATE source_adapter_binding_revisions SET superseded_at=? WHERE id='bndrev-0'",
        (LATER,),
    )
    db.conn.execute(
        "UPDATE source_adapter_bindings SET current_revision_id='bndrev-0-v2' WHERE id='bnd-0'"
    )
    db.conn.commit()
    pinned = db.conn.execute(
        "SELECT fallback_rank,binding_revision_id FROM run_source_plans"
        " WHERE run_id=? ORDER BY fallback_rank",
        (run_id,),
    ).fetchall()
    assert [(r["fallback_rank"], r["binding_revision_id"]) for r in pinned] == [
        (0, "bndrev-0"), (1, "bndrev-1"), (2, "bndrev-2")
    ]
    historical = db.conn.execute(
        "SELECT id,superseded_at FROM source_adapter_binding_revisions WHERE id='bndrev-0'"
    ).fetchone()
    assert historical is not None and historical["superseded_at"] == LATER
    db.close()


def test_failed_rank_activates_next_once_and_restart_reproduces_same_rank(tmp_path):
    path = tmp_path / "restart.db"
    db = _db(path)
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    assert _group(db, run_id)["active_fallback_rank"] == 1

    # Duplicate report cannot skip rank 1.
    set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    assert _group(db, run_id)["active_fallback_rank"] == 1
    db.close()

    reopened = Database(path)
    assert plan_is_active(reopened.conn, plan_ids[1])
    assert not plan_is_active(reopened.conn, plan_ids[2])
    reopened.close()


def test_successful_fallback_satisfies_group_and_skips_later_rank(tmp_path):
    db = _db(tmp_path / "success.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    set_group_outcome(db.conn, plan_ids[1], "SATISFIED", now=LATER)

    assert _group(db, run_id)["group_outcome"] == "SATISFIED"
    assert _outcomes(db, run_id) == ["FAILED", "SATISFIED", "SKIPPED_NOT_NEEDED"]
    assert aggregate_run(db.conn, run_id, now=LATER) == "SUCCEEDED"
    db.close()


def test_partial_is_visible_terminal_group_not_silent_fallback(tmp_path):
    db = _db(tmp_path / "partial.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED_PARTIAL", now=LATER)

    assert _group(db, run_id)["group_outcome"] == "SATISFIED_PARTIAL"
    assert _outcomes(db, run_id) == [
        "SATISFIED_PARTIAL", "SKIPPED_NOT_NEEDED", "SKIPPED_NOT_NEEDED"
    ]
    assert aggregate_run(db.conn, run_id, now=LATER) == "PARTIAL"
    db.close()


def test_active_plan_cannot_close_while_owned_work_is_open(tmp_path):
    db = _db(tmp_path / "barrier.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    request_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src",
        binding_id="bnd-0",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs",
        now=NOW,
    )
    try:
        set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    except RunStateConflict:
        pass
    else:  # pragma: no cover - regression failure path
        raise AssertionError("open acquisition work incorrectly allowed fallback advancement")
    assert _group(db, run_id)["active_fallback_rank"] == 0

    db.conn.execute(
        "UPDATE scrape_requests SET status='FAILED', finished_at=?, updated_at=? WHERE id=?",
        (LATER, LATER, request_id),
    )
    db.conn.commit()
    set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    assert _group(db, run_id)["active_fallback_rank"] == 1
    db.close()


def test_generic_claim_path_cannot_claim_dormant_fallback(tmp_path):
    db = _db(tmp_path / "claim.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    rank0_req, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src", binding_id="bnd-0", request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?rank=0", priority=0,
        logical_key="rank0", now=NOW,
    )
    rank1_req, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[1],
        source_id="src", binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?rank=1", priority=100,
        logical_key="rank1", now=NOW,
    )

    first = claim_next_request(db.conn, "worker", now=NOW)
    assert first is not None and first.request_id == rank0_req
    assert first.request_id != rank1_req
    db.close()


def test_run_finalization_waits_for_relevant_local_processing(tmp_path):
    db = _db(tmp_path / "local.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(1), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=LATER)
    db.conn.execute(
        "INSERT INTO scrape_requests"
        " (id,run_id,run_source_plan_id,source_id,binding_id,request_type,request_unique_key,"
        " status,created_at,updated_at)"
        " VALUES ('local',?,?, 'src','bnd-0','ELIGIBILITY','local-key','PENDING',?,?)",
        (run_id, plan_ids[0], NOW, NOW),
    )
    db.conn.commit()
    assert aggregate_run(db.conn, run_id, now=LATER) is None
    db.conn.execute(
        "UPDATE scrape_requests SET status='SUCCEEDED',finished_at=?,updated_at=? WHERE id='local'",
        (LATER, LATER),
    )
    db.conn.commit()
    assert aggregate_run(db.conn, run_id, now=LATER) == "SUCCEEDED"
    db.close()



def test_primary_success_is_zero_job_success_and_unused_fallback_is_not_failure(tmp_path):
    db = _db(tmp_path / "zero.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    # No observation/job rows exist: a complete valid zero-job result is still SATISFIED.
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=LATER)
    assert _outcomes(db, run_id) == ["SATISFIED", "SKIPPED_NOT_NEEDED"]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_observations WHERE run_id=?", (run_id,)
    ).fetchone()[0] == 0
    assert aggregate_run(db.conn, run_id, now=LATER) == "SUCCEEDED"
    db.close()


def test_all_fallbacks_failed_is_failed(tmp_path):
    db = _db(tmp_path / "all-fail.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    set_group_outcome(db.conn, plan_ids[1], "FAILED", now=LATER)
    assert _group(db, run_id)["group_outcome"] == "FAILED"
    assert _outcomes(db, run_id) == ["FAILED", "FAILED"]
    assert aggregate_run(db.conn, run_id, now=LATER) == "FAILED"
    db.close()


def test_policy_denied_can_fallback_but_mixed_exhaustion_is_failed(tmp_path):
    db = _db(tmp_path / "policy.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "POLICY_DENIED", now=LATER)
    assert _group(db, run_id)["active_fallback_rank"] == 1
    set_group_outcome(db.conn, plan_ids[1], "FAILED", now=LATER)
    assert _group(db, run_id)["group_outcome"] == "FAILED"
    assert aggregate_run(db.conn, run_id, now=LATER) == "FAILED"
    db.close()


def test_duplicate_activation_race_does_not_skip_a_rank(tmp_path):
    path = tmp_path / "race.db"
    db = _db(path)
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(), now=NOW)
    db.close()

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def finish_primary():
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            barrier.wait(timeout=10)
            set_group_outcome(conn, plan_ids[0], "FAILED", now=LATER)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=finish_primary) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors

    reopened = Database(path)
    assert _group(reopened, run_id)["active_fallback_rank"] == 1
    assert _outcomes(reopened, run_id) == ["FAILED", None, None]
    reopened.close()


def test_partial_group_plus_successful_group_aggregates_partial(tmp_path):
    db = _db(tmp_path / "partial-mix.db")
    plans = _plans(1)
    second = dict(plans[0])
    second.update(
        source_plan_group_id="grp-other",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="feed-fallback",
        strategy="HTTP_HTML",
    )
    run_id, plan_ids = create_run(
        db.conn, profile_id=None, plans=[plans[0], second], now=NOW
    )
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED_PARTIAL", now=LATER)
    set_group_outcome(db.conn, plan_ids[1], "SATISFIED", now=LATER)
    assert aggregate_run(db.conn, run_id, now=LATER) == "PARTIAL"
    db.close()


def test_cancellation_preserves_completed_group_and_cancels_only_open_group(tmp_path):
    db = _db(tmp_path / "cancel.db")
    first = _plans(1)[0]
    second = dict(first)
    second.update(
        source_plan_group_id="grp-other",
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="feed-fallback",
        strategy="HTTP_HTML",
    )
    run_id, plan_ids = create_run(
        db.conn, profile_id=None, plans=[first, second], now=NOW
    )
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=LATER)
    request_run_cancellation(db.conn, run_id, now=LATER)

    rows = db.conn.execute(
        "SELECT source_plan_group_id,group_outcome FROM source_plan_group_state"
        " WHERE run_id=? ORDER BY source_plan_group_id",
        (run_id,),
    ).fetchall()
    assert {row["source_plan_group_id"]: row["group_outcome"] for row in rows} == {
        "grp-other": "CANCELLED",
        "grp-src": "SATISFIED",
    }
    assert aggregate_run(db.conn, run_id, now=LATER) == "CANCELLED"
    db.close()


def test_v19_backfill_closes_legacy_null_rank_around_later_success_without_stranding_local_work(tmp_path):
    """Pre-S3.9 could run a later fallback while an earlier rank stayed open.

    v19 must adopt the durable success, mark the ambiguous NULL rank unused,
    ignore its now-irrelevant acquisition work, but still drain any accepted
    host-native processing created before migration.
    """
    db = Database(tmp_path / "legacy-v18.db")
    migrate_schema(db.conn, 18)
    _authority_rows(db)
    c = db.conn
    c.execute(
        "INSERT INTO scrape_runs(id,status,created_at) VALUES ('legacy','RUNNING',?)",
        (NOW,),
    )
    for rank, outcome in ((0, None), (1, "SATISFIED")):
        c.execute(
            "INSERT INTO run_source_plans"
            " (id,run_id,source_id,source_plan_group_id,fallback_rank,binding_id,"
            " binding_revision_id,adapter_id,adapter_version,adapter_api_version,strategy,"
            " execution_class,permission_profile_id,permission_profile_revision,group_outcome,created_at)"
            " VALUES (?,?, 'src','grp-src',?,?,?,?,'1.0.0','1',?,'HTTP','perm',1,?,?)",
            (
                f"legacy-plan-{rank}",
                "legacy",
                rank,
                f"bnd-{rank}",
                f"bndrev-{rank}",
                ("feed-primary", "feed-fallback")[rank],
                ("FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", "HTTP_HTML")[rank],
                outcome,
                NOW,
            ),
        )
    c.execute(
        "INSERT INTO scrape_requests"
        " (id,run_id,run_source_plan_id,source_id,binding_id,request_type,request_unique_key,"
        " status,created_at,updated_at)"
        " VALUES ('legacy-acq','legacy','legacy-plan-0','src','bnd-0','LIST_FETCH',"
        " 'legacy-acq-key','PENDING',?,?)",
        (NOW, NOW),
    )
    c.execute(
        "INSERT INTO scrape_requests"
        " (id,run_id,run_source_plan_id,source_id,binding_id,request_type,request_unique_key,"
        " status,created_at,updated_at)"
        " VALUES ('legacy-local','legacy','legacy-plan-0','src','bnd-0','ELIGIBILITY',"
        " 'legacy-local-key','PENDING',?,?)",
        (NOW, NOW),
    )
    c.commit()

    migrate_schema(c, 19)
    state = c.execute(
        "SELECT active_fallback_rank,group_outcome FROM source_plan_group_state"
        " WHERE run_id='legacy' AND source_plan_group_id='grp-src'"
    ).fetchone()
    assert tuple(state) == (1, "SATISFIED")
    assert c.execute(
        "SELECT group_outcome FROM run_source_plans WHERE id='legacy-plan-0'"
    ).fetchone()[0] == "SKIPPED_NOT_NEEDED"

    # The skipped rank's acquisition request is no longer relevant collection
    # work, but accepted host-native processing must still block finalization.
    assert aggregate_run(c, "legacy", now=LATER) is None
    c.execute(
        "UPDATE scrape_requests SET status='SUCCEEDED',finished_at=?,updated_at=?"
        " WHERE id='legacy-local'",
        (LATER, LATER),
    )
    c.commit()
    assert aggregate_run(c, "legacy", now=LATER) == "SUCCEEDED"
    db.close()


def test_cancellation_after_collection_success_still_finishes_cancelled_after_local_drain(tmp_path):
    db = _db(tmp_path / "cancel-after-collection.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(1), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=LATER)
    db.conn.execute(
        "INSERT INTO scrape_requests"
        " (id,run_id,run_source_plan_id,source_id,binding_id,request_type,request_unique_key,"
        " status,created_at,updated_at)"
        " VALUES ('local-after-collection',?,?, 'src','bnd-0','ELIGIBILITY',"
        " 'local-after-collection-key','PENDING',?,?)",
        (run_id, plan_ids[0], NOW, NOW),
    )
    db.conn.commit()

    request_run_cancellation(db.conn, run_id, now=LATER)
    assert _group(db, run_id)["group_outcome"] == "SATISFIED"
    assert aggregate_run(db.conn, run_id, now=LATER) is None

    db.conn.execute(
        "UPDATE scrape_requests SET status='SUCCEEDED',finished_at=?,updated_at=?"
        " WHERE id='local-after-collection'",
        (LATER, LATER),
    )
    db.conn.commit()
    assert aggregate_run(db.conn, run_id, now=LATER) == "CANCELLED"
    db.close()


def test_group_active_rank_is_foreign_key_bound_to_a_real_pinned_plan(tmp_path):
    db = _db(tmp_path / "group-fk.db")
    run_id, _ = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    try:
        db.conn.execute(
            "UPDATE source_plan_group_state SET active_fallback_rank=99"
            " WHERE run_id=? AND source_plan_group_id='grp-src'",
            (run_id,),
        )
        db.conn.commit()
    except sqlite3.IntegrityError:
        db.conn.rollback()
    else:  # pragma: no cover - regression failure path
        raise AssertionError("group state accepted an active rank with no pinned plan")
    db.close()


FAR_FUTURE = "2026-09-11T20:00:00.000000Z"


def _acquisition_retry(db: Database, run_id: str, plan_id: str, name: str, *, next_retry_at: str) -> None:
    db.conn.execute(
        "INSERT INTO scrape_requests"
        " (id,run_id,run_source_plan_id,source_id,binding_id,request_type,request_unique_key,"
        " status,next_retry_at,created_at,updated_at)"
        " VALUES (?,?,?, 'src','bnd-0','LIST_FETCH',?,'RETRY_WAIT',?,?,?)",
        (name, run_id, plan_id, f"{name}-key", next_retry_at, NOW, NOW),
    )
    db.conn.commit()


def test_future_retry_blocks_plan_and_run_terminalization(tmp_path):
    """A not-yet-due retry is not claimable yet, but it remains accepted
    durable work and therefore blocks terminal plan/run truth until resolved."""
    db = _db(tmp_path / "dormant.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(1), now=NOW)
    _acquisition_retry(db, run_id, plan_ids[0], "dormant", next_retry_at=FAR_FUTURE)
    try:
        set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    except RunStateConflict:
        pass
    else:  # pragma: no cover - regression failure path
        raise AssertionError("future retry incorrectly allowed plan terminalization")
    assert _group(db, run_id)["group_outcome"] is None
    assert aggregate_run(db.conn, run_id, now=LATER) is None
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id='dormant'"
    ).fetchone()[0] == "RETRY_WAIT"
    db.close()


def test_due_retry_still_blocks_plan_terminalization(tmp_path):
    """The C2 barrier keeps its teeth: a due retry is claimable now, so the
    active plan still cannot terminalize around it."""
    db = _db(tmp_path / "due.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(1), now=NOW)
    _acquisition_retry(db, run_id, plan_ids[0], "due", next_retry_at=NOW)
    try:
        set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    except RunStateConflict:
        pass
    else:  # pragma: no cover - regression failure path
        raise AssertionError("due retry incorrectly allowed plan terminalization")
    db.conn.execute(
        "UPDATE scrape_requests SET status='FAILED',finished_at=?,updated_at=? WHERE id='due'",
        (LATER, LATER),
    )
    db.conn.commit()
    set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    assert aggregate_run(db.conn, run_id, now=LATER) == "FAILED"
    db.close()


def test_partial_report_with_open_children_is_allowed_and_claimable(tmp_path):
    """A throttled pass honestly reports partial with PENDING children open,
    and the partial rank keeps owning those children for a later pass."""
    db = _db(tmp_path / "partial-open.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    child_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_ids[0],
        source_id="src",
        binding_id="bnd-0",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs?detail=1",
        logical_key="detail-1",
        now=NOW,
    )
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED_PARTIAL", now=LATER)
    assert _group(db, run_id)["group_outcome"] == "SATISFIED_PARTIAL"
    assert aggregate_run(db.conn, run_id, now=LATER) is None
    claimed = claim_next_request(db.conn, "worker", now=LATER)
    assert claimed is not None and claimed.request_id == child_id
    db.close()


def test_partial_upgrades_to_satisfied_once_children_drain(tmp_path):
    """Restart recovery completion: draining a partial rank's accepted
    children upgrades plan and group to SATISFIED, and the run succeeds."""
    db = _db(tmp_path / "upgrade.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED_PARTIAL", now=LATER)
    assert _group(db, run_id)["group_outcome"] == "SATISFIED_PARTIAL"
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=LATER)
    assert _outcomes(db, run_id) == ["SATISFIED", "SKIPPED_NOT_NEEDED"]
    assert _group(db, run_id)["group_outcome"] == "SATISFIED"
    assert aggregate_run(db.conn, run_id, now=LATER) == "SUCCEEDED"
    db.close()


def test_partial_downgrade_to_failed_closes_group_without_revival(tmp_path):
    """A partial rank whose remaining work then fails closes the group as
    FAILED; already-skipped later ranks are not revived into fallback."""
    db = _db(tmp_path / "downgrade.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(2), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED_PARTIAL", now=LATER)
    set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    assert _outcomes(db, run_id) == ["FAILED", "SKIPPED_NOT_NEEDED"]
    assert _group(db, run_id)["group_outcome"] == "FAILED"
    assert aggregate_run(db.conn, run_id, now=LATER) == "FAILED"
    db.close()


def test_final_verdicts_stay_write_once(tmp_path):
    """Only a partial verdict may be superseded: SATISFIED then FAILED is a
    durable-truth contradiction, not a reopen."""
    db = _db(tmp_path / "write-once.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(1), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=LATER)
    try:
        set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)
    except RunStateConflict:
        pass
    else:  # pragma: no cover - regression failure path
        raise AssertionError("final verdict incorrectly allowed a rewrite")
    assert _outcomes(db, run_id) == ["SATISFIED"]
    assert aggregate_run(db.conn, run_id, now=LATER) == "SUCCEEDED"
    db.close()


def test_crash_window_plan_diagnostic_is_repaired_from_group_truth(tmp_path):
    """A plan diagnostic lost after its group verdict committed is backfilled
    when the reported outcome agrees with durable group truth."""
    db = _db(tmp_path / "repair.db")
    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(1), now=NOW)
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=LATER)
    db.conn.execute(
        "UPDATE run_source_plans SET group_outcome = NULL WHERE id = ?",
        (plan_ids[0],),
    )
    db.conn.commit()
    set_group_outcome(db.conn, plan_ids[0], "SATISFIED", now=LATER)
    assert _outcomes(db, run_id) == ["SATISFIED"]
    assert aggregate_run(db.conn, run_id, now=LATER) == "SUCCEEDED"
    db.close()
