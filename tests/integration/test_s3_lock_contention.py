"""Expired owners cannot write after waiting for another SQLite writer."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from jobscraper.acquisition.envelope import bind_execution_plan
from jobscraper.db.connection import Database, connect_db
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.authorization import evaluate_request_authorization, finalize_authorization_denial
from jobscraper.runtime.cancellation import abandon_request_for_cancellation, request_run_cancellation
from jobscraper.runtime.claim_control import yield_unstarted_claim
from jobscraper.runtime.claims import StaleOwnership, claim_next_request, heartbeat, _add_seconds
from jobscraper.runtime.clock import begin_service_epoch, db_utc_now, ServiceClockGuard
from jobscraper.runtime.fence import fenced_commit
from tests.integration.test_s32_authorization_fence import _seed, _envelope


@pytest.mark.parametrize('operation', [
    'heartbeat', 'guarded_heartbeat', 'commit', 'retry_commit', 'refund',
    'denial', 'cancellation', 'bind',
])
def test_expiry_while_waiting_for_write_lock_cannot_mutate(tmp_path, operation):
    db = Database(tmp_path / 'contention.db')
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    run_id, plan_id, request_id = _seed(db)
    epoch = begin_service_epoch(db.conn)
    claim = claim_next_request(db.conn, 'owner', lease_window_s=1.0)
    envelope = _envelope(db, claim, plan_id)
    guard = ServiceClockGuard(epoch)
    if operation == 'denial':
        db.conn.execute("UPDATE sources SET desired_state='DISABLED'")
    elif operation == 'cancellation':
        request_run_cancellation(db.conn, run_id)
    decision = evaluate_request_authorization(db.conn, request_id)
    before = list(db.conn.iterdump())
    contender = connect_db(db.path)
    attempting = threading.Event()
    # SQLite trace fires immediately before executing BEGIN, while another
    # connection really owns the write lock. No mocked transaction or clock.
    contender.set_trace_callback(lambda sql: attempting.set() if sql == 'BEGIN IMMEDIATE' else None)

    def transition():
        if operation in ('heartbeat', 'guarded_heartbeat'):
            return heartbeat(contender, request_id, claim.attempt_id,
                             guard=guard if operation == 'guarded_heartbeat' else None)
        if operation in ('commit', 'retry_commit'):
            with fenced_commit(contender, request_id, claim.attempt_id,
                               outcome='RETRY_WAIT' if operation == 'retry_commit' else 'SUCCEEDED',
                               retry_delay_s=5):
                contender.execute("INSERT INTO events(at,level,kind,message) VALUES (?, 'INFO', 'STALE_OUTPUT', 'must roll back')", (db_utc_now(contender),))
            return True
        if operation == 'refund':
            return yield_unstarted_claim(contender, request_id, claim.attempt_id, reason='CAPACITY_UNAVAILABLE')
        if operation == 'denial':
            return finalize_authorization_denial(contender, request_id, claim.attempt_id, decision=decision)
        if operation == 'cancellation':
            return abandon_request_for_cancellation(contender, request_id, attempt_id=claim.attempt_id)
        return bind_execution_plan(contender, envelope)

    try:
        db.conn.execute('BEGIN IMMEDIATE')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(transition)
            try:
                assert attempting.wait(2), 'contender never attempted BEGIN IMMEDIATE'
                assert db_utc_now(db.conn) < claim.lease_until, 'lease must be live when lock wait starts'
                deadline = time.monotonic() + 3
                while db_utc_now(db.conn) <= claim.lease_until:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                assert not future.done(), 'contender must still be blocked on the writer'
            finally:
                db.conn.execute('COMMIT')
            if operation in ('refund', 'denial', 'cancellation'):
                future.result(timeout=3)
                assert list(db.conn.iterdump()) == before, 'expired owner changed durable state'
            else:
                with pytest.raises(StaleOwnership):
                    future.result(timeout=3)
        assert list(db.conn.iterdump()) == before, 'expired owner changed durable state'
    finally:
        contender.close()
        db.close()


@pytest.mark.parametrize('operation', ['heartbeat', 'claim'])
@pytest.mark.parametrize('direction', ['FORWARD', 'BACKWARD'])
def test_clock_jump_during_write_lock_invalidates_old_epoch(tmp_path, monkeypatch, operation, direction):
    from jobscraper.runtime import claims as claims_module
    from jobscraper.runtime.clock import current_service_epoch

    db = Database(tmp_path / 'lock-clock.db')
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    _run_id, _plan_id, rid = _seed(db)
    initial = '2026-09-11T08:00:00.000000Z'
    clock = [initial]
    mono = [0.0]
    monkeypatch.setattr(claims_module, 'db_utc_now', lambda conn: clock[0])
    epoch = begin_service_epoch(db.conn, now=initial)
    guard = ServiceClockGuard(epoch, monotonic=lambda: mono[0])
    claim = claim_next_request(db.conn, 'old', guard=guard, lease_window_s=60)
    contender = connect_db(db.path)
    attempting = threading.Event()
    contender.set_trace_callback(lambda sql: attempting.set() if sql == 'BEGIN IMMEDIATE' else None)
    before_request = tuple(db.conn.execute('SELECT * FROM scrape_requests WHERE id=?', (rid,)).fetchone())
    before_attempt = tuple(db.conn.execute('SELECT * FROM request_attempts WHERE attempt_id=?', (claim.attempt_id,)).fetchone())
    try:
        db.conn.execute('BEGIN IMMEDIATE')
        with ThreadPoolExecutor(max_workers=1) as pool:
            if operation == 'heartbeat':
                future = pool.submit(heartbeat, contender, rid, claim.attempt_id, guard=guard)
            else:
                future = pool.submit(claim_next_request, contender, 'new', guard=guard)
            try:
                assert attempting.wait(2)
                # The lease elapses while the writer is held, followed by the
                # injected wall-clock jump. The actual Windows clock is untouched.
                mono[0] = 61.0
                clock[0] = _add_seconds(initial, 61)
                assert clock[0] > claim.lease_until
                clock[0] = _add_seconds(initial, 3600 if direction == 'FORWARD' else -3600)
                assert not future.done()
            finally:
                db.conn.execute('COMMIT')
            if operation == 'heartbeat':
                with pytest.raises(StaleOwnership):
                    future.result(timeout=3)
                assert tuple(db.conn.execute('SELECT * FROM scrape_requests WHERE id=?', (rid,)).fetchone()) == before_request
                assert tuple(db.conn.execute('SELECT * FROM request_attempts WHERE attempt_id=?', (claim.attempt_id,)).fetchone()) == before_attempt
                assert guard.claims_halted
            else:
                assert future.result(timeout=3) is None
                assert not guard.claims_halted
                assert db.conn.execute('SELECT outcome FROM request_attempts WHERE attempt_id=?', (claim.attempt_id,)).fetchone()[0] == 'ABANDONED'
                assert db.conn.execute('SELECT status FROM scrape_requests WHERE id=?', (rid,)).fetchone()[0] == 'RETRY_WAIT'
            assert current_service_epoch(db.conn).epoch_id != epoch.epoch_id
            assert db.conn.execute('SELECT end_reason FROM service_clock_epochs WHERE id=?', (epoch.epoch_id,)).fetchone()[0] == 'CLOCK_ANOMALY_' + direction
    finally:
        contender.close()
        db.close()

