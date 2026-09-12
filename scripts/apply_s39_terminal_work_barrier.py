from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one replacement target, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


runs = Path("src/jobscraper/runtime/runs.py")
tests = Path("tests/integration/test_s39_fallback_groups.py")

replace_once(
    runs,
    '''    Mirrors the claim path exactly: ``PENDING`` is always claimable,\n    ``RUNNING`` is in flight under a lease, and ``RETRY_WAIT`` is claimable\n    only with a valid durable ``next_retry_at`` that is already due.  A\n    dormant retry (not-yet-due, NULL, or empty timestamp) is neither\n    claimable nor in flight: nothing in the bounded pass can act on it, so\n    it must not wedge plan terminalization or run finalization (RUN-01\n    liveness; RUN-07 lease-loss recovery keeps the retry row durable for a\n    later pass via the terminal-plan re-drive path).  The caller binds the\n    authoritative timestamp once.\n    """\n    prefix = f"{alias}." if alias else ""\n    return (\n        f"({prefix}status IN ('PENDING', 'RUNNING')"\n        f" OR ({prefix}status = 'RETRY_WAIT'"\n        f" AND {prefix}next_retry_at IS NOT NULL"\n        f" AND {prefix}next_retry_at <> ''"\n        f" AND {prefix}next_retry_at <= ?))"\n    )\n''',
    '''    Terminal truth is stricter than immediate claimability: ``PENDING`` and\n    ``RUNNING`` are open now, while every ``RETRY_WAIT`` is durable promised\n    future work even when ``next_retry_at`` is not due yet.  Claim selection\n    still applies the due-time predicate; this barrier only prevents a plan or\n    run from becoming terminal while accepted retry work remains outstanding.\n    """\n    prefix = f"{alias}." if alias else ""\n    return f"{prefix}status IN ('PENDING', 'RUNNING', 'RETRY_WAIT')"\n''',
)

replace_once(
    runs,
    '''            (plan_id, *sorted(ACQUISITION_REQUEST_TYPES), now),\n''',
    '''            (plan_id, *sorted(ACQUISITION_REQUEST_TYPES)),\n''',
)

replace_once(
    runs,
    '''    Acquisition work counts only while its owning plan can still drive it:\n    the plan is undecided or partial, and the request is claimable or in\n    flight.  A dormant retry is actionable by nothing in the bounded pass,\n    and leftovers owned by a finally-terminal plan are claimable by nobody,\n    so neither wedges finalization while their rows stay durable.  Only\n    acquisition work belonging to an explicitly skipped plan was already\n    irrelevant to further collection.\n''',
    '''    Acquisition work counts only while its owning plan can still drive it:\n    the plan is undecided or partial, and the request is pending, in flight,\n    or waiting for its durable retry time.  Deferred retry work therefore\n    keeps the run non-terminal even though it is not claimable yet.  Leftovers\n    owned by a finally-terminal/skipped plan remain diagnostic rows and do not\n    wedge finalization because that plan can no longer drive them.\n''',
)

replace_once(
    runs,
    '''                *sorted(ACQUISITION_REQUEST_TYPES),\n                now,\n            ),\n''',
    '''                *sorted(ACQUISITION_REQUEST_TYPES),\n            ),\n''',
)

replace_once(
    runs,
    '''        # An explicitly incomplete verdict (PARTIAL/CANCELLED) honestly reports\n        # unfinished collection, so open acquisition work does not block it;\n        # accepted host-native obligations must still drain first.  A verdict\n        # of SUCCEEDED/FAILED claims nothing is left actionable.\n        if status in ("PARTIAL", "CANCELLED"):\n            blocking = _relevant_native_open_work(conn, run_id)\n        else:\n            blocking = _relevant_open_work(conn, run_id, now=ts)\n''',
    '''        # Group truth may be visibly partial while accepted work remains, but\n        # RUN-01 does not permit the run itself to become terminal until every\n        # relevant acquisition and host-native obligation has drained.\n        blocking = _relevant_open_work(conn, run_id, now=ts)\n''',
)

replace_once(
    tests,
    '''def test_dormant_retry_does_not_block_plan_or_run_terminalization(tmp_path):\n    """RUN-01 liveness: a not-yet-due retry is neither claimable (claim path\n    requires a valid due ``next_retry_at``) nor in flight, so it must not\n    wedge plan terminalization or run finalization.  The retry row stays\n    durable for a later pass (RUN-07 lease-loss shape)."""\n    db = _db(tmp_path / "dormant.db")\n    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(1), now=NOW)\n    _acquisition_retry(db, run_id, plan_ids[0], "dormant", next_retry_at=FAR_FUTURE)\n    set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)\n    assert _group(db, run_id)["group_outcome"] == "FAILED"\n    assert aggregate_run(db.conn, run_id, now=LATER) == "FAILED"\n    assert db.conn.execute(\n        "SELECT status FROM scrape_requests WHERE id='dormant'"\n    ).fetchone()[0] == "RETRY_WAIT"\n    db.close()\n''',
    '''def test_future_retry_blocks_plan_and_run_terminalization(tmp_path):\n    """A not-yet-due retry is not claimable yet, but it remains accepted\n    durable work and therefore blocks terminal plan/run truth until resolved."""\n    db = _db(tmp_path / "dormant.db")\n    run_id, plan_ids = create_run(db.conn, profile_id=None, plans=_plans(1), now=NOW)\n    _acquisition_retry(db, run_id, plan_ids[0], "dormant", next_retry_at=FAR_FUTURE)\n    try:\n        set_group_outcome(db.conn, plan_ids[0], "FAILED", now=LATER)\n    except RunStateConflict:\n        pass\n    else:  # pragma: no cover - regression failure path\n        raise AssertionError("future retry incorrectly allowed plan terminalization")\n    assert _group(db, run_id)["group_outcome"] is None\n    assert aggregate_run(db.conn, run_id, now=LATER) is None\n    assert db.conn.execute(\n        "SELECT status FROM scrape_requests WHERE id='dormant'"\n    ).fetchone()[0] == "RETRY_WAIT"\n    db.close()\n''',
)

replace_once(
    tests,
    '''    assert aggregate_run(db.conn, run_id, now=LATER) == "PARTIAL"\n    claimed = claim_next_request(db.conn, "worker", now=LATER)\n''',
    '''    assert aggregate_run(db.conn, run_id, now=LATER) is None\n    claimed = claim_next_request(db.conn, "worker", now=LATER)\n''',
)
