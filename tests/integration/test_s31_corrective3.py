"""S3.1 corrective III regressions for §50 ownership safety.

These tests pin two service-lifetime guarantees that must hold before S3.1
can close:

* a guarded heartbeat observes the service clock before renewing a lease, so
  a material wall-clock anomaly invalidates ownership instead of extending it;
* restart recovery is a startup gate, not best-effort diagnostics: if recovery
  fails, the service must fail closed before it can serve or provision work.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.claims import StaleOwnership, claim_next_request, heartbeat
from jobscraper.runtime.clock import ServiceClockGuard, begin_service_epoch
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-08T08:00:00.000000Z"
FUTURE = "2026-09-08T09:00:00.000000Z"


def _family(db: Database) -> None:
    conn = db.conn
    conn.execute(
        "INSERT INTO sources (id, display_name, source_family, entry_url, desired_state,"
        " administrative_state, created_at, updated_at)"
        " VALUES ('src-1','Feed','PUBLIC_FEED','https://example.test/feed',"
        " 'ENABLED','NORMAL',?,?)",
        (NOW, NOW),
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
        " administrative_state, created_at)"
        " VALUES ('bnd-1','src-1','api','ENABLED','NORMAL',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions (id, binding_id, revision, adapter_id,"
        " adapter_version, strategy, execution_class, permission_profile_id,"
        " permission_profile_revision, created_at)"
        " VALUES ('bndrev-1','bnd-1',1,'feed','1.0.0',"
        " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?)",
        (NOW,),
    )
    conn.commit()


def _enqueue(db: Database) -> str:
    plan = dict(
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
    run_id, plans = create_run(db.conn, profile_id=None, plans=[plan], now=NOW)
    request_id, _created = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plans[0],
        source_id="src-1",
        binding_id="bnd-1",
        request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
        now=NOW,
    )
    return request_id


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "s31-corrective3.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    _family(database)
    yield database
    try:
        database.close()
    except Exception:
        pass


def test_guarded_heartbeat_detects_backward_clock_jump_before_lease_renewal(db):
    ticks = iter((0.0, 1.0))
    epoch = begin_service_epoch(db.conn, now=FUTURE)
    guard = ServiceClockGuard(epoch, tolerance_s=5.0, monotonic=lambda: next(ticks))
    request_id = _enqueue(db)

    # The guarded claim establishes the clock baseline at FUTURE. Its lease
    # expires after FUTURE, so when the wall clock jumps backward to NOW the
    # lease still appears unexpired to a naive DB-time-only heartbeat.
    claim = claim_next_request(db.conn, "worker-1", now=FUTURE, guard=guard)
    assert claim is not None and claim.request_id == request_id
    old_lease = claim.lease_until
    assert old_lease > FUTURE

    # No intervening claim occurs. The heartbeat itself must observe the
    # material backward jump, rotate the epoch and reject this old owner.
    # Without the guard seam this heartbeat would be accepted because
    # old_lease > NOW, silently preserving ownership across the anomaly.
    with pytest.raises(StaleOwnership):
        heartbeat(
            db.conn,
            request_id,
            claim.attempt_id,
            now=NOW,
            guard=guard,
        )

    row = db.conn.execute(
        "SELECT lease_until FROM scrape_requests WHERE id = ?", (request_id,)
    ).fetchone()
    old_epoch = db.conn.execute(
        "SELECT ended_at, end_reason FROM service_clock_epochs WHERE id = ?",
        (epoch.epoch_id,),
    ).fetchone()
    assert row["lease_until"] == old_lease
    assert guard.claims_halted
    assert old_epoch["end_reason"] == "CLOCK_ANOMALY_BACKWARD"


def test_run_service_fails_closed_when_restart_recovery_fails(tmp_path, monkeypatch):
    """The listener must never become serviceable after failed ownership recovery."""
    import jobscraper.runtime.recovery as recovery_mod
    import jobscraper.search.provision as provision_mod
    import jobscraper.service.runner as runner
    import uvicorn

    database = Database(tmp_path / "service.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)

    class FakeSocket:
        def __init__(self):
            self.closed = False

        def getsockname(self):
            return ("127.0.0.1", 43123)

        def close(self):
            self.closed = True

    sock = FakeSocket()

    class FakeLifespan:
        def __init__(self, *args, **kwargs):
            pass

        async def start_background(self):
            pass

        async def stop_background(self):
            pass

        def remove_descriptor(self):
            pass

        def emit_stopped(self):
            pass

        def emit_crashed(self, _message):
            pass

        def build_descriptor(self, **kwargs):
            return kwargs

        def publish_descriptor(self, _desc):
            pass

    class FakeServer:
        def __init__(self, _config):
            self.started = False
            self.should_exit = False

        async def serve(self, *, sockets):
            self.should_exit = True

    monkeypatch.setattr(runner, "open_service_database", lambda _config: database)
    monkeypatch.setattr(runner, "_bind_loopback_socket", lambda _host: sock)
    monkeypatch.setattr(
        runner,
        "create_service_app",
        lambda *_args, **_kwargs: (FastAPI(), SimpleNamespace(instance_id="svc-test")),
    )
    monkeypatch.setattr(runner, "ServiceLifespan", FakeLifespan)

    def fail_recovery(*_args, **_kwargs):
        raise RuntimeError("synthetic restart-recovery failure")

    monkeypatch.setattr(recovery_mod, "recover_interrupted_requests", fail_recovery)
    monkeypatch.setattr(provision_mod, "provision_search", lambda *_a, **_k: None)
    monkeypatch.setattr(uvicorn, "Config", lambda *_a, **_k: object())
    monkeypatch.setattr(uvicorn, "Server", FakeServer)

    code = runner.run_service(
        AppConfig(data_root=tmp_path),
        install_secret=b"\x11" * 32,
    )

    assert code == 4
    assert sock.closed
