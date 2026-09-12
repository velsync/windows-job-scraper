from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one replacement target, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


driver = Path("src/jobscraper/pipeline/driver.py")
greenhouse = Path("tests/integration/test_greenhouse_e2e.py")
lever = Path("tests/integration/test_lever_e2e.py")
run_driver = Path("tests/integration/test_run_driver.py")
runtime_correctives = Path("tests/integration/test_s2_5_runtime_correctives.py")

replace_once(
    driver,
    '''    open_child_work = _open_acquisition_requests(conn, plan_id)\n    if not (state_changed or pages or details or terminal or cancelled or ownership_lost):\n''',
    '''    open_child_work = _open_acquisition_requests(conn, plan_id)\n    if open_child_work and not (pages or details or terminal or cancelled):\n        # A consumed/lost attempt can be reclaimed into RETRY_WAIT.  That is\n        # still accepted future work, not a terminal failure.  Leave the plan\n        # and coverage generation open so a later pass can claim the retry.\n        return\n    if not (state_changed or pages or details or terminal or cancelled or ownership_lost):\n''',
)

replace_once(
    greenhouse,
    '''def test_an_open_detail_child_blocks_terminalization_until_drained(db, server):\n    """ACQ-04/RUN-01: the host finishes what it accepted before claiming done.\n\n    Driven in two passes: the first pass is stopped after the listing (its\n    detail children are still open), the second pass drains them.  Only the\n    second may report a satisfied plan and complete coverage.\n    """\n    from jobscraper.pipeline import driver as driver_module\n\n    provisioned = _provision(db, server, board="acme")\n    run_id = _start_run(db, provisioned, board="acme")\n    original = driver_module.MAX_DETAIL_REQUESTS_PER_RUN\n    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) == "PARTIAL"\n    finally:\n        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original\n''',
    '''def test_an_open_detail_child_blocks_terminalization_until_drained(db, server):\n    """ACQ-04/RUN-01: the host finishes what it accepted before claiming done.\n\n    Driven in two passes: the first pass is stopped after the listing (its\n    detail children are still open), the second pass drains them.  Only the\n    second may report a satisfied plan and complete coverage.\n    """\n    from jobscraper.pipeline import driver as driver_module\n\n    provisioned = _provision(db, server, board="acme")\n    run_id = _start_run(db, provisioned, board="acme")\n    original = driver_module.MAX_DETAIL_REQUESTS_PER_RUN\n    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) is None\n    finally:\n        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original\n    run = db.conn.execute(\n        "SELECT status, finished_at FROM scrape_runs WHERE id = ?", (run_id,)\n    ).fetchone()\n    assert tuple(run) == ("RUNNING", None)\n''',
)

replace_once(
    greenhouse,
    '''    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) == "PARTIAL"\n        observations = db.conn.execute(\n''',
    '''    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) is None\n        observations = db.conn.execute(\n''',
)

replace_once(
    lever,
    '''def test_rate_limited_site_is_partial_and_never_terminal(db, server):\n    """A 429 is a typed page class, not a parser failure and not an empty site."""\n    run_id, _ = _run_board(db, server, board="ratelimited")\n    assert execute_run(db.conn, run_id) == "PARTIAL"\n''',
    '''def test_rate_limited_site_is_partial_and_never_terminal(db, server):\n    """A 429 is a typed page class, not a parser failure and not an empty site."""\n    run_id, _ = _run_board(db, server, board="ratelimited")\n    assert execute_run(db.conn, run_id) is None\n''',
)

replace_once(
    lever,
    '''def test_challenge_page_site_is_partial_and_keeps_evidence(db, server):\n    run_id, _ = _run_board(db, server, board="challenge")\n    assert execute_run(db.conn, run_id) == "PARTIAL"\n''',
    '''def test_challenge_page_site_is_partial_and_keeps_evidence(db, server):\n    run_id, _ = _run_board(db, server, board="challenge")\n    assert execute_run(db.conn, run_id) is None\n''',
)

replace_once(
    lever,
    '''def test_an_open_detail_child_blocks_terminalization_until_drained(db, server):\n    from jobscraper.pipeline import driver as driver_module\n\n    provisioned = _provision(db, server, board="acme")\n    run_id = _start_run(db, provisioned, board="acme")\n    original = driver_module.MAX_DETAIL_REQUESTS_PER_RUN\n    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) == "PARTIAL"\n    finally:\n        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original\n''',
    '''def test_an_open_detail_child_blocks_terminalization_until_drained(db, server):\n    from jobscraper.pipeline import driver as driver_module\n\n    provisioned = _provision(db, server, board="acme")\n    run_id = _start_run(db, provisioned, board="acme")\n    original = driver_module.MAX_DETAIL_REQUESTS_PER_RUN\n    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) is None\n    finally:\n        driver_module.MAX_DETAIL_REQUESTS_PER_RUN = original\n''',
)

replace_once(
    lever,
    '''    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) == "PARTIAL"\n        observations = db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0]\n''',
    '''    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) is None\n        observations = db.conn.execute("SELECT COUNT(*) FROM job_observations").fetchone()[0]\n''',
)

replace_once(
    runtime_correctives,
    '''    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) == "PARTIAL"\n    finally:\n''',
    '''    driver_module.MAX_DETAIL_REQUESTS_PER_RUN = 0\n    try:\n        assert execute_run(db.conn, run_id) is None\n    finally:\n''',
)

replace_once(
    run_driver,
    '''    status = execute_run(db.conn, run_id)\n\n    assert status == "FAILED"  # nothing committed (page budget consumed by loss)\n    req = db.conn.execute(\n''',
    '''    status = execute_run(db.conn, run_id)\n\n    assert status is None  # reclaimed RETRY_WAIT keeps terminal run truth open\n    run = db.conn.execute(\n        "SELECT status, finished_at FROM scrape_runs WHERE id = ?", (run_id,)\n    ).fetchone()\n    assert tuple(run) == ("RUNNING", None)\n    plan = db.conn.execute(\n        "SELECT group_outcome FROM run_source_plans WHERE run_id = ?", (run_id,)\n    ).fetchone()\n    assert plan["group_outcome"] is None\n    req = db.conn.execute(\n''',
)
