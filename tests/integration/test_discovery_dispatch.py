"""S3.3/S3.4 discovery dispatch regression: SOURCE_DISCOVERY through the
service-owned HTTP dispatch seam (no direct executor calls).

Covers capacity deferral, active cooldown, authorization denial and
cancellation: no network I/O before dispatch, no consumed provider budget,
no stranded RUNNING request, durable retry/cooldown state.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.capacity import CapacityKey, service_capacity_coordinator
from jobscraper.runtime.discovery import (
    DiscoveryError,
    execute_source_discovery,
    queue_source_discovery,
)
from jobscraper.runtime.rate import RateKey, record_failure

NOW = "2026-09-10T06:30:00.000000Z"

_GENERIC_PAGE = b"<!doctype html><html><body><h1>Careers</h1><p>Join us.</p></body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802 - stdlib handler interface
        type(self).hits += 1
        body = _GENERIC_PAGE
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
        VALUES ('perm-dispatch', 'dispatch', '{NOW}');
        INSERT INTO adapter_permission_profile_revisions
            (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-dispatch', 'perm-dispatch', 1, '{{}}', '{NOW}');
        """
    )
    db.conn.commit()
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(db.conn, now=NOW)
    return db


def _queue(db, server):
    return queue_source_discovery(
        db.conn,
        display_name="Dispatch careers",
        entry_url=f"http://127.0.0.1:{server.server_address[1]}/careers/generic",
        source_family="EMPLOYER_CAREERS",
        now=NOW,
    )


def test_capacity_deferral_performs_no_io_and_preserves_attempt_budget(
    tmp_path, server
):
    """Exhausted provider capacity: no request, ABANDONED attempt, PENDING work."""
    db = _database(tmp_path / "capacity.db")
    reservations = []
    try:
        queued = _queue(db, server)
        coordinator = service_capacity_coordinator()
        key = CapacityKey(
            execution_class="HTTP",
            source_id=queued.source_id,
            host="127.0.0.1",
        )
        for _ in range(2):  # default per-source cap is 2
            reservation = coordinator.try_reserve(key)
            assert reservation is not None
            reservations.append(reservation)

        with pytest.raises(DiscoveryError, match="no provider capacity"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=NOW)

        assert _Handler.hits == 0
        request = db.conn.execute(
            "SELECT status, attempt_count, current_attempt_id"
            " FROM scrape_requests WHERE id = ?",
            (queued.request_id,),
        ).fetchone()
        assert request["status"] == "PENDING"
        assert request["attempt_count"] == 0
        assert request["current_attempt_id"] is None
        attempt = db.conn.execute(
            "SELECT outcome, abandoned_reason FROM request_attempts"
            " WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()
        assert attempt["outcome"] == "ABANDONED"
        assert attempt["abandoned_reason"] == "CAPACITY_UNAVAILABLE"
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()[0] == 0
    finally:
        for reservation in reservations:
            reservation.release()
        db.close()


def test_active_cooldown_defers_probe_to_retry_wait(tmp_path, server):
    """Persisted Retry-After: no I/O, RETRY_WAIT with the durable cooldown."""
    db = _database(tmp_path / "cooldown.db")
    try:
        queued = _queue(db, server)
        record_failure(
            db.conn,
            RateKey(queued.binding_id, "127.0.0.1", None),
            failure_kind="RATE_LIMIT",
            delay_s=120,
            retry_after_raw="120",
            now=NOW,
        )
        with pytest.raises(DiscoveryError, match="cooldown"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=NOW)

        assert _Handler.hits == 0
        request = db.conn.execute(
            "SELECT status, next_retry_at FROM scrape_requests WHERE id = ?",
            (queued.request_id,),
        ).fetchone()
        assert request["status"] == "RETRY_WAIT"
        assert request["next_retry_at"] == "2026-09-10T06:32:00.000000Z"
        run = db.conn.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (queued.run_id,)
        ).fetchone()
        assert run["status"] == "RUNNING"
    finally:
        db.close()


def test_binding_revocation_between_claim_and_dispatch_denies_probe(
    tmp_path, server, monkeypatch
):
    """Current revocation after claim: FAILED, run mirrors it, no I/O.

    The wrapper revokes inside the dispatch window (after the claim, before
    the authorization checkpoint) to deterministically exercise the exact
    race the live-authorization seam exists for.
    """
    from jobscraper.runtime import discovery as discovery_module

    real_dispatch = discovery_module.dispatch_http

    db = _database(tmp_path / "denied.db")
    try:
        queued = _queue(db, server)

        def _revoke_then_dispatch(conn, envelope, policy, **kwargs):
            conn.execute(
                "UPDATE source_adapter_bindings SET desired_state = 'DISABLED'"
                " WHERE id = ?",
                (queued.binding_id,),
            )
            conn.commit()
            return real_dispatch(conn, envelope, policy, **kwargs)

        monkeypatch.setattr(
            discovery_module, "dispatch_http", _revoke_then_dispatch
        )

        with pytest.raises(DiscoveryError, match="denied"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=NOW)

        assert _Handler.hits == 0
        request = db.conn.execute(
            "SELECT status FROM scrape_requests WHERE id = ?",
            (queued.request_id,),
        ).fetchone()
        assert request["status"] == "FAILED"
        run = db.conn.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (queued.run_id,)
        ).fetchone()
        assert run["status"] == "FAILED"
        group = db.conn.execute(
            "SELECT group_outcome FROM run_source_plans WHERE run_id = ?",
            (queued.run_id,),
        ).fetchone()
        assert group["group_outcome"] == "FAILED"
    finally:
        db.close()


def test_cancellation_between_claim_and_dispatch_abandons_probe(
    tmp_path, server, monkeypatch
):
    """Cancellation after claim: cooperatively CANCELLED, never stranded.

    Same dispatch-window injection as above, but for run cancellation: the
    attempt is abandoned (not left RUNNING) and the run closes CANCELLED.
    """
    from jobscraper.runtime import discovery as discovery_module

    real_dispatch = discovery_module.dispatch_http

    db = _database(tmp_path / "cancelled.db")
    try:
        queued = _queue(db, server)

        def _cancel_then_dispatch(conn, envelope, policy, **kwargs):
            request_run_cancellation(conn, queued.run_id, now=NOW)
            return real_dispatch(conn, envelope, policy, **kwargs)

        monkeypatch.setattr(
            discovery_module, "dispatch_http", _cancel_then_dispatch
        )

        with pytest.raises(DiscoveryError, match="denied"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=NOW)

        assert _Handler.hits == 0
        request = db.conn.execute(
            "SELECT status FROM scrape_requests WHERE id = ?",
            (queued.request_id,),
        ).fetchone()
        assert request["status"] == "CANCELLED"
        run = db.conn.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (queued.run_id,)
        ).fetchone()
        assert run["status"] == "CANCELLED"
    finally:
        db.close()


def test_lease_expiry_between_claim_and_dispatch_blocks_io(
    tmp_path, server, monkeypatch
):
    """A lease that expires after claim must fail the pre-I/O fence.

    Production-style call (`now=None`): the claim samples T1 and the
    dispatch seam samples T2 (past the 120s lease window) through
    monkeypatched DB-clock seams — the analogue of wall-clock time passing
    between claim and dispatch, without touching the system clock or
    sleeping. `bind_execution_plan` must reject ownership before any
    network I/O: zero HTTP hits, zero fetch evidence. The truly expired
    lease is consumed (RETRY_WAIT) while the run stays open for the later
    redrive proven below; nothing commits request-owned outputs.
    """
    from jobscraper.acquisition import envelope as envelope_module
    from jobscraper.runtime import claims as claims_module
    from jobscraper.runtime import discovery as discovery_module
    from jobscraper.runtime import dispatch as dispatch_module

    T1 = NOW
    T2 = "2026-09-10T07:30:00.000000Z"  # T1 + 1h, past the 120s lease window
    T3 = "2026-09-10T09:30:00.000000Z"  # past the reclaim retry window
    monkeypatch.setattr(discovery_module, "db_utc_now", lambda conn: T1)
    monkeypatch.setattr(dispatch_module, "db_utc_now", lambda conn: T2)
    monkeypatch.setattr(envelope_module, "db_utc_now", lambda conn: T2)
    monkeypatch.setattr(claims_module, "db_utc_now", lambda conn: T2)

    db = _database(tmp_path / "lease.db")
    try:
        queued = _queue(db, server)
        with pytest.raises(DiscoveryError, match="still open"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=None)

        # bind rejected ownership before network I/O
        assert _Handler.hits == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM acquisition_evidence WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()[0] == 0
        # retryable request under an open run: no false terminalization
        request = db.conn.execute(
            "SELECT status, next_retry_at FROM scrape_requests WHERE id = ?",
            (queued.request_id,),
        ).fetchone()
        assert request["status"] == "RETRY_WAIT"
        assert request["next_retry_at"] > T2
        run = db.conn.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (queued.run_id,)
        ).fetchone()
        assert run["status"] == "RUNNING"
        group = db.conn.execute(
            "SELECT group_outcome FROM run_source_plans WHERE run_id = ?",
            (queued.run_id,),
        ).fetchone()
        assert group["group_outcome"] is None

        # resumability: advance past next_retry_at and redrive; the normal
        # path completes and the run/group terminalize instead of stranding
        monkeypatch.setattr(discovery_module, "db_utc_now", lambda conn: T3)
        monkeypatch.setattr(dispatch_module, "db_utc_now", lambda conn: T3)
        monkeypatch.setattr(envelope_module, "db_utc_now", lambda conn: T3)
        monkeypatch.setattr(claims_module, "db_utc_now", lambda conn: T3)
        outcome = execute_source_discovery(db.conn, queued, worker_id="w2", now=None)
        assert _Handler.hits == 1
        assert outcome.run_status in ("SUCCEEDED", "PARTIAL")
        request = db.conn.execute(
            "SELECT status FROM scrape_requests WHERE id = ?",
            (queued.request_id,),
        ).fetchone()
        assert request["status"] in ("SUCCEEDED", "FAILED")
        run = db.conn.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (queued.run_id,)
        ).fetchone()
        # persisted terminal state agrees with the returned outcome
        assert run["status"] == outcome.run_status
        group = db.conn.execute(
            "SELECT group_outcome FROM run_source_plans WHERE run_id = ?",
            (queued.run_id,),
        ).fetchone()
        assert group["group_outcome"] is not None
    finally:
        db.close()


def test_epoch_stale_live_request_leaves_discovery_run_open(
    tmp_path, server, monkeypatch
):
    """Epoch invalidation with a live lease cannot close the discovery run.

    The epoch rotates inside the dispatch window (after claim, before the
    bind fence): the attempt is epoch-stale but its lease is live, so
    reclamation consumes nothing and the request stays RUNNING under an
    open RUNNING run with a NULL group outcome.
    """
    from jobscraper.runtime import discovery as discovery_module
    from jobscraper.runtime.clock import begin_service_epoch

    real_dispatch = discovery_module.dispatch_http

    db = _database(tmp_path / "epoch.db")
    try:
        queued = _queue(db, server)

        def _rotate_then_dispatch(conn, envelope, policy, **kwargs):
            begin_service_epoch(conn, now=NOW)
            return real_dispatch(conn, envelope, policy, **kwargs)

        monkeypatch.setattr(discovery_module, "dispatch_http", _rotate_then_dispatch)

        with pytest.raises(DiscoveryError, match="still open"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=NOW)

        assert _Handler.hits == 0
        request = db.conn.execute(
            "SELECT status, current_attempt_id FROM scrape_requests WHERE id = ?",
            (queued.request_id,),
        ).fetchone()
        assert request["status"] == "RUNNING"
        assert request["current_attempt_id"] is not None
        run = db.conn.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (queued.run_id,)
        ).fetchone()
        assert run["status"] == "RUNNING"
        group = db.conn.execute(
            "SELECT group_outcome FROM run_source_plans WHERE run_id = ?",
            (queued.run_id,),
        ).fetchone()
        assert group["group_outcome"] is None
    finally:
        db.close()


def _event_time_machine(monkeypatch, *, claim_ts, event_ts):
    """Shared mutable clock for ownership-time regressions.

    The discovery seam starts at `claim_ts` (claim/run-start history);
    wrappers advance it to `event_ts` inside the dispatch window, so
    post-claim handlers observe event time — the production analogue of
    time passing, without touching the system clock. All other ownership
    namespaces are fixed at `event_ts` (they are only read post-claim).
    """
    from jobscraper.acquisition import envelope as envelope_module
    from jobscraper.runtime import claims as claims_module
    from jobscraper.runtime import discovery as discovery_module
    from jobscraper.runtime import dispatch as dispatch_module
    from jobscraper.runtime import fence as fence_module

    state = {"t": claim_ts}
    monkeypatch.setattr(discovery_module, "db_utc_now", lambda conn: state["t"])
    monkeypatch.setattr(dispatch_module, "db_utc_now", lambda conn: event_ts)
    monkeypatch.setattr(envelope_module, "db_utc_now", lambda conn: event_ts)
    monkeypatch.setattr(claims_module, "db_utc_now", lambda conn: event_ts)
    monkeypatch.setattr(fence_module, "db_utc_now", lambda conn: event_ts)
    return state


def _request_row(db, request_id):
    return db.conn.execute(
        "SELECT status, current_attempt_id, attempt_count, next_retry_at"
        " FROM scrape_requests WHERE id = ?",
        (request_id,),
    ).fetchone()


def _run_open(db, run_id):
    run = db.conn.execute(
        "SELECT status FROM scrape_runs WHERE id = ?", (run_id,)
    ).fetchone()
    group = db.conn.execute(
        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return run["status"] == "RUNNING" and group["group_outcome"] is None


def test_capacity_deferral_after_lease_expiry_refunds_nothing(
    tmp_path, server, monkeypatch
):
    """Expired lease + capacity pressure: no refund, no evidence, run open."""
    from jobscraper.runtime.capacity import CapacityKey, service_capacity_coordinator

    T1 = NOW
    T2 = "2026-09-10T07:30:00.000000Z"  # past the 120s lease window
    clock = _event_time_machine(monkeypatch, claim_ts=T1, event_ts=T2)

    from jobscraper.runtime import discovery as discovery_module

    real_dispatch = discovery_module.dispatch_http

    def _advance_then_dispatch(conn, envelope, policy, **kwargs):
        clock["t"] = T2
        return real_dispatch(conn, envelope, policy, **kwargs)

    monkeypatch.setattr(discovery_module, "dispatch_http", _advance_then_dispatch)

    db = _database(tmp_path / "cap-expired.db")
    reservations = []
    try:
        queued = _queue(db, server)
        coordinator = service_capacity_coordinator()
        key = CapacityKey(
            execution_class="HTTP",
            source_id=queued.source_id,
            host="127.0.0.1",
        )
        for _ in range(2):
            reservations.append(coordinator.try_reserve(key))

        with pytest.raises(DiscoveryError, match="no provider capacity"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=None)

        assert _Handler.hits == 0
        # the stale attempt could not be refunded with T1: still RUNNING,
        # budget untouched (claim minted attempt 1, no decrement restored it)
        request = _request_row(db, queued.request_id)
        assert request["status"] == "RUNNING"
        assert request["current_attempt_id"] is not None
        assert request["attempt_count"] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM acquisition_evidence WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()[0] == 0
        assert _run_open(db, queued.run_id)
        # later recovery remains authoritative for the expired lease
        from jobscraper.runtime.claims import reclaim_expired

        assert queued.request_id in reclaim_expired(db.conn, now=T2)
    finally:
        for reservation in reservations:
            reservation.release()
        db.close()


def test_cooldown_deferral_after_lease_expiry_mutates_nothing(
    tmp_path, server, monkeypatch
):
    """Expired lease + active cooldown: no RETRY_WAIT self-mutation."""
    T1 = NOW
    T2 = "2026-09-10T07:00:00.000000Z"  # past lease, inside the 1h cooldown
    clock = _event_time_machine(monkeypatch, claim_ts=T1, event_ts=T2)

    from jobscraper.runtime import discovery as discovery_module

    real_dispatch = discovery_module.dispatch_http

    def _advance_then_dispatch(conn, envelope, policy, **kwargs):
        clock["t"] = T2
        return real_dispatch(conn, envelope, policy, **kwargs)

    monkeypatch.setattr(discovery_module, "dispatch_http", _advance_then_dispatch)

    db = _database(tmp_path / "cool-expired.db")
    try:
        queued = _queue(db, server)
        record_failure(
            db.conn,
            RateKey(queued.binding_id, "127.0.0.1", None),
            failure_kind="RATE_LIMIT",
            delay_s=3600,
            retry_after_raw="3600",
            now=T1,
        )
        with pytest.raises(DiscoveryError, match="cooldown"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=None)

        assert _Handler.hits == 0
        # the expired worker could not park itself RETRY_WAIT with stale T1
        request = _request_row(db, queued.request_id)
        assert request["status"] == "RUNNING"
        assert request["next_retry_at"] is None
        assert _run_open(db, queued.run_id)
        from jobscraper.runtime.claims import reclaim_expired

        assert queued.request_id in reclaim_expired(db.conn, now=T2)
    finally:
        db.close()


def test_terminal_denial_after_lease_expiry_commits_nothing(
    tmp_path, server, monkeypatch
):
    """Expired lease + dispatch rejection: denial fence cannot commit stale."""
    from jobscraper.runtime import discovery as discovery_module
    from jobscraper.runtime.claims import claim_next_request

    T1 = NOW
    T2 = "2026-09-10T07:30:00.000000Z"
    clock = _event_time_machine(monkeypatch, claim_ts=T1, event_ts=T2)

    db = _database(tmp_path / "denied-expired.db")
    try:
        queued = _queue(db, server)
        claim = claim_next_request(
            db.conn, "w", now=T1,
            types=frozenset({"SOURCE_DISCOVERY"}),
            run_source_plan_id=queued.run_source_plan_id,
        )
        assert claim.request_id == queued.request_id
        clock["t"] = T2

        with pytest.raises(DiscoveryError, match="ownership was lost"):
            discovery_module._terminalize_discovery_denied(
                db.conn, queued, claim,
                failure_kind="POLICY_REJECTED",
                detail={"reason": "DISPATCH_PLAN_INVALID"},
                now=None,  # production: fresh event time (T2), not T1
            )

        assert _Handler.hits == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM acquisition_evidence WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()[0] == 0
        request = _request_row(db, queued.request_id)
        assert request["status"] == "RUNNING"
        # no execute ran, so the run was never started: QUEUED untouched,
        # group open, nothing falsely closed
        run = db.conn.execute(
            "SELECT status FROM scrape_runs WHERE id = ?", (queued.run_id,)
        ).fetchone()
        assert run["status"] == "QUEUED"
        group = db.conn.execute(
            "SELECT group_outcome FROM run_source_plans WHERE run_id = ?",
            (queued.run_id,),
        ).fetchone()
        assert group["group_outcome"] is None
    finally:
        db.close()


def test_executor_error_after_lease_expiry_commits_no_retry(
    tmp_path, server, monkeypatch
):
    """Lease expiring during I/O: local-retry fence commits nothing stale."""
    from jobscraper.runtime import discovery as discovery_module
    from jobscraper.runtime.dispatch import DispatchExecutionError

    T1 = NOW
    T2 = "2026-09-10T07:30:00.000000Z"
    clock = _event_time_machine(monkeypatch, claim_ts=T1, event_ts=T2)

    def _blow_up_after_io_window(conn, envelope, policy, **kwargs):
        clock["t"] = T2
        raise DispatchExecutionError("SimulatedBlowup")

    monkeypatch.setattr(discovery_module, "dispatch_http", _blow_up_after_io_window)

    db = _database(tmp_path / "exec-expired.db")
    try:
        queued = _queue(db, server)
        with pytest.raises(DiscoveryError, match="committed nothing"):
            execute_source_discovery(db.conn, queued, worker_id="w", now=None)

        assert _Handler.hits == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM acquisition_evidence WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()[0] == 0
        request = _request_row(db, queued.request_id)
        assert request["status"] == "RUNNING"
        assert _run_open(db, queued.run_id)
        # later recovery/reclaim remains authoritative
        from jobscraper.runtime.claims import reclaim_expired

        assert queued.request_id in reclaim_expired(db.conn, now=T2)
    finally:
        db.close()
