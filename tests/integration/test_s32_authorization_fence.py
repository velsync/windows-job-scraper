"""S3.2 authorization/fence regressions.  GENERATED ONLY; not executed."""

from __future__ import annotations

import pytest

from jobscraper.acquisition.envelope import (
    ExecutionPlanEnvelope,
    RequestPlan,
    bind_execution_plan,
    policy_snapshot_reference,
)
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.authorization import (
    AuthorizationDenied,
    evaluate_request_authorization,
    finalize_authorization_denial,
)
from jobscraper.runtime.cancellation import (
    abandon_request_for_cancellation,
    request_run_cancellation,
)
from jobscraper.runtime.claims import StaleOwnership, claim_next_request
from jobscraper.runtime.clock import begin_service_epoch
from jobscraper.runtime.fence import FenceTransition, fenced_commit
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run

NOW = "2026-09-11T08:00:00.000000Z"


def _seed(db: Database) -> tuple[str, str, str]:
    c = db.conn
    c.execute(
        "INSERT INTO sources (id,display_name,source_family,entry_url,desired_state,"
        "administrative_state,created_at,updated_at) VALUES "
        "('src','S','PUBLIC_FEED','https://example.test/jobs','ENABLED','NORMAL',?,?)",
        (NOW, NOW),
    )
    c.execute(
        "INSERT INTO adapter_definitions (adapter_id,adapter_version,adapter_api_version,"
        "manifest_json,created_at) VALUES ('feed','1','1','{}',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO adapter_permission_profiles (id,display_name,created_at)"
        " VALUES ('perm','P',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO adapter_permission_profile_revisions"
        " (id,permission_profile_id,revision,policy_json,created_at)"
        " VALUES ('perm-r1','perm',1,'{}',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO source_adapter_bindings"
        " (id,source_id,display_name,desired_state,administrative_state,created_at)"
        " VALUES ('bnd','src','B','ENABLED','NORMAL',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id,binding_id,revision,adapter_id,adapter_version,strategy,auth_requirement,"
        "execution_class,permission_profile_id,permission_profile_revision,created_at)"
        " VALUES ('bnd-r1','bnd',1,'feed','1','FEED_OR_PUBLIC_STRUCTURED_ENDPOINT',"
        "'NONE','HTTP','perm',1,?)",
        (NOW,),
    )
    c.commit()
    plan = {
        "source_id": "src",
        "source_plan_group_id": "grp",
        "fallback_rank": 0,
        "binding_id": "bnd",
        "binding_revision_id": "bnd-r1",
        "adapter_id": "feed",
        "adapter_version": "1",
        "adapter_api_version": "1",
        "strategy": "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        "execution_class": "HTTP",
        "permission_profile_id": "perm",
        "permission_profile_revision": 1,
    }
    run_id, plans = create_run(c, profile_id=None, plans=[plan], now=NOW)
    req, _ = enqueue_request(
        c,
        run_id=run_id,
        run_source_plan_id=plans[0],
        source_id="src",
        binding_id="bnd",
        request_type="LIST_FETCH",
        target_identity="https://example.test/jobs",
        strategy=plan["strategy"],
        execution_class="HTTP",
        now=NOW,
    )
    return run_id, plans[0], req


def _envelope(db: Database, claim, plan_id: str) -> ExecutionPlanEnvelope:
    row = db.conn.execute("SELECT * FROM run_source_plans WHERE id = ?", (plan_id,)).fetchone()
    return ExecutionPlanEnvelope(
        plan_id="plan-test",
        request_id=claim.request_id,
        attempt_id=claim.attempt_id,
        run_id=claim.run_id,
        run_source_plan_id=plan_id,
        source_id=row["source_id"],
        binding_id=row["binding_id"],
        binding_revision_id=row["binding_revision_id"],
        adapter_id=row["adapter_id"],
        adapter_version=row["adapter_version"],
        strategy=row["strategy"],
        execution_class=row["execution_class"],
        policy_snapshot_ref=policy_snapshot_reference(row),
        permission_profile_id=row["permission_profile_id"],
        permission_profile_revision=row["permission_profile_revision"],
        payload_kind="REQUEST",
        payload=RequestPlan("GET", "https://example.test/jobs", purpose="LIST_FETCH"),
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "s32.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    begin_service_epoch(database.conn, now=NOW)
    yield database
    database.close()


def test_execution_plan_identity_is_persisted_before_dispatch(db):
    _run, plan_id, request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    assert claim and claim.request_id == request_id
    env = _envelope(db, claim, plan_id)
    bind_execution_plan(db.conn, env, now=NOW)
    row = db.conn.execute(
        "SELECT execution_plan_id FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert row["execution_plan_id"] == env.plan_id


def test_malformed_envelope_cannot_replace_pinned_binding_identity(db):
    _run, plan_id, _request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    env = _envelope(db, claim, plan_id)
    malformed = ExecutionPlanEnvelope(**{**env.__dict__, "binding_revision_id": "other"})
    with pytest.raises(ValueError):
        bind_execution_plan(db.conn, malformed, now=NOW)
    row = db.conn.execute(
        "SELECT execution_plan_id FROM request_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    ).fetchone()
    assert row["execution_plan_id"] is None


def test_permission_revocation_between_dispatch_and_commit_rolls_back_outputs(db):
    _run, plan_id, _request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    env = _envelope(db, claim, plan_id)
    bind_execution_plan(db.conn, env, now=NOW)
    db.conn.execute(
        "UPDATE adapter_permission_profiles SET administrative_state='QUARANTINED'"
        " WHERE id='perm'"
    )
    db.conn.commit()

    def mutate(conn):
        conn.execute(
            "INSERT INTO events(at,level,kind,message,data_json,request_id)"
            " VALUES (?,?,?,?,?,?)",
            (NOW, "INFO", "SHOULD_ROLL_BACK", "x", "{}", claim.request_id),
        )

    with pytest.raises(AuthorizationDenied):
        with fenced_commit(db.conn, claim.request_id, claim.attempt_id, now=NOW, mutate=mutate):
            pass
    assert db.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='SHOULD_ROLL_BACK'"
    ).fetchone()[0] == 0


def test_cancellation_does_not_cancel_existing_host_native_obligation(db):
    run_id, plan_id, _request_id = _seed(db)
    native_id, _ = enqueue_request(
        db.conn,
        run_id=run_id,
        run_source_plan_id=plan_id,
        source_id="src",
        binding_id="bnd",
        request_type="NORMALIZE",
        target_identity="obs-1",
        now=NOW,
    )
    request_run_cancellation(db.conn, run_id, now=NOW)
    claim = claim_next_request(db.conn, "svc", now=NOW, types=frozenset({"NORMALIZE"}))
    assert claim and claim.request_id == native_id
    with fenced_commit(db.conn, claim.request_id, claim.attempt_id, now=NOW):
        pass
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (native_id,)
    ).fetchone()["status"] == "SUCCEEDED"


def test_revocation_between_claim_and_bind_blocks_dispatch_identity(db):
    _run, plan_id, _request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    env = _envelope(db, claim, plan_id)
    db.conn.execute(
        "UPDATE adapter_permission_profiles SET administrative_state='QUARANTINED'"
        " WHERE id='perm'"
    )
    db.conn.commit()
    with pytest.raises(AuthorizationDenied):
        bind_execution_plan(db.conn, env, now=NOW)
    assert db.conn.execute(
        "SELECT execution_plan_id FROM request_attempts WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()[0] is None


def test_epoch_advance_between_claim_and_bind_blocks_dispatch_identity(db):
    _run, plan_id, _request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    env = _envelope(db, claim, plan_id)
    begin_service_epoch(db.conn, now="2026-09-11T08:00:01.000000Z")
    with pytest.raises(StaleOwnership):
        bind_execution_plan(db.conn, env, now="2026-09-11T08:00:01.000000Z")
    assert db.conn.execute(
        "SELECT execution_plan_id FROM request_attempts WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()[0] is None


def test_denial_finalizer_cannot_close_stale_epoch_attempt(db):
    _run, _plan_id, request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    db.conn.execute(
        "UPDATE adapter_permission_profiles SET administrative_state='QUARANTINED'"
        " WHERE id='perm'"
    )
    db.conn.commit()
    decision = evaluate_request_authorization(
        db.conn, request_id, attempt_id=claim.attempt_id, require_running=True
    )
    assert not decision.allowed
    begin_service_epoch(db.conn, now="2026-09-11T08:00:01.000000Z")
    assert not finalize_authorization_denial(
        db.conn, request_id, claim.attempt_id,
        decision=decision, now="2026-09-11T08:00:01.000000Z",
    )
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id=?", (request_id,)
    ).fetchone()[0] == "RUNNING"


def test_post_mutation_partial_transition_cannot_finish_succeeded(db):
    _run, _plan_id, request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    selected = {"value": FenceTransition()}

    def mutate(conn):
        conn.execute(
            "INSERT INTO events(at,level,kind,message,data_json,request_id)"
            " VALUES (?,?,?,?,?,?)",
            (NOW, "INFO", "PARTIAL_ACCEPTED", "x", "{}", request_id),
        )
        selected["value"] = FenceTransition(
            outcome="FAILED",
            failure_json='{"kind":"PARTIAL"}',
        )

    with fenced_commit(
        db.conn, request_id, claim.attempt_id, now=NOW,
        mutate=mutate, transition_resolver=lambda: selected["value"],
    ):
        pass
    row = db.conn.execute(
        "SELECT status,last_failure_json FROM scrape_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    assert row["status"] == "FAILED"
    assert "PARTIAL" in row["last_failure_json"]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='PARTIAL_ACCEPTED'"
    ).fetchone()[0] == 1


def test_cancellation_helper_never_cancels_host_native_owner(db):
    run_id, plan_id, _request_id = _seed(db)
    native_id, _ = enqueue_request(
        db.conn, run_id=run_id, run_source_plan_id=plan_id,
        source_id="src", binding_id="bnd", request_type="NORMALIZE",
        target_identity="obs-2", now=NOW,
    )
    claim = claim_next_request(
        db.conn, "svc", now=NOW, types=frozenset({"NORMALIZE"})
    )
    assert claim and claim.request_id == native_id
    abandon_request_for_cancellation(
        db.conn, native_id, attempt_id=claim.attempt_id, now=NOW
    )
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id=?", (native_id,)
    ).fetchone()[0] == "RUNNING"


def test_denial_finalizer_rechecks_restored_current_authority(db):
    _run, _plan_id, request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    db.conn.execute(
        "UPDATE adapter_permission_profiles SET administrative_state='QUARANTINED'"
        " WHERE id='perm'"
    )
    db.conn.commit()
    decision = evaluate_request_authorization(
        db.conn, request_id, attempt_id=claim.attempt_id, require_running=True
    )
    assert not decision.allowed
    db.conn.execute(
        "UPDATE adapter_permission_profiles SET administrative_state='NORMAL'"
        " WHERE id='perm'"
    )
    db.conn.commit()
    assert not finalize_authorization_denial(
        db.conn, request_id, claim.attempt_id, decision=decision, now=NOW
    )
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id=?", (request_id,)
    ).fetchone()[0] == "RUNNING"


def test_expired_lease_cannot_bind_execution_identity(db):
    _run, plan_id, request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW, lease_window_s=1)
    assert claim and claim.request_id == request_id
    env = _envelope(db, claim, plan_id)
    with pytest.raises(StaleOwnership):
        bind_execution_plan(
            db.conn, env, now="2026-09-11T08:00:02.000000Z"
        )
    assert db.conn.execute(
        "SELECT execution_plan_id FROM request_attempts WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()[0] is None


def test_attempt_cannot_bind_execution_plan_twice(db):
    _run, plan_id, request_id = _seed(db)
    claim = claim_next_request(db.conn, "svc", now=NOW)
    assert claim and claim.request_id == request_id
    env = _envelope(db, claim, plan_id)
    bind_execution_plan(db.conn, env, now=NOW)
    with pytest.raises(StaleOwnership):
        bind_execution_plan(db.conn, env, now=NOW)
    assert db.conn.execute(
        "SELECT execution_plan_id FROM request_attempts WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()[0] == env.plan_id
