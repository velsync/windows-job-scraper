"""S3.1 integration tests: durable frontier ownership (03 RUN-04..07/09/20, §18/§50).

Hardening proven with real SQLite file databases:

* RUN-07: atomic single-winner claims; fresh attempt_id per claim bound to
  the current service epoch; lease renewal; an expired lease is already lost
  ownership and cannot be revived before reclaim;
* RUN-06: attempt history preserved across abandons/retries;
* RUN-07 reclaim: prior attempt ABANDONED, RETRY_WAIT within budget, FAILED
  once the retry budget is exhausted; RETRY_WAIT is claimable only after the
  durable retry time;
* §50/RUN-20: service clock epochs — stale-epoch rejection, material
  forward/backward wall-clock anomaly stops new claims, rotates the service
  epoch, invalidates in-memory ownership and reclaims/retries safely rather
  than extending an elapsed lease; monotonic time governs the live anomaly
  comparison while durable comparisons stay DB UTC;
* RUN-09/§18: restart recovery rotates the epoch and reclaims outstanding
  RUNNING work;
* R2-F3 task-class claim policy: acquisition requires current source/binding
  authority and stops after cancellation; host-native obligations never gain
  source-network authority from being claimable and are never stranded by
  cancellation or source/binding administrative revocation;
* §50: no network/file I/O while holding claim/heartbeat transactions.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.claims import (
    Claim,
    ClaimsHalted,
    StaleOwnership,
    claim_next_request,
    heartbeat,
    reclaim_expired,
    reclaim_orphaned_epoch_work,
)
from jobscraper.runtime.clock import (
    ClockAnomaly,
    ServiceClockGuard,
    ServiceEpoch,
    StaleServiceEpoch,
    begin_service_epoch,
    current_service_epoch,
)
from jobscraper.runtime.recovery import recover_interrupted_requests
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T08:00:00.000000Z"
T1 = "2026-09-08T08:01:00.000000Z"
T2 = "2026-09-08T08:02:00.000000Z"
T3 = "2026-09-08T08:03:00.000000Z"
FUTURE = "2026-09-08T09:00:00.000000Z"


def _family(
    db: Database,
    *,
    source_desired: str = "ENABLED",
    source_admin: str = "NORMAL",
    binding_desired: str = "ENABLED",
    binding_admin: str = "NORMAL",
):
    conn = db.conn
    conn.execute(
        "INSERT INTO sources (id, display_name, source_family, entry_url, desired_state,"
        " administrative_state, created_at, updated_at)"
        " VALUES ('src-1','Feed','PUBLIC_FEED','https://example.test/feed', ?, ?, ?, ?)",
        (source_desired, source_admin, NOW, NOW),
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
        "INSERT INTO source_adapter_bindings (id, source_id, display_name, desired_state,"
        " administrative_state, created_at) VALUES ('bnd-1','src-1','api', ?, ?, ?)",
        (binding_desired, binding_admin, NOW),
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
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "s31.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    _family(database)
    yield database
    database.close()


def _enqueue(db, *, request_type="LIST_FETCH", target="https://example.test/feed",
             logical_key=None, max_attempts=3, priority=0):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    request_id, _created = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type=request_type, target_identity=target,
        logical_key=logical_key, max_attempts=max_attempts, priority=priority,
    )
    return run_id, plans[0], request_id


def _epoch_rows(db):
    return db.conn.execute(
        "SELECT id, started_at, ended_at, end_reason FROM service_clock_epochs"
        " ORDER BY created_at"
    ).fetchall()


# ------------------------------------------------------------- RUN-05 enqueue


def test_duplicate_enqueue_is_one_durable_work_item(db):
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    first, created1 = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    second, created2 = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed?utm_source=x",
    )
    assert created1 and not created2 and first == second
    # same target under a different strategy/purpose stays distinct (ACQ-07)
    _third, created3 = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed", strategy="PLAYWRIGHT_PUBLIC",
    )
    assert created3


# ------------------------------------------------- service epoch claim binding


def test_claim_binds_fresh_attempt_to_current_service_epoch(db):
    epoch = begin_service_epoch(db.conn, now=NOW)
    assert isinstance(epoch, ServiceEpoch) and epoch.epoch_id
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "worker-1", now=NOW, epoch=epoch)
    assert isinstance(claim, Claim)
    assert claim.request_id == rid
    assert claim.service_epoch_id == epoch.epoch_id
    att = db.conn.execute(
        "SELECT service_epoch_id FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert att["service_epoch_id"] == epoch.epoch_id


def test_claim_without_open_epoch_remains_unbound_backcompat(db):
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim is not None and claim.request_id == rid
    assert claim.service_epoch_id is None
    att = db.conn.execute(
        "SELECT service_epoch_id FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert att["service_epoch_id"] is None


def test_claim_auto_binds_to_open_epoch_without_explicit_argument(db):
    epoch = begin_service_epoch(db.conn, now=NOW)
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim.service_epoch_id == epoch.epoch_id


def test_claim_with_stale_explicit_epoch_is_rejected(db):
    epoch1 = begin_service_epoch(db.conn, now=NOW)
    epoch2 = begin_service_epoch(db.conn, now=T1)  # rotates: epoch1 is over
    assert epoch2.epoch_id != epoch1.epoch_id
    rows = _epoch_rows(db)
    assert rows[0]["ended_at"] == T1 and rows[0]["end_reason"] == "SERVICE_RESTART"
    assert rows[1]["ended_at"] is None
    _run_id, _plan_id, _rid = _enqueue(db)
    with pytest.raises(StaleServiceEpoch):
        claim_next_request(db.conn, "worker-1", now=T1, epoch=epoch1)
    # the current epoch is accepted
    claim = claim_next_request(db.conn, "worker-1", now=T1, epoch=epoch2)
    assert claim is not None and claim.service_epoch_id == epoch2.epoch_id


# --------------------------------------------------------------- RUN-07 lease


def test_expired_lease_cannot_be_revived_before_reclaim(db):
    epoch = begin_service_epoch(db.conn, now=NOW)
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "worker-1", now=NOW, epoch=epoch)
    # force expiry; the reclaimer has NOT run yet
    db.conn.execute(
        "UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (T1, rid)
    )
    db.conn.commit()
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=T2, epoch=epoch)
    # reclaim records ABANDONED and requeues within budget
    assert rid in reclaim_expired(db.conn, now=T2)
    att = db.conn.execute(
        "SELECT outcome, abandoned_reason FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert att["outcome"] == "ABANDONED" and att["abandoned_reason"] == "LEASE_EXPIRED"
    # even after reclaim the dead token stays dead; retry mints a fresh attempt
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=T3, epoch=epoch)
    claim2 = claim_next_request(db.conn, "worker-2", now=T3, epoch=epoch)
    assert claim2 is not None and claim2.attempt_id != claim.attempt_id


def test_retry_wait_is_claimable_only_after_durable_retry_time(db):
    epoch = begin_service_epoch(db.conn, now=NOW)
    _run_id, _plan_id, rid = _enqueue(db)
    claim_next_request(db.conn, "worker-1", now=NOW, epoch=epoch)
    db.conn.execute(
        "UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (T1, rid)
    )
    db.conn.commit()
    reclaim_expired(db.conn, now=T2)
    row = db.conn.execute(
        "SELECT status, next_retry_at FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "RETRY_WAIT" and row["next_retry_at"] > T2
    # not yet due
    assert claim_next_request(db.conn, "worker-2", now=T2, epoch=epoch) is None
    # due
    assert claim_next_request(db.conn, "worker-2", now=row["next_retry_at"], epoch=epoch) is not None


def test_attempt_history_preserved_across_abandon_and_retry(db):
    epoch = begin_service_epoch(db.conn, now=NOW)
    _run_id, _plan_id, rid = _enqueue(db, max_attempts=3)
    first = claim_next_request(db.conn, "worker-1", now=NOW, epoch=epoch)
    db.conn.execute(
        "UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (T1, rid)
    )
    db.conn.commit()
    reclaim_expired(db.conn, now=T2)
    second = claim_next_request(db.conn, "worker-2", now=T3, epoch=epoch)
    assert second is not None and second.attempt_id != first.attempt_id
    attempts = db.conn.execute(
        "SELECT attempt_id, worker_id, outcome, abandoned_reason, service_epoch_id"
        " FROM request_attempts WHERE request_id = ? ORDER BY created_at",
        (rid,),
    ).fetchall()
    assert len(attempts) == 2
    assert attempts[0]["outcome"] == "ABANDONED"
    assert attempts[0]["abandoned_reason"] == "LEASE_EXPIRED"
    assert attempts[1]["outcome"] is None  # in flight
    assert all(a["service_epoch_id"] == epoch.epoch_id for a in attempts)
    row = db.conn.execute(
        "SELECT attempt_count FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()
    assert row["attempt_count"] == 2


def test_budget_exhaustion_is_terminal_failure(db):
    epoch = begin_service_epoch(db.conn, now=NOW)
    _run_id, _plan_id, rid = _enqueue(db, max_attempts=1)
    claim_next_request(db.conn, "worker-1", now=NOW, epoch=epoch)
    db.conn.execute(
        "UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (T1, rid)
    )
    db.conn.commit()
    assert rid in reclaim_expired(db.conn, now=T2)
    row = db.conn.execute(
        "SELECT status, last_failure_kind FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "FAILED" and row["last_failure_kind"] == "LEASE_LOST"
    assert claim_next_request(db.conn, "worker-2", now=T3, epoch=epoch) is None


# ------------------------------------------------- epoch rotation / staleness


def test_heartbeat_rejected_after_epoch_rotation(db):
    epoch1 = begin_service_epoch(db.conn, now=NOW)
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "worker-1", now=NOW, epoch=epoch1)
    # service epoch rotates (e.g. restart/anomaly); the lease is still valid
    epoch2 = begin_service_epoch(db.conn, now=T1)
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=T1, epoch=epoch2)
    # the old explicit epoch is refused outright
    with pytest.raises(StaleServiceEpoch):
        heartbeat(db.conn, rid, claim.attempt_id, now=T1, epoch=epoch1)


def test_epoch_scoped_reclaim_abandons_orphans_of_an_invalidated_epoch(db):
    epoch1 = begin_service_epoch(db.conn, now=NOW)
    _run_id, _plan_id, rid_keep = _enqueue(db, logical_key="page=1")
    _r2, _p2, rid_fail = _enqueue(db, logical_key="page=2", max_attempts=1)
    claim_keep = claim_next_request(db.conn, "worker-1", now=NOW, epoch=epoch1)
    claim_fail = claim_next_request(db.conn, "worker-1", now=NOW, epoch=epoch1)
    assert {claim_keep.request_id, claim_fail.request_id} == {rid_keep, rid_fail}
    # epoch invalidated (leases are NOT expired — this is not lease reclaim)
    begin_service_epoch(db.conn, now=T1)
    reclaimed = reclaim_orphaned_epoch_work(db.conn, epoch1.epoch_id, now=T1)
    assert sorted(reclaimed) == sorted([rid_keep, rid_fail])
    keep = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (rid_keep,)
    ).fetchone()
    fail = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (rid_fail,)
    ).fetchone()
    assert keep["status"] == "RETRY_WAIT"
    assert fail["status"] == "FAILED"
    for attempt_id in (claim_keep.attempt_id, claim_fail.attempt_id):
        att = db.conn.execute(
            "SELECT outcome, abandoned_reason FROM request_attempts"
            " WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert att["outcome"] == "ABANDONED"
        assert att["abandoned_reason"] == "CLOCK_ANOMALY"
    # idempotent: nothing left bound to the dead epoch
    assert reclaim_orphaned_epoch_work(db.conn, epoch1.epoch_id, now=T1) == []


# ------------------------------------------------------ §50 clock anomaly


def _guard(db, epoch, *, tolerance_s=30.0):
    # injected monotonic source: deterministic live-elapsed comparison
    return ServiceClockGuard(epoch, tolerance_s=tolerance_s, monotonic=lambda: 0.0)


def test_forward_clock_anomaly_stops_claims_rotates_epoch_and_reclaims(db):
    epoch1 = begin_service_epoch(db.conn, now=NOW)
    guard = _guard(db, epoch1)
    assert guard.observe(db.conn, db_now=T1) is None  # baseline checkpoint
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "worker-1", now=T1, guard=guard)
    assert claim is not None and claim.service_epoch_id == epoch1.epoch_id

    # material forward wall-clock jump (~1h wall, ~0s monotonic)
    anomaly = guard.observe(db.conn, db_now=FUTURE)
    assert isinstance(anomaly, ClockAnomaly)
    assert anomaly.direction == "FORWARD"
    assert anomaly.invalidated_epoch_id == epoch1.epoch_id
    assert anomaly.new_epoch_id != epoch1.epoch_id
    assert guard.claims_halted

    # durable anomaly record: the old epoch ended with an anomaly reason and
    # a fresh open epoch exists
    rows = _epoch_rows(db)
    assert rows[0]["end_reason"] == "CLOCK_ANOMALY_FORWARD"
    assert rows[1]["ended_at"] is None and rows[1]["id"] == anomaly.new_epoch_id

    # new claims stop while halted
    with pytest.raises(ClaimsHalted):
        claim_next_request(db.conn, "worker-2", now=FUTURE, guard=guard)

    # the stale attempt cannot heartbeat; recovery never revives it
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=FUTURE)

    # safe reclaim/retry under the budget (post-jump DB time stays at/after
    # FUTURE; going back below it would itself be a backward anomaly)
    assert reclaim_orphaned_epoch_work(
        db.conn, anomaly.invalidated_epoch_id, now=FUTURE
    ) == [rid]
    guard.clear_halt()
    due = db.conn.execute(
        "SELECT next_retry_at FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()["next_retry_at"]
    assert due > FUTURE
    retry = claim_next_request(db.conn, "worker-2", now=due, guard=guard)
    assert retry is not None and retry.attempt_id != claim.attempt_id
    assert retry.service_epoch_id == anomaly.new_epoch_id


def test_backward_clock_anomaly_invalidates_unexpired_lease_ownership(db):
    epoch1 = begin_service_epoch(db.conn, now=NOW)
    guard = _guard(db, epoch1)
    assert guard.observe(db.conn, db_now=FUTURE) is None  # baseline at "future"
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "worker-1", now=FUTURE, guard=guard)
    assert claim is not None

    # material backward wall-clock jump back to NOW; the lease (FUTURE+120s)
    # still appears unexpired at NOW, yet ownership is already invalidated
    anomaly = guard.observe(db.conn, db_now=NOW)
    assert anomaly is not None and anomaly.direction == "BACKWARD"
    assert guard.claims_halted
    rows = _epoch_rows(db)
    assert rows[0]["end_reason"] == "CLOCK_ANOMALY_BACKWARD"

    lease = db.conn.execute(
        "SELECT lease_until FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()["lease_until"]
    assert lease > NOW  # unexpired by DB time — still dead ownership
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=NOW)

    # lease-based reclaim finds nothing (nothing expired); epoch reclaim does it
    assert reclaim_expired(db.conn, now=NOW) == []
    assert reclaim_orphaned_epoch_work(db.conn, epoch1.epoch_id, now=NOW) == [rid]
    att = db.conn.execute(
        "SELECT outcome, abandoned_reason FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert att["outcome"] == "ABANDONED" and att["abandoned_reason"] == "CLOCK_ANOMALY"

    guard.clear_halt()
    due = db.conn.execute(
        "SELECT next_retry_at FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()["next_retry_at"]
    retry = claim_next_request(db.conn, "worker-2", now=due, guard=guard)
    assert retry is not None and retry.attempt_id != claim.attempt_id
    # the dead token can never heartbeat again
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=due)


def test_clock_drift_within_tolerance_keeps_the_epoch(db):
    epoch1 = begin_service_epoch(db.conn, now=NOW)
    guard = _guard(db, epoch1, tolerance_s=30.0)
    assert guard.observe(db.conn, db_now=T1) is None
    # +5s forward drift: within tolerance
    assert guard.observe(db.conn, db_now="2026-09-08T08:01:05.000000Z") is None
    # -5s backward drift: within tolerance
    assert guard.observe(db.conn, db_now=T1) is None
    assert not guard.claims_halted
    assert current_service_epoch(db.conn).epoch_id == epoch1.epoch_id
    _run_id, _plan_id, _rid = _enqueue(db)
    assert claim_next_request(db.conn, "w", now=T1, guard=guard) is not None


def test_repeated_anomaly_without_clear_halt_keeps_claims_stopped(db):
    epoch1 = begin_service_epoch(db.conn, now=NOW)
    guard = _guard(db, epoch1)
    guard.observe(db.conn, db_now=T1)
    anomaly = guard.observe(db.conn, db_now=FUTURE)
    assert anomaly is not None
    with pytest.raises(ClaimsHalted):
        claim_next_request(db.conn, "w", now=FUTURE, guard=guard)
    # reclaim happened, but the halt persists until explicitly cleared
    reclaim_orphaned_epoch_work(db.conn, anomaly.invalidated_epoch_id, now=FUTURE)
    with pytest.raises(ClaimsHalted):
        claim_next_request(db.conn, "w", now=FUTURE, guard=guard)
    guard.clear_halt()
    assert claim_next_request(db.conn, "w", now=FUTURE, guard=guard) is None  # no work


# --------------------------------------------- RUN-09/§18 restart recovery


def test_restart_recovery_rotates_epoch_and_reclaims_outstanding_running(db):
    epoch1 = begin_service_epoch(db.conn, now=NOW)
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "dead-worker", now=NOW, epoch=epoch1)
    assert claim is not None

    # the service process died; a fresh process starts a fresh epoch BEFORE
    # recovery, then reclaims the orphaned RUNNING work
    epoch2 = begin_service_epoch(db.conn, now=T1)
    assert epoch2.epoch_id != epoch1.epoch_id
    old = db.conn.execute(
        "SELECT ended_at, end_reason FROM service_clock_epochs WHERE id = ?",
        (epoch1.epoch_id,),
    ).fetchone()
    assert old["ended_at"] == T1 and old["end_reason"] == "SERVICE_RESTART"

    report = recover_interrupted_requests(db.conn, now=T1)
    assert report["reclaimed"] == [rid]
    att = db.conn.execute(
        "SELECT outcome, abandoned_reason FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert att["outcome"] == "ABANDONED"
    assert att["abandoned_reason"] == "SERVICE_RESTART"
    # the prior owner cannot heartbeat under the fresh epoch
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=T1, epoch=epoch2)
    # fresh claim after backoff binds to the fresh epoch
    due = db.conn.execute(
        "SELECT next_retry_at FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()["next_retry_at"]
    retry = claim_next_request(db.conn, "new-worker", now=due, epoch=epoch2)
    assert retry is not None
    assert retry.service_epoch_id == epoch2.epoch_id
    assert retry.attempt_id != claim.attempt_id


# --------------------------------- R2-F3 task-class claim/authorization split


def test_host_native_claims_are_not_stranded_by_administrative_revocation(db):
    _run_id, _plan_id, rid_acq = _enqueue(db, request_type="LIST_FETCH")
    _r2, _p2, rid_local = _enqueue(db, request_type="ELIGIBILITY", target="obs-1")
    # source/binding lose current authority AFTER accepted evidence exists
    db.conn.execute(
        "UPDATE sources SET administrative_state = 'QUARANTINED' WHERE id = 'src-1'"
    )
    db.conn.execute(
        "UPDATE source_adapter_bindings SET desired_state = 'DISABLED'"
        " WHERE id = 'bnd-1'"
    )
    db.conn.commit()
    # acquisition is blocked by the current-authority predicate…
    # …but the host-native obligation stays claimable (it never gains
    # source-network authority from being claimable)
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim is not None and claim.request_id == rid_local
    acq = db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (rid_acq,)
    ).fetchone()
    assert acq["status"] == "PENDING"  # blocked, not silently cancelled
    # restoring authority makes the acquisition claimable again
    db.conn.execute(
        "UPDATE sources SET administrative_state = 'NORMAL' WHERE id = 'src-1'"
    )
    db.conn.execute(
        "UPDATE source_adapter_bindings SET desired_state = 'ENABLED'"
        " WHERE id = 'bnd-1'"
    )
    db.conn.commit()
    claim2 = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim2 is not None and claim2.request_id == rid_acq


def test_host_native_heartbeat_survives_run_cancellation(db):
    run_id, _plan_id, rid = _enqueue(db, request_type="NORMALIZE", target="obs-1")
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim.request_id == rid
    request_run_cancellation(db.conn, run_id, now=T1)
    # §18/R2-F3: cancellation stops source-network work, not accepted local
    # processing obligations — the heartbeat keeps renewing the lease
    lease_until = heartbeat(db.conn, rid, claim.attempt_id, now=T1)
    assert lease_until > T1


def test_host_native_heartbeat_survives_source_disable(db):
    _run_id, _plan_id, rid = _enqueue(db, request_type="SCORE", target="obs-1")
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    assert claim.request_id == rid
    db.conn.execute("UPDATE sources SET desired_state = 'DISABLED' WHERE id = 'src-1'")
    db.conn.commit()
    lease_until = heartbeat(db.conn, rid, claim.attempt_id, now=T1)
    assert lease_until > T1


def test_acquisition_heartbeat_denied_after_binding_quarantine(db):
    _run_id, _plan_id, rid = _enqueue(db, request_type="LIST_FETCH")
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    db.conn.execute(
        "UPDATE source_adapter_bindings SET administrative_state = 'QUARANTINED'"
        " WHERE id = 'bnd-1'"
    )
    db.conn.commit()
    # RUN-02 rule 9: current revocation overrides the pinned plan — the live
    # authorization checkpoint refuses further lease renewals
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=T1)


def test_acquisition_heartbeat_denied_after_cancellation(db):
    run_id, _plan_id, rid = _enqueue(db, request_type="LIST_FETCH")
    claim = claim_next_request(db.conn, "worker-1", now=NOW)
    request_run_cancellation(db.conn, run_id, now=T1)
    with pytest.raises(StaleOwnership):
        heartbeat(db.conn, rid, claim.attempt_id, now=T1)


# ----------------------------------------------------- concurrent claim race


def test_concurrent_claims_single_winner_under_epoch(db, tmp_path):
    epoch = begin_service_epoch(db.conn, now=NOW)
    run_id, plans = create_run(db.conn, profile_id=None, plans=[_plan()], now=NOW)
    rid, _created = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    path = tmp_path / "s31.db"
    winners: list[Claim] = []
    lock = threading.Lock()

    def claimer(name: str):
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            try:
                claim = claim_next_request(conn, name, now=NOW, epoch=epoch)
            except sqlite3.OperationalError:
                claim = None
            if claim is not None:
                with lock:
                    winners.append(claim)
        finally:
            conn.close()

    threads = [threading.Thread(target=claimer, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(winners) == 1  # exactly one winner
    assert winners[0].request_id == rid
    assert winners[0].service_epoch_id == epoch.epoch_id
    atts = db.conn.execute(
        "SELECT COUNT(*) FROM request_attempts WHERE request_id = ?", (rid,)
    ).fetchone()[0]
    assert atts == 1


# ----------------------------------------- §50 no I/O inside claim transactions


def test_claim_and_heartbeat_transactions_perform_no_network_or_file_io(
    db, monkeypatch
):
    import builtins
    import http.client
    import socket
    import urllib.request

    def _deny(*args, **kwargs):  # pragma: no cover - failure is the point
        raise AssertionError("network/file I/O attempted inside claim path")

    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _deny)
    monkeypatch.setattr(urllib.request, "urlopen", _deny)
    monkeypatch.setattr(builtins, "open", _deny)

    epoch = begin_service_epoch(db.conn, now=NOW)
    guard = _guard(db, epoch)
    guard.observe(db.conn, db_now=NOW)
    _run_id, _plan_id, rid = _enqueue(db)
    claim = claim_next_request(db.conn, "worker-1", now=NOW, guard=guard)
    assert claim is not None
    heartbeat(db.conn, rid, claim.attempt_id, now=T1)
    db.conn.execute(
        "UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (T1, rid)
    )
    db.conn.commit()
    reclaim_expired(db.conn, now=T2)
    reclaim_orphaned_epoch_work(db.conn, epoch.epoch_id, now=T2)
