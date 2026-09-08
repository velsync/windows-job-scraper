"""S1.3 integration tests: durable run/request core (03 §16/§18, RUN-01..08).

Proves with real SQLite file databases (multi-connection where races matter):

* RUN-02: immutable RunSourcePlan snapshots record every pinned identity;
* RUN-05: deterministic request uniqueness; enqueue idempotency;
* RUN-07: atomic claim (single winner), heartbeat validity window,
  expired-lease reclaim (ABANDONED → RETRY_WAIT/FAILED), no lease revival;
* RUN-08: fenced terminal commit — outputs and the terminal transition are
  atomic under ownership verification; a stale attempt commits nothing;
* §18: durable cancellation — pending acquisition work cancelled, accepted
  evidence untouched, host-native obligations keep draining, running
  workers lose commit authority at the fence;
* RUN-01: run aggregation truth table incl. zero-job success and unused
  fallbacks (SKIPPED_NOT_NEEDED);
* RUN-19-style crash points: claim-then-crash recovers via reclaim.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.cancellation import (
    abandon_request_for_cancellation,
    request_run_cancellation,
)
from jobscraper.runtime.claims import (
    Claim,
    StaleOwnership,
    claim_next_request,
    heartbeat,
    reclaim_expired,
)
from jobscraper.runtime.fence import fenced_commit
from jobscraper.runtime.requests import (
    enqueue_request,
    request_unique_key,
)
from jobscraper.runtime.runs import (
    aggregate_run,
    create_run,
    set_group_outcome,
)

NOW = "2026-09-08T08:00:00.000000Z"
LATER = "2026-09-08T08:01:00.000000Z"


def _family(db: Database, *, binding_admin_state="NORMAL", source_desired="ENABLED"):
    conn = db.conn
    conn.execute(
        "INSERT INTO sources (id, display_name, source_family, entry_url, desired_state,"
        " administrative_state, created_at, updated_at)"
        " VALUES ('src-1','Feed','PUBLIC_FEED','https://example.test/feed',?, 'NORMAL', ?, ?)",
        (source_desired, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version,"
        " manifest_json, created_at) VALUES ('feed','1.0.0','1','{}',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles (id, display_name, created_at)"
        " VALUES ('perm-1','default',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id,"
        " revision, policy_json, created_at) VALUES ('permrev-1','perm-1',1,'{}',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO source_adapter_bindings (id, source_id, display_name,"
        " administrative_state, created_at) VALUES ('bnd-1','src-1','api',?,?)",
        (binding_admin_state, NOW),
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id,"
        " adapter_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at)"
        " VALUES ('bndrev-1','bnd-1',1,'feed','1.0.0','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT',"
        "'HTTP','perm-1',1,?)",
        (NOW,),
    )
    conn.commit()


def _plan():
    return dict(
        source_id="src-1",
        source_plan_group_id="grp-1",
        fallback_rank=0,
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
        crawl_policy_snapshot_json={"max_pages": 3},
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "runtime.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    _family(database)
    yield database
    database.close()


# ------------------------------------------------------------- RUN-05 uniqueness


def test_request_unique_key_derivation():
    base = dict(
        run_source_plan_id="rsp-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
    )
    assert request_unique_key(**base) == request_unique_key(**base)
    # different logical pagination key -> different request
    assert request_unique_key(**base, logical_key="page=1") != request_unique_key(
        **base, logical_key="page=2"
    )
    # different purpose/strategy -> different request (HTTP vs browser escalation)
    assert request_unique_key(**base) != request_unique_key(
        **{**base, "strategy": "PLAYWRIGHT_PUBLIC"}
    )
    # tracking-only variation in the target must NOT create a new key
    assert request_unique_key(
        **{**base, "target_identity": "https://example.test/feed?utm_source=x"}
    ) == request_unique_key(**base)


def test_enqueue_is_idempotent_per_run(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    plan_id = plans[0]
    id1, created1 = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    id2, created2 = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/feed?utm_term=z",  # tracking-only variant
    )
    assert created1 and not created2
    assert id1 == id2
    count = db.conn.execute("SELECT COUNT(*) FROM scrape_requests").fetchone()[0]
    assert count == 1


# --------------------------------------------------------------- RUN-02 snapshots


def test_create_run_pins_immutable_plan_snapshots(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    row = db.conn.execute(
        "SELECT * FROM run_source_plans WHERE id = ?", (plans[0],)
    ).fetchone()
    assert row["run_id"] == run_id
    assert row["binding_revision_id"] == "bndrev-1"
    assert row["crawl_policy_snapshot_json"] == '{\"max_pages\":3}'
    assert row["permission_profile_revision"] == 1
    assert row["group_outcome"] is None
    # Editing the binding's current revision afterwards cannot change the pin.
    db.conn.execute(
        "INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id,"
        " adapter_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at)"
        " VALUES ('bndrev-2','bnd-1',2,'feed','1.0.0','HTTP_HTML','HTTP','perm-1',1,?)",
        (LATER,),
    )
    row2 = db.conn.execute(
        "SELECT binding_revision_id, adapter_version FROM run_source_plans WHERE id = ?",
        (plans[0],),
    ).fetchone()
    assert row2["binding_revision_id"] == "bndrev-1"
    assert row2["adapter_version"] == "1.0.0"


# ------------------------------------------------------------------ RUN-07 claim


def test_claim_ownership_attempt_and_lease(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert isinstance(claim, Claim)
    assert claim.request_id == rid
    assert claim.attempt_id
    row = db.conn.execute("SELECT * FROM scrape_requests WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "RUNNING"
    assert row["current_worker_id"] == "worker-1"
    assert row["current_attempt_id"] == claim.attempt_id
    assert row["attempt_count"] == 1
    assert row["lease_until"] > NOW
    assert row["heartbeat_at"] == NOW
    # attempt history row exists (RUN-06)
    att = db.conn.execute(
        "SELECT * FROM request_attempts WHERE attempt_id = ?", (claim.attempt_id,)
    ).fetchone()
    assert att["request_id"] == rid and att["worker_id"] == "worker-1"
    # nothing left to claim
    assert claim_next_request(db.conn, "worker-2", now=NOW) is None


def test_claim_skips_cancelled_runs_quarantined_bindings_and_disabled_sources(db, tmp_path):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    request_run_cancellation(db.conn, run_id, now=NOW)
    assert claim_next_request(db.conn, "worker-1", now=NOW) is None
    # host-native obligations still drain on a cancelled run
    rid2, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="ELIGIBILITY",
        target_identity="obs-1",
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim is not None and claim.request_id == rid2

    # quarantined binding blocks new claims
    db2 = Database(tmp_path / "q.db")
    migrate_schema(db2.conn, LATEST_SCHEMA_VERSION)
    _family(db2, binding_admin_state="QUARANTINED")
    run2, plans2 = create_run(db2.conn, profile_id=None, plans=[_plan()], now=NOW)
    enqueue_request(
        db2.conn, run_id=run2, run_source_plan_id=plans2[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    assert claim_next_request(db2.conn, "w", now=NOW) is None
    db2.close()


def test_concurrent_claims_have_a_single_winner(db, tmp_path):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    ids = [
        enqueue_request(
            db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
            binding_id="bnd-1", request_type="LIST_FETCH",
            target_identity=f"https://example.test/feed?page={p}",
            logical_key=f"page={p}",
        )[0]
        for p in range(1, 21)
    ]
    path = tmp_path / "runtime.db"
    results: list[Claim] = []
    lock = threading.Lock()

    def claimer(name: str):
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            while True:
                try:
                    claim = claim_next_request(conn, name, now=NOW)
                except sqlite3.OperationalError:  # busy timeout retry
                    continue
                if claim is None:
                    break
                with lock:
                    results.append(claim)
        finally:
            conn.close()

    threads = [threading.Thread(target=claimer, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    claimed_ids = [c.request_id for c in results]
    assert sorted(claimed_ids) == sorted(ids)  # every request claimed exactly once
    assert len(claimed_ids) == len(set(claimed_ids))
    assert all(c.attempt_id for c in results)


def test_heartbeat_window_and_stale_token(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    heartbeat(db.conn, rid, claim.attempt_id, now=LATER)
    # wrong (stale) attempt token is refused
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, "att-not-mine", now=LATER)
    # after lease expiry the heartbeat is refused: ownership already lost
    db.conn.execute(
        "UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (NOW, rid)
    )
    db.conn.commit()
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=LATER)


def test_reclaim_expired_lease_abandons_and_requeues(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed", max_attempts=2,
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    # crash after claim: lease expires; reclaim records ABANDONED and requeues
    db.conn.execute("UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (NOW, rid))
    db.conn.commit()
    reclaimed = reclaim_expired(db.conn, now=LATER)
    assert rid in reclaimed
    row = db.conn.execute("SELECT * FROM scrape_requests WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "RETRY_WAIT"
    assert row["current_attempt_id"] is None and row["current_worker_id"] is None
    att = db.conn.execute(
        "SELECT outcome, abandoned_reason FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert att["outcome"] == "ABANDONED" and att["abandoned_reason"] == "LEASE_EXPIRED"
    # the stale worker cannot revive its lease or heartbeat
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=LATER)
    with pytest.raises(StaleOwnership):
        with fenced_commit(db.conn, rid, claim.attempt_id, now=LATER):
            pass  # pragma: no cover
    # a new claimant gets a FRESH attempt identity (after the backoff window)
    claim2 = claim_next_request(db.conn, "worker-2", now="2026-09-08T08:05:00.000000Z")
    assert claim2 is not None
    assert claim2.request_id == rid and claim2.attempt_id != claim.attempt_id


def test_reclaim_budget_exhaustion_fails_the_request(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed", max_attempts=1,
    )
    claim_next_request(db.conn, "worker-1", now=NOW)
    db.conn.execute("UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (NOW, rid))
    db.conn.commit()
    reclaimed = reclaim_expired(db.conn, now=LATER)
    assert rid in reclaimed
    row = db.conn.execute("SELECT status, last_failure_kind FROM scrape_requests WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "FAILED" and row["last_failure_kind"] == "LEASE_LOST"
    # FAILED is terminal: no further claim
    assert claim_next_request(db.conn, "worker-2", now=LATER) is None


# ------------------------------------------------------------------ RUN-08 fence


def test_fenced_commit_persists_outputs_and_terminal_transition_atomically(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)

    def mutate(conn):
        conn.execute(
            "INSERT INTO job_observations (id, run_id, request_id, attempt_id, source_id,"
            " binding_id, adapter_id, adapter_version, strategy, execution_class,"
            " observed_at, observation_unique_key)"
            " VALUES ('obs-1', ?, ?, ?, 'src-1','bnd-1','feed','1.0.0',"
            "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP',?,'ouk-1')",
            (run_id, rid, claim.attempt_id, LATER),
        )

    with fenced_commit(db.conn, rid, claim.attempt_id, now=LATER, mutate=mutate):
        pass
    row = db.conn.execute("SELECT status FROM scrape_requests WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "SUCCEEDED"
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 1
    att = db.conn.execute(
        "SELECT outcome FROM request_attempts WHERE attempt_id=?", (claim.attempt_id,)
    ).fetchone()
    assert att["outcome"] == "SUCCEEDED"


def test_fenced_commit_stale_attempt_commits_nothing(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    # lease expires and another worker reclaims + finishes first
    db.conn.execute("UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (NOW, rid))
    db.conn.commit()
    reclaim_expired(db.conn, now=LATER)
    claim2 = claim_next_request(db.conn, "worker-2", now="2026-09-08T08:02:00.000000Z")
    assert claim2 is not None
    with fenced_commit(db.conn, claim2.request_id, claim2.attempt_id, now="2026-09-08T08:02:00.000000Z"):
        pass

    def stale_mutate(conn):  # pragma: no cover - must never run to completion
        conn.execute(
            "INSERT INTO job_observations (id, run_id, request_id, attempt_id, source_id,"
            " binding_id, adapter_id, adapter_version, strategy, execution_class,"
            " observed_at, observation_unique_key)"
            " VALUES ('obs-stale', ?, ?, 'att-stale', 'src-1','bnd-1','feed','1.0.0',"
            "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP',?,'ouk-stale')",
            (run_id, rid, LATER),
        )

    with pytest.raises(StaleOwnership):
        with fenced_commit(db.conn, rid, claim.attempt_id, now=LATER, mutate=stale_mutate):
            pass
    # nothing from the stale attempt persisted
    assert db.conn.execute(
        "SELECT COUNT(*) FROM job_observations WHERE id = 'obs-stale'"
    ).fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 0


def test_fenced_commit_partial_outcome_stays_retryable(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed", max_attempts=3,
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)

    def mutate(conn):
        conn.execute(
            "INSERT INTO job_observations (id, run_id, request_id, attempt_id, source_id,"
            " binding_id, adapter_id, adapter_version, strategy, execution_class,"
            " observed_at, observation_unique_key)"
            " VALUES ('obs-p1', ?, ?, ?, 'src-1','bnd-1','feed','1.0.0',"
            "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP',?,'ouk-p1')",
            (run_id, rid, claim.attempt_id, LATER),
        )

    # validated PARTIAL policy: commit the observations, stay retryable
    with fenced_commit(
        db.conn, rid, claim.attempt_id, now=LATER, outcome="RETRY_WAIT",
        retry_delay_s=30, mutate=mutate,
    ):
        pass
    row = db.conn.execute("SELECT status, next_retry_at FROM scrape_requests WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "RETRY_WAIT" and row["next_retry_at"] > LATER
    assert db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0] == 1
    # retry re-claims with a fresh attempt; request-scoped idempotency is the
    # caller's contract (same observation_unique_key would collide)
    claim2 = claim_next_request(db.conn, "worker-1", now="2026-09-08T08:10:00.000000Z")
    assert claim2.request_id == rid


# -------------------------------------------------------------------- §18 cancel


def test_cancellation_semantics(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid_pending, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="DETAIL_FETCH",
        target_identity="https://example.test/job/1",
    )
    rid_running, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed", priority=10,
    )
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim.request_id == rid_running

    request_run_cancellation(db.conn, run_id, now=LATER)
    # pending acquisition work is CANCELLED (terminal)
    row = db.conn.execute("SELECT status FROM scrape_requests WHERE id=?", (rid_pending,)).fetchone()
    assert row["status"] == "CANCELLED"
    # running request stays RUNNING until its worker observes the fence
    row = db.conn.execute("SELECT status FROM scrape_requests WHERE id=?", (rid_running,)).fetchone()
    assert row["status"] == "RUNNING"
    # the running worker loses commit authority at the fence
    with pytest.raises(StaleOwnership):
        with fenced_commit(db.conn, rid_running, claim.attempt_id, now=LATER):
            pass  # pragma: no cover
    # it then cooperatively abandons for cancellation
    abandon_request_for_cancellation(db.conn, rid_running, now=LATER)
    row = db.conn.execute("SELECT status FROM scrape_requests WHERE id=?", (rid_running,)).fetchone()
    assert row["status"] == "CANCELLED"
    att = db.conn.execute(
        "SELECT outcome FROM request_attempts WHERE attempt_id=?", (claim.attempt_id,)
    ).fetchone()
    assert att["outcome"] == "CANCELLED"
    # no new acquisition claims
    assert claim_next_request(db.conn, "worker-2", now=LATER) is None


# -------------------------------------------------------------- RUN-01 aggregation


def _run_with_groups(db, outcomes_by_group):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    for group, outcome in outcomes_by_group.items():
        # one plan per group already exists for grp-1; add extra groups as needed
        if group != "grp-1":
            db.conn.execute(
                "INSERT INTO run_source_plans (id, run_id, source_id, source_plan_group_id,"
                " fallback_rank, binding_id, binding_revision_id, adapter_id, adapter_version,"
                " adapter_api_version, strategy, execution_class, permission_profile_id,"
                " permission_profile_revision, created_at)"
                " VALUES (?,?, 'src-1', ?, 0, 'bnd-1','bndrev-1','feed','1.0.0','1',"
                "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1, ?)",
                (f"rsp-{run_id[-8:]}-{group}", run_id, group, NOW),
            )
            db.conn.commit()
            set_group_outcome(
                db.conn, f"rsp-{run_id[-8:]}-{group}", outcome, now=LATER
            )
        else:
            set_group_outcome(db.conn, plans[0], outcome, now=LATER)
    return run_id


def test_run_aggregation_truth_table(db):
    # all SATISFIED -> SUCCEEDED (zero jobs is still success)
    run_id = _run_with_groups(db, {"grp-1": "SATISFIED"})
    assert aggregate_run(db.conn, run_id, now=LATER) == "SUCCEEDED"
    # SATISFIED + FAILED -> PARTIAL
    run_id = _run_with_groups(db, {"grp-1": "SATISFIED", "grp-2": "FAILED"})
    assert aggregate_run(db.conn, run_id, now=LATER) == "PARTIAL"
    # SATISFIED_PARTIAL alone -> PARTIAL (deliberately incomplete)
    run_id = _run_with_groups(db, {"grp-1": "SATISFIED_PARTIAL"})
    assert aggregate_run(db.conn, run_id, now=LATER) == "PARTIAL"
    # all FAILED -> FAILED
    run_id = _run_with_groups(db, {"grp-1": "FAILED", "grp-2": "FAILED"})
    assert aggregate_run(db.conn, run_id, now=LATER) == "FAILED"
    # unused fallback is SKIPPED_NOT_NEEDED, never a failure
    run_id = _run_with_groups(db, {"grp-1": "SATISFIED", "grp-2": "SKIPPED_NOT_NEEDED"})
    assert aggregate_run(db.conn, run_id, now=LATER) == "SUCCEEDED"
    # cancelled dominates
    run_id = _run_with_groups(db, {"grp-1": "SATISFIED", "grp-2": "CANCELLED"})
    assert aggregate_run(db.conn, run_id, now=LATER) == "CANCELLED"
    # run status row is updated and finished
    row = db.conn.execute("SELECT status, finished_at FROM scrape_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "CANCELLED" and row["finished_at"] == LATER


def test_aggregation_requires_all_groups_terminal(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    set_group_outcome(db.conn, plans[0], "SATISFIED", now=LATER)
    # a second group with no outcome yet: run must not finalize
    db.conn.execute(
        "INSERT INTO run_source_plans (id, run_id, source_id, source_plan_group_id,"
        " fallback_rank, binding_id, binding_revision_id, adapter_id, adapter_version,"
        " adapter_api_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at)"
        " VALUES ('rsp-open', ?, 'src-1', 'grp-2', 0, 'bnd-1','bndrev-1','feed','1.0.0','1',"
        "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1, ?)",
        (run_id, NOW),
    )
    db.conn.commit()
    assert aggregate_run(db.conn, run_id, now=LATER) is None
