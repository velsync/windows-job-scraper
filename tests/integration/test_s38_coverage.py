"""S3.8 coverage authority / same-scope absence / replay correctness."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from jobscraper.acquisition.crawler.revalidation import (
    RevalidationCompatibilityError,
    restore_membership,
    store_representation,
)
from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.db.connection import Database
from jobscraper.db.migrations import migrate_schema
from jobscraper.pipeline.coverage import (
    CoverageFinalizationError,
    CoverageIdentityError,
    degrade_coverage,
    finalize_coverage,
    open_coverage,
    open_or_resume_coverage,
    record_contributing_request,
    record_seen_identity,
)
from jobscraper.runtime.runs import create_run
from jobscraper.version import SCHEMA_VERSION

T0 = "2026-09-11T10:00:00.000000Z"
T1 = "2026-09-11T11:00:00.000000Z"
T2 = "2026-09-11T12:00:00.000000Z"
T3 = "2026-09-11T13:00:00.000000Z"
T4 = "2026-09-11T14:00:00.000000Z"


@dataclass(frozen=True)
class _Obs:
    source_job_id: str | None
    raw_url: str | None = None


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "s38.db")
    migrate_schema(database.conn, SCHEMA_VERSION)
    c = database.conn
    c.execute(
        "INSERT INTO sources(id,display_name,source_family,entry_url,created_at,updated_at)"
        " VALUES ('src','Fixture','CAREERS','https://jobs.example.test',?,?)",
        (T0, T0),
    )
    c.execute(
        "INSERT INTO adapter_definitions(adapter_id,adapter_version,adapter_api_version,"
        " manifest_json,created_at) VALUES ('fixture','1','1','{}',?)",
        (T0,),
    )
    c.execute(
        "INSERT INTO adapter_permission_profiles(id,display_name,created_at)"
        " VALUES ('perm','fixture',?)",
        (T0,),
    )
    c.execute(
        "INSERT INTO adapter_permission_profile_revisions(id,permission_profile_id,"
        " revision,policy_json,created_at) VALUES ('permrev','perm',1,'{}',?)",
        (T0,),
    )
    c.execute(
        "INSERT INTO source_adapter_bindings(id,source_id,display_name,created_at)"
        " VALUES ('bnd','src','fixture',?)",
        (T0,),
    )
    c.execute(
        "INSERT INTO source_adapter_binding_revisions(id,binding_id,revision,adapter_id,"
        " adapter_version,strategy,config_json,auth_requirement,execution_class,"
        " permission_profile_id,permission_profile_revision,created_at)"
        " VALUES ('bndrev','bnd',1,'fixture','1','HTTP_HTML','{}','NONE','HTTP',"
        " 'perm',1,?)",
        (T0,),
    )
    c.commit()
    yield database
    database.close()


def _plan(conn, suffix: str, *, now: str, binding_revision_id: str = "bndrev"):
    run_id, plan_ids = create_run(
        conn,
        profile_id=None,
        plans=[
            {
                "source_id": "src",
                "source_plan_group_id": f"grp-{suffix}",
                "fallback_rank": 0,
                "binding_id": "bnd",
                "binding_revision_id": binding_revision_id,
                "adapter_id": "fixture",
                "adapter_version": "1",
                "adapter_api_version": "1",
                "strategy": "HTTP_HTML",
                "execution_class": "HTTP",
                "permission_profile_id": "perm",
                "permission_profile_revision": 1,
                "cursor_schema_version": 1,
                "crawl_policy_snapshot_json": {},
            }
        ],
        now=now,
    )
    plan = conn.execute(
        "SELECT * FROM run_source_plans WHERE id = ?", (plan_ids[0],)
    ).fetchone()
    return run_id, plan


def _request(
    conn,
    run_id: str,
    plan,
    suffix: str,
    *,
    request_type: str = "LIST_FETCH",
    status: str = "SUCCEEDED",
    now: str = T1,
    payload: str = "{}",
):
    rid = f"req-{suffix}"
    conn.execute(
        """
        INSERT INTO scrape_requests(
            id,run_id,run_source_plan_id,source_id,binding_id,request_type,
            request_unique_key,payload_json,status,max_attempts,finished_at,
            created_at,updated_at)
        VALUES (?,?,?,?,?,?,?, ?,?,3,?,?,?)
        """,
        (
            rid,
            run_id,
            plan["id"],
            plan["source_id"],
            plan["binding_id"],
            request_type,
            f"key-{suffix}",
            payload,
            status,
            now if status in {"SUCCEEDED", "FAILED", "CANCELLED"} else None,
            now,
            now,
        ),
    )
    conn.commit()
    return rid


def _presence(conn, native_id: str, *, now: str = T0):
    jid = f"job-{native_id}"
    jsid = f"js-{native_id}"
    conn.execute(
        """
        INSERT INTO jobs(
            id,title,normalized_title,discovered_at,first_seen_at,last_seen_at,
            last_verified_at,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (jid, f"Job {native_id}", f"job {native_id}", now, now, now, now, now, now),
    )
    conn.execute(
        """
        INSERT INTO job_sources(
            id,job_id,source_id,binding_id,source_job_id,source_identity_generation,
            first_seen_at,last_seen_at,last_verified_at,presence_state,
            content_revision,created_at,updated_at)
        VALUES (?,?,?,?,?,1,?,?,?,'ACTIVE',1,?,?)
        """,
        (jsid, jid, "src", "bnd", native_id, now, now, now, now, now),
    )
    conn.commit()
    return jsid


def _coverage(
    conn,
    plan,
    *,
    scope: str,
    generation: str,
    now: str,
    listing_identity_sufficient: bool = True,
):
    authority = (
        "AUTHORITATIVE_FULL_SOURCE"
        if scope == "full-source"
        else "AUTHORITATIVE_DECLARED_SCOPE"
    )
    return open_coverage(
        conn,
        run_source_plan_id=plan["id"],
        scope_key=scope,
        generation_key=generation,
        coverage_authority=authority,
        now=now,
        listing_identity_sufficient=listing_identity_sufficient,
    )


def _successful_contributor(conn, coverage_id: str, run_id: str, plan, suffix: str, *, now: str):
    rid = _request(conn, run_id, plan, suffix, status="SUCCEEDED", now=now)
    assert record_contributing_request(conn, coverage_id, rid) is True
    return rid


def _finalize_complete(conn, coverage_id: str, *, now: str):
    finalize_coverage(
        conn,
        coverage_id,
        completion_state="COMPLETE",
        stop_reason="terminal cursor",
        terminal_enumeration_proven=True,
        now=now,
    )


def test_v18_schema_has_explicit_scope_and_application_storage(db):
    tables = {
        row[0]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"source_presence_scope_membership", "coverage_presence_application"} <= tables
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(enumeration_coverage)")}
    assert {"listing_identity_sufficient", "generation_order_key"} <= columns
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_open_coverage_derives_and_pins_immutable_plan_identity(db):
    run_id, plan = _plan(db.conn, "pin", now=T0)
    with pytest.raises(CoverageIdentityError):
        open_coverage(
            db.conn,
            run_source_plan_id=plan["id"],
            source_id="wrong-source",
            binding_id=plan["binding_id"],
            binding_revision_id=plan["binding_revision_id"],
            scope_key="full-source",
            generation_key="g-bad",
            coverage_authority="AUTHORITATIVE_FULL_SOURCE",
            now=T1,
        )

    cov = open_coverage(
        db.conn,
        run_source_plan_id=plan["id"],
        scope_key="full-source",
        generation_key="g-1",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        now=T1,
        listing_identity_sufficient=True,
    )
    row = db.conn.execute("SELECT * FROM enumeration_coverage WHERE id=?", (cov,)).fetchone()
    assert row["source_plan_group_id"] == plan["source_plan_group_id"]
    assert row["source_id"] == plan["source_id"]
    assert row["binding_id"] == plan["binding_id"]
    assert row["binding_revision_id"] == plan["binding_revision_id"]
    assert row["generation_order_key"].startswith(T1 + "|")

    resumed, was_resumed = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan["id"],
        scope_key="full-source",
        generation_key="ignored-on-resume",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        now=T2,
        listing_identity_sufficient=True,
    )
    assert (resumed, was_resumed) == (cov, True)


def test_contributor_cannot_cross_plan_generation(db):
    run1, plan1 = _plan(db.conn, "p1", now=T0)
    run2, plan2 = _plan(db.conn, "p2", now=T0)
    cov = _coverage(db.conn, plan1, scope="full-source", generation="g", now=T1)
    wrong = _request(db.conn, run2, plan2, "wrong-plan", now=T1)
    with pytest.raises(CoverageIdentityError):
        record_contributing_request(db.conn, cov, wrong)


def test_same_source_two_scopes_never_cross_age_membership(db):
    _presence(db.conn, "B", now=T0)
    run_id, plan = _plan(db.conn, "scopes", now=T0)

    # B is proven to belong to scope-B only.
    base = _coverage(db.conn, plan, scope="scope-B", generation="base", now=T0)
    _successful_contributor(db.conn, base, run_id, plan, "scope-b-base", now=T0)
    record_seen_identity(db.conn, base, "B", now=T0)
    _finalize_complete(db.conn, base, now=T0)

    # A complete empty scope-A run has no authority over B.
    scope_a = _coverage(db.conn, plan, scope="scope-A", generation="a-empty", now=T1)
    _successful_contributor(db.conn, scope_a, run_id, plan, "scope-a-empty", now=T1)
    _finalize_complete(db.conn, scope_a, now=T1)
    state = db.conn.execute("SELECT presence_state FROM job_sources WHERE id='js-B'").fetchone()[0]
    assert state == "ACTIVE"

    # A later complete empty scope-B run is genuinely same-scope absence.
    scope_b = _coverage(db.conn, plan, scope="scope-B", generation="b-empty", now=T2)
    _successful_contributor(db.conn, scope_b, run_id, plan, "scope-b-empty", now=T2)
    _finalize_complete(db.conn, scope_b, now=T2)
    state = db.conn.execute("SELECT presence_state FROM job_sources WHERE id='js-B'").fetchone()[0]
    assert state == "UNCERTAIN"


def test_generation_wide_seen_union_keeps_members_seen_across_pages(db):
    _presence(db.conn, "A", now=T0)
    _presence(db.conn, "B", now=T0)
    run_id, plan = _plan(db.conn, "union", now=T0)
    cov = _coverage(db.conn, plan, scope="full-source", generation="g1", now=T1)
    r1 = _request(db.conn, run_id, plan, "page-1", now=T1)
    r2 = _request(db.conn, run_id, plan, "page-2", now=T1)
    record_contributing_request(db.conn, cov, r1)
    record_contributing_request(db.conn, cov, r2)
    record_seen_identity(db.conn, cov, "A", evidence_ref="page://1", now=T1)
    record_seen_identity(db.conn, cov, "B", evidence_ref="page://2", now=T1)
    _finalize_complete(db.conn, cov, now=T2)
    assert {
        row[0]
        for row in db.conn.execute(
            "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id=?",
            (cov,),
        )
    } == {"A", "B"}
    assert {
        row[0]
        for row in db.conn.execute("SELECT presence_state FROM job_sources")
    } == {"ACTIVE"}


@pytest.mark.parametrize("state", ["PARTIAL", "BUDGET_EXHAUSTED", "CANCELLED", "FAILED"])
def test_incomplete_or_invalidated_generation_never_applies_absence(db, state):
    _presence(db.conn, "B", now=T0)
    run_id, plan = _plan(db.conn, f"invalid-{state}", now=T0)
    base = _coverage(db.conn, plan, scope="full-source", generation="base", now=T0)
    _successful_contributor(db.conn, base, run_id, plan, f"base-{state}", now=T0)
    record_seen_identity(db.conn, base, "B", now=T0)
    _finalize_complete(db.conn, base, now=T0)

    cov = _coverage(db.conn, plan, scope="full-source", generation=f"g-{state}", now=T1)
    finalize_coverage(
        db.conn,
        cov,
        completion_state=state,
        stop_reason=state.lower(),
        terminal_enumeration_proven=False,
        now=T2,
    )
    row = db.conn.execute(
        "SELECT presence_state,last_absence_coverage_id FROM job_sources WHERE id='js-B'"
    ).fetchone()
    assert tuple(row) == ("ACTIVE", None)


def test_failed_contributor_cannot_be_called_complete(db):
    _presence(db.conn, "B", now=T0)
    run_id, plan = _plan(db.conn, "failed-contrib", now=T0)
    cov = _coverage(db.conn, plan, scope="full-source", generation="g", now=T1)
    rid = _request(db.conn, run_id, plan, "failed-page", status="FAILED", now=T1)
    record_contributing_request(db.conn, cov, rid)
    with pytest.raises(CoverageFinalizationError, match="accepted SUCCEEDED"):
        _finalize_complete(db.conn, cov, now=T2)
    assert db.conn.execute(
        "SELECT finalized_at FROM enumeration_coverage WHERE id=?", (cov,)
    ).fetchone()[0] is None


def test_detail_work_joins_barrier_only_when_listing_identity_insufficient(db):
    run_id, plan = _plan(db.conn, "detail", now=T0)
    strict = _coverage(
        db.conn,
        plan,
        scope="strict",
        generation="g-strict",
        now=T1,
        listing_identity_sufficient=False,
    )
    _successful_contributor(db.conn, strict, run_id, plan, "strict-list", now=T1)
    _request(
        db.conn,
        run_id,
        plan,
        "detail-pending",
        request_type="DETAIL_FETCH",
        status="PENDING",
        now=T1,
    )
    with pytest.raises(CoverageFinalizationError, match="required detail"):
        _finalize_complete(db.conn, strict, now=T2)

    sufficient = _coverage(
        db.conn,
        plan,
        scope="sufficient",
        generation="g-sufficient",
        now=T1,
        listing_identity_sufficient=True,
    )
    _successful_contributor(db.conn, sufficient, run_id, plan, "sufficient-list", now=T1)
    _finalize_complete(db.conn, sufficient, now=T2)
    assert db.conn.execute(
        "SELECT completion_state FROM enumeration_coverage WHERE id=?", (sufficient,)
    ).fetchone()[0] == "COMPLETE"


def test_duplicate_finalization_and_application_are_idempotent(db):
    _presence(db.conn, "B", now=T0)
    run_id, plan = _plan(db.conn, "replay", now=T0)
    base = _coverage(db.conn, plan, scope="full-source", generation="base", now=T0)
    _successful_contributor(db.conn, base, run_id, plan, "replay-base", now=T0)
    record_seen_identity(db.conn, base, "B", now=T0)
    _finalize_complete(db.conn, base, now=T0)

    missing = _coverage(db.conn, plan, scope="full-source", generation="missing-1", now=T1)
    _successful_contributor(db.conn, missing, run_id, plan, "replay-missing", now=T1)
    _finalize_complete(db.conn, missing, now=T2)
    assert db.conn.execute("SELECT presence_state FROM job_sources WHERE id='js-B'").fetchone()[0] == "UNCERTAIN"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM coverage_presence_application WHERE coverage_id=?", (missing,)
    ).fetchone()[0] == 1

    # Same finalized facts replay safely; no second transition/application.
    _finalize_complete(db.conn, missing, now=T3)
    assert db.conn.execute("SELECT presence_state FROM job_sources WHERE id='js-B'").fetchone()[0] == "UNCERTAIN"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM coverage_presence_application WHERE coverage_id=?", (missing,)
    ).fetchone()[0] == 1

    missing2 = _coverage(db.conn, plan, scope="full-source", generation="missing-2", now=T3)
    _successful_contributor(db.conn, missing2, run_id, plan, "replay-missing-2", now=T3)
    _finalize_complete(db.conn, missing2, now=T4)
    assert db.conn.execute("SELECT presence_state FROM job_sources WHERE id='js-B'").fetchone()[0] == "EXPIRED"


def test_reverse_completion_of_older_absence_cannot_intensify_newer_absence(db):
    _presence(db.conn, "B", now=T0)
    run0, plan0 = _plan(db.conn, "baseline", now=T0)
    base = _coverage(db.conn, plan0, scope="full-source", generation="base", now=T0)
    _successful_contributor(db.conn, base, run0, plan0, "reverse-base", now=T0)
    record_seen_identity(db.conn, base, "B", now=T0)
    _finalize_complete(db.conn, base, now=T0)

    run_old, plan_old = _plan(db.conn, "old", now=T1)
    old = _coverage(db.conn, plan_old, scope="full-source", generation="old", now=T1)
    _successful_contributor(db.conn, old, run_old, plan_old, "reverse-old", now=T1)

    run_new, plan_new = _plan(db.conn, "new", now=T2)
    new = _coverage(db.conn, plan_new, scope="full-source", generation="new", now=T2)
    _successful_contributor(db.conn, new, run_new, plan_new, "reverse-new", now=T2)

    _finalize_complete(db.conn, new, now=T3)
    assert db.conn.execute("SELECT presence_state FROM job_sources WHERE id='js-B'").fetchone()[0] == "UNCERTAIN"

    # Older generation finishes later: it must not turn UNCERTAIN -> EXPIRED.
    _finalize_complete(db.conn, old, now=T4)
    assert db.conn.execute("SELECT presence_state FROM job_sources WHERE id='js-B'").fetchone()[0] == "UNCERTAIN"
    decision = db.conn.execute(
        "SELECT decision FROM coverage_presence_application WHERE coverage_id=? AND job_source_id='js-B'",
        (old,),
    ).fetchone()[0]
    assert decision == "SKIPPED_NEWER_ABSENCE"


def test_terminal_presence_newer_absence_is_not_mislabeled_as_newer_presence(db):
    # S3.10 absence advances last_verified_at even when CLOSED is preserved.
    # A still-older coverage generation must therefore be attributed to the
    # newer absence, not mistaken for newer positive presence merely because
    # the terminal semantic evidence kind remains the original close marker.
    from jobscraper.pipeline.availability import (
        EXPLICIT_TRUSTED_CLOSE,
        apply_presence_evidence,
    )

    _presence(db.conn, "B", now=T0)
    db.conn.execute(
        "UPDATE job_sources SET source_quality_class='EMPLOYER_CAREERS_PAGE' "
        "WHERE id='js-B'"
    )
    db.conn.commit()

    run0, plan0 = _plan(db.conn, "terminal-baseline", now=T0)
    base = _coverage(db.conn, plan0, scope="full-source", generation="base", now=T0)
    _successful_contributor(db.conn, base, run0, plan0, "terminal-base", now=T0)
    record_seen_identity(db.conn, base, "B", now=T0)
    _finalize_complete(db.conn, base, now=T0)

    close = apply_presence_evidence(
        db.conn,
        presence_id="js-B",
        evidence_kind=EXPLICIT_TRUSTED_CLOSE,
        effective_at=T1,
        received_at=T1,
        evidence_ref="detail:terminal-close",
    )
    assert close.accepted and close.new_state == "CLOSED"

    run_old, plan_old = _plan(db.conn, "terminal-old", now=T2)
    old = _coverage(db.conn, plan_old, scope="full-source", generation="old", now=T2)
    _successful_contributor(db.conn, old, run_old, plan_old, "terminal-old", now=T2)

    run_new, plan_new = _plan(db.conn, "terminal-new", now=T3)
    new = _coverage(db.conn, plan_new, scope="full-source", generation="new", now=T3)
    _successful_contributor(db.conn, new, run_new, plan_new, "terminal-new", now=T3)

    _finalize_complete(db.conn, new, now=T4)
    row = db.conn.execute(
        "SELECT presence_state,availability_evidence_kind,last_verified_at "
        "FROM job_sources WHERE id='js-B'"
    ).fetchone()
    assert tuple(row) == ("CLOSED", EXPLICIT_TRUSTED_CLOSE, T3)

    _finalize_complete(db.conn, old, now=T4)
    decision = db.conn.execute(
        "SELECT decision FROM coverage_presence_application "
        "WHERE coverage_id=? AND job_source_id='js-B'",
        (old,),
    ).fetchone()[0]
    assert decision == "SKIPPED_NEWER_ABSENCE"


def test_reverse_completion_of_old_missing_generation_cannot_regress_newer_presence(db):
    _presence(db.conn, "B", now=T0)
    run0, plan0 = _plan(db.conn, "baseline-active", now=T0)
    base = _coverage(db.conn, plan0, scope="full-source", generation="base", now=T0)
    _successful_contributor(db.conn, base, run0, plan0, "presence-base", now=T0)
    record_seen_identity(db.conn, base, "B", now=T0)
    _finalize_complete(db.conn, base, now=T0)

    run_old, plan_old = _plan(db.conn, "old-missing", now=T1)
    old = _coverage(db.conn, plan_old, scope="full-source", generation="old", now=T1)
    _successful_contributor(db.conn, old, run_old, plan_old, "presence-old", now=T1)

    run_new, plan_new = _plan(db.conn, "new-seen", now=T2)
    new = _coverage(db.conn, plan_new, scope="full-source", generation="new", now=T2)
    _successful_contributor(db.conn, new, run_new, plan_new, "presence-new", now=T2)
    db.conn.execute(
        "UPDATE job_sources SET last_seen_at=?,last_verified_at=?,updated_at=? WHERE id='js-B'",
        (T2, T2, T2),
    )
    db.conn.commit()
    record_seen_identity(db.conn, new, "B", now=T2)
    _finalize_complete(db.conn, new, now=T3)

    _finalize_complete(db.conn, old, now=T4)
    assert db.conn.execute("SELECT presence_state FROM job_sources WHERE id='js-B'").fetchone()[0] == "ACTIVE"
    decision = db.conn.execute(
        "SELECT decision FROM coverage_presence_application WHERE coverage_id=? AND job_source_id='js-B'",
        (old,),
    ).fetchone()[0]
    assert decision == "SKIPPED_NEWER_PRESENCE"


def test_unstable_or_partial_generation_stays_non_authoritative_across_restart(db):
    run_id, plan = _plan(db.conn, "unstable", now=T0)
    cov = _coverage(db.conn, plan, scope="full-source", generation="g", now=T1)
    _successful_contributor(db.conn, cov, run_id, plan, "unstable-page", now=T1)
    degrade_coverage(db.conn, cov, reason="unstable pagination/order")

    # Simulated restart uses durable coverage degradation, not in-memory state.
    resumed, was_resumed = open_or_resume_coverage(
        db.conn,
        run_source_plan_id=plan["id"],
        scope_key="full-source",
        generation_key="ignored",
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        now=T2,
        listing_identity_sufficient=True,
    )
    assert (resumed, was_resumed) == (cov, True)
    with pytest.raises(CoverageFinalizationError, match="degraded"):
        _finalize_complete(db.conn, cov, now=T2)


def _request_plan() -> RequestPlan:
    return RequestPlan(
        method="GET",
        url="https://jobs.example.test/jobs",
        headers={"Accept": "application/json"},
        expected_content_types=("application/json",),
        purpose="LIST_FETCH",
    )


def test_retained_304_membership_enters_same_scope_union_without_content_revision(db):
    _presence(db.conn, "A", now=T0)
    _presence(db.conn, "B", now=T0)
    run_id, plan = _plan(db.conn, "cache", now=T0)
    rep = store_representation(
        db.conn,
        plan_row=plan,
        request_plan=_request_plan(),
        validated_page_class="VALID_LIST",
        body=b'{"jobs":[{"id":"A"},{"id":"B"}]}',
        body_hash="1" * 64,
        normalized_content_hash="2" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1"'},
        observations=(_Obs("A", "list://A"), _Obs("B", "list://B")),
        membership_complete=True,
        normalization_version=1,
        now=T0,
    )
    cov = _coverage(db.conn, plan, scope="full-source", generation="304", now=T1)
    restored = restore_membership(
        db.conn,
        representation_id=rep,
        coverage_id=cov,
        plan_row=plan,
        now=T1,
    )
    assert restored == 2
    assert {
        row[0]
        for row in db.conn.execute(
            "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id=?",
            (cov,),
        )
    } == {"A", "B"}
    assert db.conn.execute(
        "SELECT COUNT(*) FROM source_presence_scope_membership WHERE scope_key='full-source'"
    ).fetchone()[0] == 2
    assert {
        row[0] for row in db.conn.execute("SELECT content_revision FROM job_sources")
    } == {1}


def test_retained_membership_binding_revision_mismatch_is_rejected(db):
    _presence(db.conn, "A", now=T0)
    run1, plan1 = _plan(db.conn, "cache-r1", now=T0)
    rep = store_representation(
        db.conn,
        plan_row=plan1,
        request_plan=_request_plan(),
        validated_page_class="VALID_LIST",
        body=b'{"jobs":[{"id":"A"}]}',
        body_hash="3" * 64,
        normalized_content_hash="4" * 64,
        content_type="application/json",
        response_headers={"ETag": '"r1"'},
        observations=(_Obs("A", "list://A"),),
        membership_complete=True,
        normalization_version=1,
        now=T0,
    )
    db.conn.execute(
        """
        INSERT INTO source_adapter_binding_revisions(
            id,binding_id,revision,adapter_id,adapter_version,strategy,config_json,
            auth_requirement,execution_class,permission_profile_id,
            permission_profile_revision,created_at)
        VALUES ('bndrev2','bnd',2,'fixture','1','HTTP_HTML','{}','NONE','HTTP','perm',1,?)
        """,
        (T1,),
    )
    db.conn.commit()
    run2, plan2 = _plan(db.conn, "cache-r2", now=T1, binding_revision_id="bndrev2")
    cov2 = _coverage(db.conn, plan2, scope="full-source", generation="g2", now=T2)
    with pytest.raises(RevalidationCompatibilityError, match="binding/source mismatch"):
        restore_membership(
            db.conn,
            representation_id=rep,
            coverage_id=cov2,
            plan_row=plan2,
            now=T2,
        )


def test_run_cancellation_refuses_complete_even_with_successful_contributor(db):
    run_id, plan = _plan(db.conn, "cancel-barrier", now=T0)
    cov = _coverage(db.conn, plan, scope="full-source", generation="g", now=T1)
    _successful_contributor(db.conn, cov, run_id, plan, "cancel-success", now=T1)
    db.conn.execute(
        "UPDATE scrape_runs SET cancel_requested_at=? WHERE id=?", (T1, run_id)
    )
    db.conn.commit()
    with pytest.raises(CoverageFinalizationError, match="cancellation"):
        _finalize_complete(db.conn, cov, now=T2)
    assert db.conn.execute(
        "SELECT finalized_at FROM enumeration_coverage WHERE id=?", (cov,)
    ).fetchone()[0] is None
