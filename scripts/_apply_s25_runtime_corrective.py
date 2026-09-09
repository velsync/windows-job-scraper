from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Greenhouse: a rejected listing member is a membership gap, distinct from a
# detail-enrichment budget.  A mixed list can preserve accepted observations
# while still refusing absence authority.
# ---------------------------------------------------------------------------
greenhouse = "src/jobscraper/adapters/greenhouse.py"
replace_once(
    greenhouse,
    '''        observations: list[ObservationRecord] = []\n        review: list[dict] = []\n        tasks: list[DiscoveredTask] = []\n        for order, item in enumerate(items):\n            observation, item_review, task = self._list_observation(item, order, envelope)\n            if observation is not None:\n                observations.append(observation)\n            if item_review is not None:\n                review.append(item_review)\n            if task is not None:\n                tasks.append(task)\n''',
    '''        observations: list[ObservationRecord] = []\n        review: list[dict] = []\n        tasks: list[DiscoveredTask] = []\n        rejected_members = 0\n        for order, item in enumerate(items):\n            observation, item_review, task = self._list_observation(item, order, envelope)\n            if observation is not None:\n                observations.append(observation)\n            else:\n                # The provider declared a member that could not be admitted to\n                # the stable membership set.  Preserve good observations, but\n                # never turn the resulting subset into absence authority.\n                rejected_members += 1\n            if item_review is not None:\n                review.append(item_review)\n            if task is not None:\n                tasks.append(task)\n''',
)
replace_once(
    greenhouse,
    '''        # §19/ACQ-03: the provider declared more members than it returned, so\n        # this enumeration did not complete — bounded PARTIAL, no authority.\n        truncated = declared_total is not None and declared_total > len(items)\n        return ParseOutcome(\n            kind=ParseOutcomeKind.PARTIAL if truncated else ParseOutcomeKind.SUCCESS_WITH_JOBS,\n            observations=tuple(observations),\n            discovered_tasks=tuple(tasks),\n            review_evidence=tuple(review),\n            continuation_required=truncated,\n            coverage_proposal={\n                "declared_total": declared_total,\n                "observed": len(observations),\n                "detail_tasks": len(tasks),\n            },\n            evidence_refs=refs,\n        )\n''',
    '''        # §19/ACQ-03/RUN-13: truncation or a rejected listed member means\n        # the membership proof is incomplete.  Detail-budget review is not a\n        # membership gap because listing_identity_sufficient is true.\n        truncated = declared_total is not None and declared_total > len(items)\n        membership_incomplete = truncated or rejected_members > 0\n        return ParseOutcome(\n            kind=(\n                ParseOutcomeKind.PARTIAL\n                if membership_incomplete\n                else ParseOutcomeKind.SUCCESS_WITH_JOBS\n            ),\n            observations=tuple(observations),\n            discovered_tasks=tuple(tasks),\n            review_evidence=tuple(review),\n            continuation_required=truncated,\n            coverage_proposal={\n                "declared_total": declared_total,\n                "observed": len(observations),\n                "rejected_members": rejected_members,\n                "detail_tasks": len(tasks),\n            },\n            evidence_refs=refs,\n        )\n''',
)


# ---------------------------------------------------------------------------
# Driver: enumeration coverage and run-level accepted child work are separate
# barriers when the adapter proves listing identity from enumeration alone.
# ---------------------------------------------------------------------------
driver = "src/jobscraper/pipeline/driver.py"
replace_once(
    driver,
    '''    return conn.execute(\n        "SELECT COUNT(*) FROM scrape_requests"\n        f" WHERE run_source_plan_id = ? AND request_type IN ({placeholders})"\n        f" AND status IN ({statuses})",\n        (run_source_plan_id, *sorted(ACQUISITION_REQUEST_TYPES), *_OPEN_REQUEST_STATUSES),\n    ).fetchone()[0]\n\n\ndef _dispatch_child_tasks(\n''',
    '''    return conn.execute(\n        "SELECT COUNT(*) FROM scrape_requests"\n        f" WHERE run_source_plan_id = ? AND request_type IN ({placeholders})"\n        f" AND status IN ({statuses})",\n        (run_source_plan_id, *sorted(ACQUISITION_REQUEST_TYPES), *_OPEN_REQUEST_STATUSES),\n    ).fetchone()[0]\n\n\ndef _latest_complete_coverage(\n    conn: sqlite3.Connection, run_source_plan_id: str\n) -> sqlite3.Row | None:\n    """Latest durable COMPLETE enumeration proof for this immutable plan."""\n    return conn.execute(\n        "SELECT * FROM enumeration_coverage"\n        " WHERE run_source_plan_id = ? AND completion_state = 'COMPLETE'"\n        " AND finalized_at IS NOT NULL AND terminal_enumeration_proven = 1"\n        " ORDER BY finalized_at DESC, id DESC LIMIT 1",\n        (run_source_plan_id,),\n    ).fetchone()\n\n\ndef _dispatch_child_tasks(\n''',
)
replace_once(
    driver,
    '''    # A resumed pass continues an unfinished generation of this run, or opens a\n    # distinct one when the earlier pass already finalized its own (generations\n    # are immutable; 03 §40).\n    coverage_id, _resumed = open_or_resume_coverage(\n        conn,\n        run_source_plan_id=plan_id,\n        source_id=plan_row["source_id"],\n        binding_id=plan_row["binding_id"],\n        scope_key="full-source",\n        generation_key=f"run-{run_id}",\n        coverage_authority="AUTHORITATIVE_FULL_SOURCE",\n        now=now,\n    )\n\n    terminal = False\n    degraded = False\n    cancelled = False\n''',
    '''    # If enumeration alone proves stable membership, a COMPLETE generation\n    # remains durable truth while accepted DETAIL enrichment is resumed.  A\n    # detail-only restart must not invent a second enumeration generation.\n    durable_complete = (\n        _latest_complete_coverage(conn, plan_id)\n        if listing_identity_sufficient\n        else None\n    )\n    coverage_finalized = durable_complete is not None\n    if durable_complete is not None:\n        coverage_id = durable_complete["id"]\n    else:\n        coverage_id, _resumed = open_or_resume_coverage(\n            conn,\n            run_source_plan_id=plan_id,\n            source_id=plan_row["source_id"],\n            binding_id=plan_row["binding_id"],\n            scope_key="full-source",\n            generation_key=f"run-{run_id}",\n            coverage_authority="AUTHORITATIVE_FULL_SOURCE",\n            now=now,\n        )\n\n    terminal = durable_complete is not None\n    coverage_degraded = False\n    run_degraded = False\n    cancelled = False\n''',
)
replace_once(
    driver,
    '''            if is_enumeration:\n                pages += 1\n            else:\n                details += 1\n            degraded = True\n            continue\n''',
    '''            if is_enumeration:\n                pages += 1\n            else:\n                details += 1\n            run_degraded = True\n            if is_enumeration or not listing_identity_sufficient:\n                coverage_degraded = True\n            continue\n''',
)
replace_once(
    driver,
    '''                    if observation.source_job_id:\n                        record_seen_identity(\n                            cursor_conn, coverage_id, observation.source_job_id,\n                            evidence_ref=observation.raw_url, commit=False,\n                        )\n''',
    '''                    if (\n                        observation.source_job_id\n                        and (is_enumeration or not listing_identity_sufficient)\n                        and not coverage_finalized\n                    ):\n                        record_seen_identity(\n                            cursor_conn, coverage_id, observation.source_job_id,\n                            evidence_ref=observation.raw_url, commit=False,\n                        )\n''',
)
replace_once(
    driver,
    '''        value = signal.get("value")\n        if value in ("INVALID", "FAILURE", "PARTIAL", "REFUSED"):\n            degraded = True\n        if is_enumeration and value in ("EMPTY", "TERMINAL"):\n''',
    '''        value = signal.get("value")\n        if value in ("INVALID", "FAILURE", "PARTIAL", "REFUSED"):\n            run_degraded = True\n            if is_enumeration or not listing_identity_sufficient:\n                coverage_degraded = True\n        if is_enumeration and value in ("EMPTY", "TERMINAL"):\n''',
)
replace_once(
    driver,
    '''    open_child_work = _open_acquisition_requests(conn, plan_id)\n    budget_exhausted = (\n        pages >= MAX_PAGES_PER_RUN or details >= MAX_DETAIL_REQUESTS_PER_RUN\n    )\n    if cancelled:\n        outcome = "CANCELLED"\n    elif terminal and not degraded and open_child_work == 0 and (pages or details):\n        # SATISFIED requires the whole accepted work set closed: a proven\n        # enumeration with a detail child still claimable is not satisfied.\n        outcome = "SATISFIED"\n    elif pages or details:\n        outcome = "SATISFIED_PARTIAL"\n    else:\n        outcome = "FAILED"\n\n    if terminal and not degraded and open_child_work == 0:\n        completion_state = "COMPLETE"\n        stop_reason = "terminal cursor"\n    elif cancelled:\n        completion_state = "PARTIAL"\n        stop_reason = "cancelled"\n    elif budget_exhausted and open_child_work:\n        completion_state = "BUDGET_EXHAUSTED"\n        stop_reason = "host acquisition budget exhausted with open child work"\n    elif open_child_work:\n        completion_state = "PARTIAL"\n        stop_reason = "open child work remains"\n    else:\n        completion_state = "PARTIAL"\n        stop_reason = "driver stop"\n    try:\n        finalize_coverage(\n            conn,\n            coverage_id,\n            completion_state=completion_state,\n            stop_reason=stop_reason,\n            terminal_enumeration_proven=terminal,\n            pages_completed=pages + details,\n            now=db_utc_now(conn),\n        )\n    except Exception:\n        # Coverage finalization must never block the run outcome.\n        finalize_coverage(\n            conn,\n            coverage_id,\n            completion_state="PARTIAL",\n            stop_reason="driver stop",\n            terminal_enumeration_proven=False,\n            pages_completed=pages + details,\n            now=db_utc_now(conn),\n        )\n    set_group_outcome(conn, plan_id, outcome, now=db_utc_now(conn))\n''',
    '''    open_child_work = _open_acquisition_requests(conn, plan_id)\n    if cancelled:\n        outcome = "CANCELLED"\n    elif terminal and not run_degraded and open_child_work == 0:\n        # Run satisfaction is the accepted-work barrier: even a COMPLETE\n        # listing cannot terminalize the run while child work is open.\n        outcome = "SATISFIED"\n    elif pages or details or terminal:\n        outcome = "SATISFIED_PARTIAL"\n    else:\n        outcome = "FAILED"\n\n    if not coverage_finalized:\n        # Enumeration completeness is a different truth from run completion.\n        # DETAIL work joins this barrier only for adapters whose listing does\n        # not itself prove stable membership.\n        coverage_barrier_open = (\n            open_child_work if not listing_identity_sufficient else 0\n        )\n        coverage_budget_exhausted = (\n            pages >= MAX_PAGES_PER_RUN\n            or (\n                not listing_identity_sufficient\n                and details >= MAX_DETAIL_REQUESTS_PER_RUN\n            )\n        )\n        if terminal and not coverage_degraded and coverage_barrier_open == 0:\n            completion_state = "COMPLETE"\n            stop_reason = "terminal cursor"\n        elif cancelled:\n            completion_state = "PARTIAL"\n            stop_reason = "cancelled"\n        elif coverage_budget_exhausted and coverage_barrier_open:\n            completion_state = "BUDGET_EXHAUSTED"\n            stop_reason = "host coverage budget exhausted with open contributing work"\n        elif coverage_barrier_open:\n            completion_state = "PARTIAL"\n            stop_reason = "open contributing work remains"\n        else:\n            completion_state = "PARTIAL"\n            stop_reason = "driver stop"\n        try:\n            finalize_coverage(\n                conn,\n                coverage_id,\n                completion_state=completion_state,\n                stop_reason=stop_reason,\n                terminal_enumeration_proven=terminal,\n                pages_completed=pages,\n                now=db_utc_now(conn),\n            )\n        except Exception:\n            # Coverage finalization must never block the run outcome.\n            finalize_coverage(\n                conn,\n                coverage_id,\n                completion_state="PARTIAL",\n                stop_reason="driver stop",\n                terminal_enumeration_proven=False,\n                pages_completed=pages,\n                now=db_utc_now(conn),\n            )\n    set_group_outcome(conn, plan_id, outcome, now=db_utc_now(conn))\n''',
)


# ---------------------------------------------------------------------------
# Existing acceptance tests are updated only where they pinned the incorrect
# coupling between DETAIL enrichment and enumeration coverage, plus the v2
# cleaner version required by changed canonicalization semantics.
# ---------------------------------------------------------------------------
content_test = "tests/unit/test_contentclean.py"
replace_once(
    content_test,
    'assert CONTENT_CLEANING_VERSION == "content-clean-v1"',
    'assert CONTENT_CLEANING_VERSION == "content-clean-v2"',
)

e2e = "tests/integration/test_greenhouse_e2e.py"
replace_once(
    e2e,
    '    assert cov["pages_completed"] == 4\n',
    '    assert cov["pages_completed"] == 1\n',
)
replace_once(
    e2e,
    '''    # The listing itself was complete and terminal, and that fact is recorded;\n    # what withholds authority is the generation state.  Absence is applied only\n    # for COMPLETE (RUN-13), and a provider whose detail answers contradict its\n    # own listing has not earned a COMPLETE generation.\n    assert cov["completion_state"] == "PARTIAL"\n    assert cov["terminal_enumeration_proven"] == 1\n''',
    '''    # The listing itself was complete and terminal, so its membership proof\n    # stays COMPLETE.  Contradictory DETAIL responses degrade the run, not the\n    # already-proven listing set (RUN-13 / listing_identity_sufficient).\n    assert cov["completion_state"] == "COMPLETE"\n    assert cov["terminal_enumeration_proven"] == 1\n    assert cov["pages_completed"] == 1\n''',
)
replace_once(
    e2e,
    '''    cov = _coverage(db)[0]\n    # the enumeration fact is recorded, the generation is not relyable: a\n    # budget-stopped run with accepted child work outstanding never applies\n    # absence evidence (only COMPLETE does)\n    assert cov["completion_state"] == "BUDGET_EXHAUSTED"\n    assert cov["terminal_enumeration_proven"] == 1\n    assert "budget" in cov["stop_reason"]\n''',
    '''    cov = _coverage(db)[0]\n    # The listing already proved the full stable membership set.  DETAIL work\n    # remains an accepted run obligation, but it is not part of this adapter's\n    # absence-authority barrier.\n    assert cov["completion_state"] == "COMPLETE"\n    assert cov["terminal_enumeration_proven"] == 1\n    assert cov["pages_completed"] == 1\n    assert "terminal" in cov["stop_reason"]\n''',
)

# One-time applicator removes itself and its workflow from the resulting tree.
Path(".github/workflows/_repair_s25_runtime.yml").unlink(missing_ok=True)
Path(__file__).unlink(missing_ok=True)
