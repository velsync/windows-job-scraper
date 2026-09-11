"""N3-A Windows checkpoint probe (development evidence, not promotion).

Real OS processes + real SQLite file DBs, deterministic timestamps only.
Never touches the Windows system clock. Exit 0 iff all six scenarios PASS.
"""
from __future__ import annotations

import json
import platform
import tempfile
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PY = Path(sys.executable)
WORK = None  # set by main or the isolated automated test fixture

sys.path.insert(0, str(REPO / "src"))

from jobscraper.db.connection import (  # noqa: E402
    connect_db,
    read_sqlite_settings,
    verify_sqlite_settings,
)
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema  # noqa: E402
from jobscraper.runtime.cancellation import request_run_cancellation  # noqa: E402
from jobscraper.runtime.claims import (  # noqa: E402
    StaleOwnership,
    claim_next_request,
    heartbeat,
)
from jobscraper.runtime.clock import begin_service_epoch  # noqa: E402
from jobscraper.runtime.fence import fenced_commit  # noqa: E402
from jobscraper.runtime.rate import RateKey, dispatch_gate, record_failure  # noqa: E402
from jobscraper.runtime.recovery import recover_interrupted_requests  # noqa: E402
from jobscraper.runtime.requests import enqueue_request  # noqa: E402
from jobscraper.runtime.runs import create_run  # noqa: E402

NOW = "2026-09-11T08:00:00.000000Z"
T1 = "2026-09-11T08:01:00.000000Z"
T2 = "2026-09-11T08:02:00.000000Z"
T3 = "2026-09-11T08:03:00.000000Z"

results: dict[str, str] = {}
clock_evidence: list[dict] = []
doctor_evidence: dict = {}


def note(check: str, ok: bool, detail: str) -> None:
    results[check] = ("PASS" if ok else "FAIL") + " | " + detail
    print(f"{check}: {results[check]}", flush=True)


def fresh_db(name: str) -> Path:
    path = WORK / f"{name}.db"
    if path.exists():
        raise FileExistsError(path)  # preserve earlier checkpoint evidence
    return path


def setup(path: Path) -> None:
    conn = connect_db(path)
    migrate_schema(conn, LATEST_SCHEMA_VERSION)
    conn.execute(
        "INSERT INTO sources (id, display_name, source_family, entry_url,"
        " desired_state, administrative_state, created_at, updated_at)"
        " VALUES ('src-1','Feed','PUBLIC_FEED','https://example.test/feed',"
        " 'ENABLED','NORMAL',?,?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO adapter_definitions (adapter_id, adapter_version,"
        " adapter_api_version, manifest_json, created_at)"
        " VALUES ('feed','1.0.0','1','{}',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles (id, display_name, created_at)"
        " VALUES ('perm-1','default',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions (id,"
        " permission_profile_id, revision, policy_json, created_at)"
        " VALUES ('permrev-1','perm-1',1,'{}',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO source_adapter_bindings (id, source_id, display_name,"
        " desired_state, administrative_state, created_at)"
        " VALUES ('bnd-1','src-1','api','ENABLED','NORMAL',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions (id, binding_id, revision,"
        " adapter_id, adapter_version, strategy, execution_class,"
        " permission_profile_id, permission_profile_revision, created_at)"
        " VALUES ('bndrev-1','bnd-1',1,'feed','1.0.0',"
        " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?)",
        (NOW,),
    )
    conn.commit()
    conn.close()


def plan() -> dict:
    return dict(
        source_id="src-1", source_plan_group_id="grp-1", fallback_rank=0,
        binding_id="bnd-1", binding_revision_id="bndrev-1", adapter_id="feed",
        adapter_version="1.0.0", adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", execution_class="HTTP",
        permission_profile_id="perm-1", permission_profile_revision=1,
    )


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=120, **kw)


def clock_anomaly_proof(direction: str, *, expired_before_jump: bool, max_attempts: int) -> dict:
    """Exercise production ServiceClockGuard with injected DB/monotonic samples."""
    from jobscraper.runtime.clock import ServiceClockGuard, current_service_epoch
    from jobscraper.runtime.claims import _add_seconds

    path = fresh_db(f'clock-{direction}-{expired_before_jump}-{max_attempts}')
    setup(path)
    conn = connect_db(path)
    try:
        epoch = begin_service_epoch(conn, now=T2)
        mono = [0.0]
        guard = ServiceClockGuard(epoch, tolerance_s=30, monotonic=lambda: mono[0])
        run_id, plans = create_run(conn, profile_id=None, plans=[plan()], now=T2)
        rid, _ = enqueue_request(
            conn, run_id=run_id, run_source_plan_id=plans[0], source_id='src-1',
            binding_id='bnd-1', request_type='LIST_FETCH',
            target_identity='https://example.test/feed', max_attempts=max_attempts,
        )
        claim = claim_next_request(conn, 'old-owner', now=T2, guard=guard, lease_window_s=60)
        assert claim is not None

        def refuse_old(at):
            before = list(conn.iterdump())
            for operation in ('heartbeat', 'commit'):
                try:
                    if operation == 'heartbeat':
                        heartbeat(conn, rid, claim.attempt_id, now=at)
                    else:
                        with fenced_commit(conn, rid, claim.attempt_id, now=at):
                            conn.execute("INSERT INTO events(at,level,kind,message) VALUES (?, 'INFO', 'STALE_CLOCK_OUTPUT', 'forbidden')", (at,))
                except StaleOwnership:
                    pass
                else:
                    raise AssertionError(f'{direction}: stale {operation} accepted')
            assert list(conn.iterdump()) == before

        if expired_before_jump:
            mono[0] = 61.0
            expired_at = _add_seconds(T2, 61)
            assert guard.observe(conn, db_now=expired_at) is None
            assert expired_at > claim.lease_until
            refuse_old(expired_at)

        jumped = _add_seconds(T2, 3600 if direction == 'FORWARD' else -3600)
        anomaly = guard.observe(conn, db_now=jumped)
        assert anomaly is not None and anomaly.direction == direction
        assert guard.claims_halted
        assert anomaly.invalidated_epoch_id == epoch.epoch_id
        assert anomaly.new_epoch_id != epoch.epoch_id
        assert guard.epoch.epoch_id == current_service_epoch(conn).epoch_id == anomaly.new_epoch_id
        old_epoch = conn.execute('SELECT ended_at,end_reason FROM service_clock_epochs WHERE id=?', (epoch.epoch_id,)).fetchone()
        assert tuple(old_epoch) == (jumped, 'CLOCK_ANOMALY_' + direction)
        assert conn.execute('SELECT COUNT(*) FROM request_attempts').fetchone()[0] == 1
        refuse_old(jumped)  # backward time must not make the old lease live again

        # The guarded coordinator must recover the invalidated epoch before
        # allowing a new attempt. A retry remains in backoff at this instant.
        assert claim_next_request(conn, 'new-owner', now=jumped, guard=guard) is None
        assert not guard.claims_halted
        row = conn.execute('SELECT status,next_retry_at,attempt_count FROM scrape_requests WHERE id=?', (rid,)).fetchone()
        old_attempt = conn.execute('SELECT outcome,abandoned_reason,lease_expires_at FROM request_attempts WHERE attempt_id=?', (claim.attempt_id,)).fetchone()
        assert tuple(old_attempt) == ('ABANDONED', 'CLOCK_ANOMALY', claim.lease_until)
        assert row['attempt_count'] == 1
        expected = 'FAILED' if max_attempts == 1 else 'RETRY_WAIT'
        assert row['status'] == expected
        fresh = None
        if max_attempts > 1:
            assert row['next_retry_at'] > jumped
            fresh = claim_next_request(conn, 'new-owner', now=row['next_retry_at'], guard=guard)
            assert fresh is not None and fresh.attempt_id != claim.attempt_id
            assert fresh.service_epoch_id == anomaly.new_epoch_id
            refuse_old(row['next_retry_at'])
        else:
            refuse_old(jumped)
        assert conn.execute('SELECT COUNT(*) FROM events WHERE kind=\'STALE_CLOCK_OUTPUT\'').fetchone()[0] == 0
        return {
            'result': 'PASS', 'direction': direction,
            'expired_before_jump': expired_before_jump, 'max_attempts': max_attempts,
            'invalidated_epoch_id': epoch.epoch_id, 'new_epoch_id': anomaly.new_epoch_id,
            'old_attempt_id': claim.attempt_id, 'new_attempt_id': fresh.attempt_id if fresh else None,
            'observed_db_now': jumped, 'drift_s': anomaly.drift_s,
            'claims_halted_before_recovery': True, 'resumed_only_after_recovery': True,
            'old_heartbeat_and_commit_rejected': True, 'stale_durable_mutations': 0,
            'old_attempt_outcome': old_attempt['outcome'], 'recovered_request_status': expected,
            'expired_lease_never_revived': True, 'system_clock_changed': False,
        }
    finally:
        conn.close()


# ---------------- N3-A-01: two real processes race one claim ----------------
def n3a_01() -> None:
    path = fresh_db("n3a01")
    setup(path)
    conn = connect_db(path)
    begin_service_epoch(conn, now=NOW)
    run_id, plans = create_run(conn, profile_id=None, plans=[plan()], now=NOW)
    rid, _ = enqueue_request(
        conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    conn.commit()
    conn.close()
    procs = [
        subprocess.Popen(
            [str(PY), str(HERE / "w_claim.py"), str(path), f"w{i}", NOW],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for i in range(2)
    ]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    winners = [o for o in outs if o.startswith("CLAIMED")]
    conn = connect_db(path)
    atts = conn.execute(
        "SELECT COUNT(*) FROM request_attempts WHERE request_id = ?", (rid,)
    ).fetchone()[0]
    conn.close()
    ok = len(winners) == 1 and atts == 1
    note("N3-A-01", ok, f"outputs={outs} attempts={atts}")


# -------- N3-A-02: expired lease cannot heartbeat or commit --------
def n3a_02() -> None:
    path = fresh_db("n3a02")
    setup(path)
    conn = connect_db(path)
    epoch = begin_service_epoch(conn, now=NOW)
    run_id, plans = create_run(conn, profile_id=None, plans=[plan()], now=NOW)
    rid, _ = enqueue_request(
        conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed",
    )
    claim = claim_next_request(conn, "worker-1", now=NOW, epoch=epoch)
    conn.execute(
        "UPDATE scrape_requests SET lease_until = ? WHERE id = ?", (T1, rid)
    )
    conn.commit()
    hb_err = cm_err = None
    try:
        heartbeat(conn, rid, claim.attempt_id, now=T2, epoch=epoch)
    except Exception as e:  # noqa: BLE001
        hb_err = f"{type(e).__name__}"
    try:
        with fenced_commit(conn, rid, claim.attempt_id, now=T2):
            pass
    except Exception as e:  # noqa: BLE001
        cm_err = f"{type(e).__name__}"
    obs = conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0]
    status = conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()[0]
    conn.close()
    ok = hb_err == "StaleOwnership" and cm_err == "StaleOwnership" and obs == 0
    note("N3-A-02", ok,
         f"heartbeat={hb_err} commit={cm_err} observations={obs} status={status}")


# -------- N3-A-03: kill -9 holder, restart, fresh epoch reclaims --------
def n3a_03() -> None:
    path = fresh_db("n3a03")
    setup(path)
    conn = connect_db(path)
    epoch1 = begin_service_epoch(conn, now=NOW)
    run_id, plans = create_run(conn, profile_id=None, plans=[plan()], now=NOW)
    rid, _ = enqueue_request(
        conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed", max_attempts=3,
    )
    conn.commit()
    conn.close()
    holder = subprocess.Popen(
        [str(PY), str(HERE / "w_hold.py"), str(path), "holder", NOW],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    attempt_id = None
    deadline = time.time() + 60
    assert holder.stdout is not None
    while time.time() < deadline:
        line = holder.stdout.readline().strip()
        if line.startswith("HELD"):
            attempt_id = line.split()[1]
            break
    if attempt_id is None:
        holder.kill()
        note("N3-A-03", False, "holder never claimed")
        return
    holder.kill()  # real OS kill, no cooperative shutdown
    holder.wait(timeout=60)
    conn = connect_db(path)
    epoch2 = begin_service_epoch(conn, now=T2)
    rec = recover_interrupted_requests(conn, now=T2)
    status = conn.execute(
        "SELECT status, attempt_count FROM scrape_requests WHERE id = ?", (rid,)
    ).fetchone()
    att = conn.execute(
        "SELECT outcome FROM request_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    stale_err = None
    try:
        heartbeat(conn, rid, attempt_id, now=T3)
    except Exception as e:  # noqa: BLE001
        stale_err = type(e).__name__
    cm_err = None
    try:
        with fenced_commit(conn, rid, attempt_id, now=T3):
            pass
    except Exception as e:  # noqa: BLE001
        cm_err = type(e).__name__
    fresh = claim_next_request(conn, "worker-2", now=T3)
    conn.close()
    ok = (
        epoch2.epoch_id != epoch1.epoch_id
        and rid in rec["reclaimed"]
        and att["outcome"] == "ABANDONED"
        and status["status"] == "RETRY_WAIT"
        and stale_err is not None
        and cm_err is not None
        and fresh is not None
        and fresh.attempt_id != attempt_id
    )
    for direction in ('FORWARD', 'BACKWARD'):
        for expired in (False, True):
            for budget in (1, 3):
                clock_evidence.append(clock_anomaly_proof(
                    direction, expired_before_jump=expired, max_attempts=budget))
    note("N3-A-03", ok and len(clock_evidence) == 8,
         f"epoch_rotated={epoch2.epoch_id != epoch1.epoch_id} reclaimed={rec} "
         f"attempt_outcome={att['outcome']} status={status['status']} "
         f"old_heartbeat={stale_err} old_commit={cm_err} "
         f"fresh_attempt={(fresh.attempt_id if fresh else None)} "
         "R2-F11 clock_anomaly=PASS (8 proofs: forward/backward, active/expired, retry/exhausted)")


# -------- N3-A-04: cancellation blocks acquisition, host-native drains --------
def n3a_04() -> None:
    path = fresh_db("n3a04")
    setup(path)
    conn = connect_db(path)
    begin_service_epoch(conn, now=NOW)
    run_id, plans = create_run(conn, profile_id=None, plans=[plan()], now=NOW)
    acq1, _ = enqueue_request(
        conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="LIST_FETCH",
        target_identity="https://example.test/feed", priority=10,
    )
    acq2, _ = enqueue_request(
        conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="DETAIL_FETCH",
        target_identity="https://example.test/job/1",
    )
    local, _ = enqueue_request(
        conn, run_id=run_id, run_source_plan_id=plans[0], source_id="src-1",
        binding_id="bnd-1", request_type="ELIGIBILITY",
        target_identity="obs-1",
    )
    claim = claim_next_request(conn, "worker-1", now=NOW)
    assert claim is not None and claim.request_id == acq1
    request_run_cancellation(conn, run_id, now=T1)
    pend = conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (acq2,)
    ).fetchone()[0]
    nxt = claim_next_request(conn, "worker-2", now=T1)
    acq_leak = None
    seen = set()
    while nxt is not None and len(seen) < 5:
        seen.add(nxt.request_id)
        if nxt.request_id != local:
            acq_leak = nxt.request_id
            break
        # leave local RUNNING; keep scanning for any acquisition grant
        nxt = claim_next_request(conn, "worker-3", now=T1)
    conn.close()
    ok = pend == "CANCELLED" and local in seen and acq_leak is None
    note("N3-A-04", ok,
         f"pending_acq={pend} claimed={sorted(seen)} acq_leak={acq_leak}")


# -------- N3-A-05: Retry-After cooldown survives a real process restart --------
def n3a_05() -> None:
    path = fresh_db("n3a05")
    setup(path)
    r = run([str(PY), str(HERE / "w_cool_write.py"), str(path), NOW])
    if r.returncode != 0:
        note("N3-A-05", False, f"writer failed: {r.stdout[-500:]}")
        return
    before = json.loads(r.stdout.strip().splitlines()[-1])
    r2 = run([str(PY), str(HERE / "w_cool_read.py"), str(path), NOW])
    if r2.returncode != 0:
        note("N3-A-05", False, f"reader failed: {r2.stdout[-500:]}")
        return
    after = json.loads(r2.stdout.strip().splitlines()[-1])
    ok = (
        not before["allowed"] and not after["allowed"]
        and after["cooldown_until"] == before["cooldown_until"]
        and after["circuit_state"] == "OPEN"
    )
    note("N3-A-05", ok, f"before={before} after_restart={after}")


# -------- N3-A-06: PRAGMAs, integrity, settings on the post-workload DB --------
def n3a_06() -> None:
    global doctor_evidence
    from types import SimpleNamespace
    from jobscraper.launcher.doctor import _check_database
    path = WORK / "n3a03.db"  # post-kill workload from N3-A-03
    conn = connect_db(path)
    settings = read_sqlite_settings(conn)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    try:
        verify_sqlite_settings(conn)
        verified = True
    except Exception:  # noqa: BLE001
        verified = False
    conn.close()
    ok = (
        settings.journal_mode == "WAL"
        and settings.foreign_keys == 1
        and settings.synchronous == 2
        and settings.busy_timeout_ms == 5000
        and integrity == "ok"
        and fk == []
        and verified
    )
    doctor = _check_database(SimpleNamespace(database_file=path))
    doctor_evidence = doctor.as_dict()
    ok = ok and doctor.status == 'PASS'
    note("N3-A-06", ok,
         f"journal={settings.journal_mode} fk={settings.foreign_keys} "
         f"sync={settings.synchronous} busy_ms={settings.busy_timeout_ms} "
         f"integrity={integrity} fk_violations={len(fk)} verified={verified} "
         f"doctor_database={doctor.status}")


def main() -> int:
    global WORK
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('N3-A requires local Windows')
    args.output.mkdir(parents=True, exist_ok=True)
    WORK = Path(tempfile.mkdtemp(prefix='n3a-', dir=args.output))
    for scenario in (n3a_01, n3a_02, n3a_03, n3a_04, n3a_05, n3a_06):
        try:
            scenario()
        except Exception as exc:
            note('N3-A-' + scenario.__name__[-2:], False, repr(exc))
    failed = [k for k, v in results.items() if not v.startswith('PASS')]
    evidence = {
        'candidate_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        'working_tree_status': subprocess.check_output(['git', 'status', '--porcelain'], cwd=REPO, text=True),
        'platform': platform.platform(), 'python': sys.version,
        'checks': results, 'passed': len(results) - len(failed), 'failed': len(failed),
        'clock_anomaly': {'result': 'PASS' if len(clock_evidence) == 8 else 'FAIL',
                          'seam': 'ServiceClockGuard DB UTC / monotonic injection',
                          'system_clock_changed': False, 'proofs': clock_evidence},
        'doctor_database': doctor_evidence,
        'database_directory': str(WORK), 'promotion': False,
    }
    evidence_path = args.output / 'n3-a.json'
    evidence_path.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(f'{len(results) - len(failed)}/{len(results)} N3-A scenarios PASS')
    print(f'Evidence: {evidence_path}')
    return 1 if failed or len(clock_evidence) != 8 else 0


if __name__ == '__main__':
    sys.exit(main())
