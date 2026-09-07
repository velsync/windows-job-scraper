"""Queue and recovery test matrix (module 06 section 60, VER-01).

A queue test is incomplete if it proves reclaim but not stale-attempt fencing.
"""

import pytest

from jobscraper.db.connection import immediate_transaction
from jobscraper.queue.cancellation import cancel_run, run_is_cancelled
from jobscraper.queue.claims import (
    LeaseLost,
    claim_next_request,
    enqueue_request,
    fenced_commit,
    heartbeat,
    reclaim_expired_requests,
    record_failure,
)
from jobscraper.queue.recovery import recover_on_startup
from jobscraper.queue.retry import (
    binding_scope_key,
    cooldown_active,
    parse_retry_after,
    record_rate_limit,
    record_success,
)
from jobscraper.timeutil import add_seconds, utc_now_s


@pytest.fixture()
def run_setup(db):
    run_id = "run-1"
    plan_id = "rsp-1"
    now = utc_now_s()
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO scrape_runs(id, run_kind, status, created_at) VALUES ('run-1','COLLECT','RUNNING',?)",
            (now,),
        )
        tx.execute(
            "INSERT INTO run_source_plans(id, run_id, source_id, source_revision_id,"
            " source_config_snapshot_ref, source_plan_group_id, fallback_rank, binding_id,"
            " binding_revision_id, binding_revision, binding_config_snapshot_json, adapter_id,"
            " adapter_version, adapter_api_version, strategy, execution_class, run_config_hash,"
            " permission_profile_id, permission_profile_revision, created_at)"
            " VALUES ('rsp-1','run-1','src-1','srev-1','{}','grp-1',1,'b-1','br-1',1,'{}',"
            " 'greenhouse','1.0.0','1','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','h','pp-1','ppr-1',?)",
            (now,),
        )
        tx.execute(
            "INSERT INTO sources(id, display_name, entry_url, canonical_host, created_at, updated_at)"
            " VALUES ('src-1','Test','https://x.example','x.example',?,?)",
            (now, now),
        )
    return run_id, plan_id


def enqueue(db, run_setup, **kw):
    run_id, plan_id = run_setup
    defaults = dict(
        db=db,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src-1",
        binding_id="b-1",
        request_type="LIST_FETCH",
        request_unique_key="k1",
        payload={"url": "https://x.example/jobs"},
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
    )
    defaults.update(kw)
    return enqueue_request(**defaults)


class TestEnqueueAndClaim:
    def test_duplicate_enqueue_is_one_work_item(self, db, run_setup):
        assert enqueue(db, run_setup) is not None
        assert enqueue(db, run_setup) is None  # same logical key suppressed
        count = db.query_one("SELECT COUNT(*) c FROM scrape_requests")["c"]
        assert count == 1

    def test_same_url_different_purpose_is_distinct(self, db, run_setup):
        enqueue(db, run_setup, request_unique_key="http:list:url1")
        enqueue(db, run_setup, request_unique_key="browser:escalate:url1")
        assert db.query_one("SELECT COUNT(*) c FROM scrape_requests")["c"] == 2

    def test_atomic_claim_single_winner(self, db, run_setup):
        enqueue(db, run_setup)
        first = claim_next_request(db, worker_id="w1")
        assert first is not None
        second = claim_next_request(db, worker_id="w2")
        assert second is None  # only one worker receives the claim

    def test_claim_skips_not_due_retry_wait(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")
        record_failure(db, request_id=rid, attempt_id=claim.attempt_id, failure_kind="TIMEOUT")
        # next_retry_at is in the future -> not claimable now
        assert claim_next_request(db, worker_id="w2") is None
        # force it due
        db.execute(
            "UPDATE scrape_requests SET next_retry_at=? WHERE id=?",
            (utc_now_s(), rid),
        )
        again = claim_next_request(db, worker_id="w2")
        assert again is not None
        assert again.attempt_id != claim.attempt_id  # new attempt, same request

    def test_execution_class_partitioning(self, db, run_setup):
        enqueue(db, run_setup, request_unique_key="h1", execution_class="HTTP")
        enqueue(db, run_setup, request_unique_key="b1", execution_class="BROWSER")
        claim = claim_next_request(db, worker_id="w1", execution_classes=("BROWSER",))
        assert claim is not None
        assert claim.execution_class == "BROWSER"

    def test_claim_skips_cancelled_run(self, db, run_setup):
        run_id, _ = run_setup
        enqueue(db, run_setup)
        cancel_run(db, run_id)
        assert claim_next_request(db, worker_id="w1") is None


class TestLeasesAndFencing:
    def test_heartbeat_renews(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")
        assert heartbeat(db, request_id=rid, attempt_id=claim.attempt_id, worker_id="w1") is True

    def test_heartbeat_wrong_attempt_rejected(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")
        assert heartbeat(db, request_id=rid, attempt_id="forged", worker_id="w1") is False

    def test_heartbeat_after_lease_expiry_rejected(self, db, run_setup):
        """VER-01: heartbeat after lease expiry is rejected; the expired
        worker cannot revive itself before the reclaimer runs."""
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1", lease_window_s=10)
        future = add_seconds(utc_now_s(), 11)
        assert heartbeat(db, request_id=rid, attempt_id=claim.attempt_id, worker_id="w1", now=future) is False
        # Ownership is already lost even though the reclaimer has not run.
        assert heartbeat(db, request_id=rid, attempt_id=claim.attempt_id, worker_id="w1") is False

    def test_stale_worker_commit_rejected(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1", lease_window_s=10)
        # Lease expires.
        future = add_seconds(utc_now_s(), 11)
        # Reclaimer runs.
        reclaimed = reclaim_expired_requests(db, now=future)
        assert rid in reclaimed
        # Stale worker tries to commit late.
        with pytest.raises(LeaseLost):

            def commit(tx):
                tx.execute("CREATE TABLE should_not_exist (x)")

            fenced_commit(
                db, request_id=rid, attempt_id=claim.attempt_id, commit=commit, now=future
            )
        # No partial write survived.
        assert db.query_one(
            "SELECT name FROM sqlite_master WHERE name='should_not_exist'"
        ) is None
        assert db.query_one("SELECT status FROM scrape_requests WHERE id=?", (rid,))["status"] == "RETRY_WAIT"

    def test_expired_worker_cannot_reclaim_ownership_via_claim_race(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim1 = claim_next_request(db, worker_id="w1", lease_window_s=10)
        future = add_seconds(utc_now_s(), 11)
        # w2 reclaims through the normal path.
        claim2 = claim_next_request(db, worker_id="w2", now=future)
        assert claim2 is not None and claim2.attempt_id != claim1.attempt_id
        # w1's late fenced commit must fail.
        with pytest.raises(LeaseLost):
            fenced_commit(db, request_id=rid, attempt_id=claim1.attempt_id, commit=lambda tx: None, now=future)

    def test_fenced_commit_atomic_success(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")

        def commit(tx):
            tx.execute(
                "INSERT INTO app_meta(key, value, updated_at) VALUES ('committed','yes',?)",
                (utc_now_s(),),
            )
            return "result"

        out = fenced_commit(db, request_id=rid, attempt_id=claim.attempt_id, commit=commit)
        assert out == "result"
        row = db.query_one("SELECT status FROM scrape_requests WHERE id=?", (rid,))
        assert row["status"] == "SUCCEEDED"
        assert db.query_one("SELECT value FROM app_meta WHERE key='committed'")["value"] == "yes"

    def test_fenced_commit_rollback_on_error(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")

        def bad_commit(tx):
            tx.execute(
                "INSERT INTO app_meta(key, value, updated_at) VALUES ('x','1',?)", (utc_now_s(),)
            )
            raise RuntimeError("worker crashed mid-commit")

        with pytest.raises(RuntimeError):
            fenced_commit(db, request_id=rid, attempt_id=claim.attempt_id, commit=bad_commit)
        assert db.query_one("SELECT value FROM app_meta WHERE key='x'") is None
        # Request remains RUNNING (owned) — recovery decides its fate.
        assert db.query_one("SELECT status FROM scrape_requests WHERE id=?", (rid,))["status"] == "RUNNING"

    def test_heartbeat_rejected_after_cancellation(self, db, run_setup):
        run_id, _ = run_setup
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")
        cancel_run(db, run_id)
        assert heartbeat(db, request_id=rid, attempt_id=claim.attempt_id, worker_id="w1") is False
        with pytest.raises(LeaseLost):
            fenced_commit(db, request_id=rid, attempt_id=claim.attempt_id, commit=lambda tx: None)

    def test_cancel_preserves_accepted_evidence(self, db, run_setup):
        run_id, _ = run_setup
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")

        def commit(tx):
            tx.execute(
                "INSERT INTO app_meta(key, value, updated_at) VALUES ('evidence','kept',?)",
                (utc_now_s(),),
            )

        # Commit before cancellation.
        fenced_commit(db, request_id=rid, attempt_id=claim.attempt_id, commit=commit)
        cancel_run(db, run_id)
        assert db.query_one("SELECT value FROM app_meta WHERE key='evidence'")["value"] == "kept"
        assert run_is_cancelled(db, run_id)


class TestRetryAndBudgets:
    def test_retry_budget_exhaustion(self, db, run_setup):
        rid = enqueue(db, run_setup, max_attempts=2)
        for i in range(2):
            claim = claim_next_request(db, worker_id=f"w{i}")
            status = record_failure(db, request_id=rid, attempt_id=claim.attempt_id, failure_kind="TIMEOUT")
            if i == 0:
                assert status == "RETRY_WAIT"
                db.execute("UPDATE scrape_requests SET next_retry_at=? WHERE id=?", (utc_now_s(), rid))
        assert status == "FAILED"

    def test_retry_after_honored(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")
        record_failure(
            db,
            request_id=rid,
            attempt_id=claim.attempt_id,
            failure_kind="RATE_LIMIT",
            retry_after_s=120,
        )
        row = db.query_one("SELECT next_retry_at, status FROM scrape_requests WHERE id=?", (rid,))
        assert row["status"] == "RETRY_WAIT"
        assert row["next_retry_at"] > add_seconds(utc_now_s(), 100)

    def test_non_retryable_failure_fails_fast(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")
        status = record_failure(
            db, request_id=rid, attempt_id=claim.attempt_id, failure_kind="POLICY_REJECTED", retryable=False
        )
        assert status == "FAILED"

    def test_idempotent_retry_no_duplicate_request(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1")
        record_failure(db, request_id=rid, attempt_id=claim.attempt_id, failure_kind="TIMEOUT")
        db.execute("UPDATE scrape_requests SET next_retry_at=? WHERE id=?", (utc_now_s(), rid))
        claim2 = claim_next_request(db, worker_id="w1")
        assert claim2.request_id == rid
        assert db.query_one("SELECT COUNT(*) c FROM scrape_requests")["c"] == 1
        assert db.query_one("SELECT COUNT(*) c FROM request_attempts WHERE request_id=?", (rid,))["c"] == 2


class TestReclaimAndRecovery:
    def test_reclaim_orphan_and_retry(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim = claim_next_request(db, worker_id="w1", lease_window_s=10)
        future = add_seconds(utc_now_s(), 11)
        reclaimed = reclaim_expired_requests(db, now=future)
        assert reclaimed == [rid]
        row = db.query_one("SELECT status FROM scrape_requests WHERE id=?", (rid,))
        assert row["status"] == "RETRY_WAIT"
        attempt = db.query_one("SELECT outcome FROM request_attempts WHERE attempt_id=?", (claim.attempt_id,))
        assert attempt["outcome"] == "ABANDONED"

    def test_reclaim_budget_exhausted_fails(self, db, run_setup):
        rid = enqueue(db, run_setup, max_attempts=1)
        claim_next_request(db, worker_id="w1", lease_window_s=10)
        future = add_seconds(utc_now_s(), 11)
        reclaimed = reclaim_expired_requests(db, now=future)
        assert reclaimed == [rid]
        assert db.query_one("SELECT status FROM scrape_requests WHERE id=?", (rid,))["status"] == "FAILED"

    def test_cancelled_run_reclaim_cancels(self, db, run_setup):
        run_id, _ = run_setup
        rid = enqueue(db, run_setup)
        claim_next_request(db, worker_id="w1", lease_window_s=10)
        cancel_run(db, run_id)
        future = add_seconds(utc_now_s(), 11)
        reclaim_expired_requests(db, now=future)
        assert db.query_one("SELECT status FROM scrape_requests WHERE id=?", (rid,))["status"] == "CANCELLED"

    def test_startup_recovery(self, db, run_setup):
        rid = enqueue(db, run_setup)
        claim_next_request(db, worker_id="dead-worker", lease_window_s=10)
        # Simulate crash: lease expires, then service restarts later.
        future = add_seconds(utc_now_s(), 30)
        report = recover_on_startup(db, now=future)
        assert rid in report["reclaimed"]
        assert db.query_one("SELECT status FROM scrape_requests WHERE id=?", (rid,))["status"] == "RETRY_WAIT"


class TestCooldowns:
    def test_cooldown_survives_restart(self, db, run_setup):
        key = binding_scope_key("b-1")
        record_rate_limit(db, scope_key=key, retry_after_s=600)
        # Simulate restart: fresh Database object over the same file.
        assert cooldown_active(db, scope_key=key) is True
        future = add_seconds(utc_now_s(), 601)
        assert cooldown_active(db, scope_key=key, now=future) is False

    def test_success_reopens_circuit(self, db, run_setup):
        key = binding_scope_key("b-1")
        record_rate_limit(db, scope_key=key, retry_after_s=10)
        record_success(db, scope_key=key)
        row = db.query_one("SELECT circuit_state FROM host_policy_state WHERE scope_key=?", (key,))
        assert row["circuit_state"] == "CLOSED"

    def test_parse_retry_after(self):
        assert parse_retry_after("120") == 120.0
        assert parse_retry_after(None) is None
        assert parse_retry_after("garbage") is None


class TestCancellation:
    def test_cancel_pending_requests(self, db, run_setup):
        run_id, _ = run_setup
        enqueue(db, run_setup, request_unique_key="a")
        enqueue(db, run_setup, request_unique_key="b")
        result = cancel_run(db, run_id)
        assert result["cancelled_requests"] == 2
        statuses = {r["status"] for r in db.query("SELECT status FROM scrape_requests")}
        assert statuses == {"CANCELLED"}

    def test_cancel_idempotent_for_terminal(self, db, run_setup):
        run_id, _ = run_setup
        db.execute("UPDATE scrape_runs SET status='SUCCEEDED', finished_at=? WHERE id=?", (utc_now_s(), run_id))
        assert cancel_run(db, run_id)["already_terminal"] is True
