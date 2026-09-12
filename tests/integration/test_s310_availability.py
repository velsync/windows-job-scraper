"""S3.10 acceptance: evidence-ordered presence and canonical availability.

These tests intentionally exercise public/current seams rather than duplicating
the policy implementation.  On the accepted S3.9 parent they form the RED
gate: schema ordering metadata is absent, trusted newer closure loses to the
old flat ACTIVE precedence, and the S3.10 availability owner does not yet
exist.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.obligations import reconcile_job


T0 = "2026-09-12T06:00:00.000000Z"
T1 = "2026-09-12T07:00:00.000000Z"
T2 = "2026-09-12T08:00:00.000000Z"
T3 = "2026-09-12T09:00:00.000000Z"
T4 = "2026-09-12T10:00:00.000000Z"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "s310.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    for source_id, family in (
        ("src-emp-a", "EMPLOYER_CAREERS"),
        ("src-emp-b", "ATS_PROVIDER_API"),
        ("src-agg", "PUBLIC_BOARD"),
    ):
        conn.execute(
            """
            INSERT INTO sources (
                id, display_name, source_family, entry_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                source_id,
                family,
                f"https://{source_id}.example.test/jobs",
                T0,
                T0,
            ),
        )
        conn.execute(
            """
            INSERT INTO source_adapter_bindings (
                id, source_id, display_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (f"bnd-{source_id}", source_id, source_id, T0),
        )
    conn.commit()
    yield database
    database.close()


def _insert_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    listing_status: str = "ACTIVE",
) -> None:
    conn.execute(
        """
        INSERT INTO jobs (
            id, title, normalized_title, remote_worldwide,
            discovered_at, first_seen_at, last_seen_at, last_verified_at,
            listing_status, created_at, updated_at)
        VALUES (?, 'Engineer', 'engineer', 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, T0, T0, T0, T0, listing_status, T0, T0),
    )


def _insert_presence(
    conn: sqlite3.Connection,
    *,
    presence_id: str,
    job_id: str,
    source_id: str,
    native_id: str,
    state: str,
    at: str,
    quality: str,
    generation: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO job_sources (
            id, job_id, source_id, binding_id, source_job_id,
            source_identity_generation, first_seen_at, last_seen_at,
            last_verified_at, presence_state, content_revision,
            source_quality_class, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            presence_id,
            job_id,
            source_id,
            f"bnd-{source_id}",
            native_id,
            generation,
            at,
            at,
            at,
            state,
            quality,
            at,
            at,
        ),
    )


def _availability_module():
    try:
        from jobscraper.pipeline import availability
    except ImportError:
        pytest.fail("S3.10 availability policy owner is not installed")
    return availability


def test_v20_adds_durable_presence_evidence_order(db):
    columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(job_sources)").fetchall()
    }
    assert {
        "availability_effective_at",
        "availability_received_at",
        "availability_evidence_kind",
        "availability_evidence_ref",
        "availability_revision",
    } <= columns


def test_newer_trusted_close_supersedes_older_trusted_active_and_history_is_once(db):
    _insert_job(db.conn, "job-close")
    _insert_presence(
        db.conn,
        presence_id="p-active-old",
        job_id="job-close",
        source_id="src-emp-a",
        native_id="job-close-a",
        state="ACTIVE",
        at=T1,
        quality="EMPLOYER_CAREERS_PAGE",
    )
    _insert_presence(
        db.conn,
        presence_id="p-close-new",
        job_id="job-close",
        source_id="src-emp-b",
        native_id="job-close-b",
        state="CLOSED",
        at=T2,
        quality="EMPLOYER_STRUCTURED_ATS",
    )
    db.conn.commit()

    assert reconcile_job(db.conn, "job-close", now=T3) == "CLOSED"
    assert (
        db.conn.execute(
            "SELECT listing_status FROM jobs WHERE id='job-close'"
        ).fetchone()[0]
        == "CLOSED"
    )
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM job_history "
            "WHERE job_id='job-close' AND change_class='JOB_CLOSED'"
        ).fetchone()[0]
        == 1
    )

    # Replayed/local duplicate reconciliation cannot duplicate meaningful
    # availability history for the already accepted current revision.
    assert reconcile_job(db.conn, "job-close", now=T4) == "CLOSED"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM job_history "
            "WHERE job_id='job-close' AND change_class='JOB_CLOSED'"
        ).fetchone()[0]
        == 1
    )


def test_newer_trusted_active_reopens_older_close_exactly_once(db):
    _insert_job(db.conn, "job-reopen", listing_status="CLOSED")
    _insert_presence(
        db.conn,
        presence_id="p-close-old",
        job_id="job-reopen",
        source_id="src-emp-b",
        native_id="job-reopen-b",
        state="CLOSED",
        at=T1,
        quality="EMPLOYER_STRUCTURED_ATS",
    )
    _insert_presence(
        db.conn,
        presence_id="p-active-new",
        job_id="job-reopen",
        source_id="src-emp-a",
        native_id="job-reopen-a",
        state="ACTIVE",
        at=T2,
        quality="EMPLOYER_CAREERS_PAGE",
    )
    db.conn.commit()

    assert reconcile_job(db.conn, "job-reopen", now=T3) == "ACTIVE"
    assert reconcile_job(db.conn, "job-reopen", now=T4) == "ACTIVE"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM job_history "
            "WHERE job_id='job-reopen' AND change_class='JOB_REOPENED'"
        ).fetchone()[0]
        == 1
    )


def test_aggregator_disappearance_or_close_cannot_defeat_trusted_active(db):
    _insert_job(db.conn, "job-multisource")
    _insert_presence(
        db.conn,
        presence_id="p-employer-active",
        job_id="job-multisource",
        source_id="src-emp-a",
        native_id="job-ms-a",
        state="ACTIVE",
        at=T1,
        quality="EMPLOYER_CAREERS_PAGE",
    )
    # Even a temporally newer third-party CLOSED projection is not sufficient
    # canonical closure authority while trusted employer evidence is active.
    _insert_presence(
        db.conn,
        presence_id="p-agg-close",
        job_id="job-multisource",
        source_id="src-agg",
        native_id="job-ms-agg",
        state="CLOSED",
        at=T3,
        quality="AGGREGATOR_WITHOUT_RESOLVED_ORIGIN",
    )
    db.conn.commit()

    assert reconcile_job(db.conn, "job-multisource", now=T4) == "ACTIVE"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM job_history "
            "WHERE job_id='job-multisource' AND change_class='JOB_CLOSED'"
        ).fetchone()[0]
        == 0
    )


def test_reverse_completion_order_converges_to_same_presence_state(db):
    availability = _availability_module()

    for suffix in ("forward", "reverse"):
        job_id = f"job-order-{suffix}"
        presence_id = f"p-order-{suffix}"
        _insert_job(db.conn, job_id)
        _insert_presence(
            db.conn,
            presence_id=presence_id,
            job_id=job_id,
            source_id="src-emp-b",
            native_id=job_id,
            state="ACTIVE",
            at=T0,
            quality="EMPLOYER_STRUCTURED_ATS",
        )
    db.conn.commit()

    # Chronological application: close at T1, then active at T2.
    assert availability.apply_presence_evidence(
        db.conn,
        presence_id="p-order-forward",
        evidence_kind=availability.EXPLICIT_TRUSTED_CLOSE,
        effective_at=T1,
        received_at=T1,
        evidence_ref="detail:forward-close",
    ).accepted
    assert availability.apply_presence_evidence(
        db.conn,
        presence_id="p-order-forward",
        evidence_kind=availability.ACTIVE_OBSERVATION,
        effective_at=T2,
        received_at=T3,
        evidence_ref="observation:forward-active",
    ).accepted

    # Reverse worker completion: the newer active is accepted first.  The
    # older close finishes later locally and must be rejected as stale.
    assert availability.apply_presence_evidence(
        db.conn,
        presence_id="p-order-reverse",
        evidence_kind=availability.ACTIVE_OBSERVATION,
        effective_at=T2,
        received_at=T2,
        evidence_ref="observation:reverse-active",
    ).accepted
    stale = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-order-reverse",
        evidence_kind=availability.EXPLICIT_TRUSTED_CLOSE,
        effective_at=T1,
        received_at=T4,
        evidence_ref="detail:reverse-close",
    )
    assert stale.accepted is False
    assert stale.reason == "STALE_EVIDENCE"

    states = db.conn.execute(
        "SELECT id, presence_state FROM job_sources "
        "WHERE id IN ('p-order-forward','p-order-reverse') ORDER BY id"
    ).fetchall()
    assert {row["presence_state"] for row in states} == {"ACTIVE"}


def test_authoritative_absence_is_uncertain_then_expired_and_replay_safe(db):
    availability = _availability_module()
    _insert_job(db.conn, "job-absence")
    _insert_presence(
        db.conn,
        presence_id="p-absence",
        job_id="job-absence",
        source_id="src-emp-a",
        native_id="job-absence",
        state="ACTIVE",
        at=T0,
        quality="EMPLOYER_CAREERS_PAGE",
    )
    db.conn.commit()

    first = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-absence",
        evidence_kind=availability.AUTHORITATIVE_ABSENCE,
        effective_at=T1,
        received_at=T1,
        evidence_ref="coverage:one",
        coverage_id=None,
        scope_key="full-source",
    )
    assert first.accepted and first.new_state == "UNCERTAIN"

    second = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-absence",
        evidence_kind=availability.AUTHORITATIVE_ABSENCE,
        effective_at=T2,
        received_at=T2,
        evidence_ref="coverage:two",
        coverage_id=None,
        scope_key="full-source",
    )
    assert second.accepted and second.new_state == "EXPIRED"

    replay = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-absence",
        evidence_kind=availability.AUTHORITATIVE_ABSENCE,
        effective_at=T2,
        received_at=T2,
        evidence_ref="coverage:two",
        coverage_id=None,
        scope_key="full-source",
    )
    assert replay.accepted is False
    assert replay.reason == "DUPLICATE_CURRENT_EVIDENCE"
    assert (
        db.conn.execute(
            "SELECT availability_revision FROM job_sources WHERE id='p-absence'"
        ).fetchone()[0]
        == 2
    )


def test_trusted_detail_close_targets_current_native_generation_and_untrusted_refuses(db):
    availability = _availability_module()
    _insert_job(db.conn, "job-detail")
    _insert_presence(
        db.conn,
        presence_id="p-detail",
        job_id="job-detail",
        source_id="src-emp-b",
        native_id="native-77",
        state="ACTIVE",
        at=T1,
        quality="EMPLOYER_STRUCTURED_ATS",
    )
    _insert_job(db.conn, "job-agg-detail")
    _insert_presence(
        db.conn,
        presence_id="p-agg-detail",
        job_id="job-agg-detail",
        source_id="src-agg",
        native_id="agg-77",
        state="ACTIVE",
        at=T1,
        quality="AGGREGATOR_WITHOUT_RESOLVED_ORIGIN",
    )
    db.conn.commit()

    closed = availability.apply_trusted_detail_closure(
        db.conn,
        source_id="src-emp-b",
        target_reference="native-77",
        effective_at=T2,
        received_at=T2,
        evidence_ref="request:req-77:closure:JOB_CLOSED",
    )
    assert closed is not None and closed.accepted
    assert closed.new_state == "CLOSED"
    assert reconcile_job(db.conn, "job-detail", now=T3) == "CLOSED"

    replay = availability.apply_trusted_detail_closure(
        db.conn,
        source_id="src-emp-b",
        target_reference="native-77",
        effective_at=T2,
        received_at=T2,
        evidence_ref="request:req-77:closure:JOB_CLOSED",
    )
    assert replay is not None and replay.accepted is False
    assert reconcile_job(db.conn, "job-detail", now=T4) == "CLOSED"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM job_history "
            "WHERE job_id='job-detail' AND change_class='JOB_CLOSED'"
        ).fetchone()[0]
        == 1
    )

    refused = availability.apply_trusted_detail_closure(
        db.conn,
        source_id="src-agg",
        target_reference="agg-77",
        effective_at=T3,
        received_at=T3,
        evidence_ref="request:req-agg:closure:JOB_CLOSED",
    )
    assert refused is not None
    assert refused.accepted is False
    assert refused.reason == "UNTRUSTED_EXPLICIT_CLOSE"
    assert (
        db.conn.execute(
            "SELECT presence_state FROM job_sources WHERE id='p-agg-detail'"
        ).fetchone()[0]
        == "ACTIVE"
    )

    # Stored provenance quality is authoritative.  Even an employer-family
    # source must not be silently upgraded when the accepted presence was
    # explicitly classified as aggregator-quality evidence.
    _insert_job(db.conn, "job-explicit-untrusted")
    _insert_presence(
        db.conn,
        presence_id="p-explicit-untrusted",
        job_id="job-explicit-untrusted",
        source_id="src-emp-b",
        native_id="explicit-untrusted",
        state="ACTIVE",
        at=T1,
        quality="AGGREGATOR_WITHOUT_RESOLVED_ORIGIN",
    )
    db.conn.commit()
    explicit_untrusted = availability.apply_trusted_detail_closure(
        db.conn,
        source_id="src-emp-b",
        target_reference="explicit-untrusted",
        effective_at=T3,
        received_at=T3,
        evidence_ref="request:explicit-untrusted-close",
    )
    assert explicit_untrusted is not None
    assert explicit_untrusted.accepted is False
    assert explicit_untrusted.reason == "UNTRUSTED_EXPLICIT_CLOSE"


def test_trusted_detail_close_refuses_reused_native_identity_without_generation_binding(db):
    availability = _availability_module()

    # A delayed bare-native-id DETAIL request cannot prove which generation it
    # belongs to after RUN-15 reuse.  Fail closed rather than guessing the
    # newest generation and risking closure of the wrong canonical job.
    _insert_job(db.conn, "job-native-old")
    _insert_presence(
        db.conn,
        presence_id="p-native-old",
        job_id="job-native-old",
        source_id="src-emp-b",
        native_id="reused-native",
        state="CLOSED",
        at=T1,
        quality="EMPLOYER_STRUCTURED_ATS",
        generation=1,
    )
    _insert_job(db.conn, "job-native-current")
    _insert_presence(
        db.conn,
        presence_id="p-native-current",
        job_id="job-native-current",
        source_id="src-emp-b",
        native_id="reused-native",
        state="ACTIVE",
        at=T2,
        quality="EMPLOYER_STRUCTURED_ATS",
        generation=2,
    )
    db.conn.commit()

    result = availability.apply_trusted_detail_closure(
        db.conn,
        source_id="src-emp-b",
        target_reference="reused-native",
        effective_at=T3,
        received_at=T3,
        evidence_ref="request:ambiguous-generation-close",
    )
    assert result is None
    assert db.conn.execute(
        "SELECT presence_state FROM job_sources WHERE id='p-native-current'"
    ).fetchone()[0] == "ACTIVE"
    assert db.conn.execute(
        "SELECT presence_state FROM job_sources WHERE id='p-native-old'"
    ).fetchone()[0] == "CLOSED"

    # URL fallback can also be ambiguous across schema-valid identities.
    # RUN-15 forbids two rows with the same (source, native id, generation), so
    # exercise the real ambiguity surface instead: two distinct native identities
    # that legitimately share one URL fallback target.
    shared_url = "https://src-emp-b.example.test/jobs/shared-target"
    _insert_job(db.conn, "job-native-amb-a")
    _insert_presence(
        db.conn,
        presence_id="p-native-amb-a",
        job_id="job-native-amb-a",
        source_id="src-emp-b",
        native_id="ambiguous-native-a",
        state="ACTIVE",
        at=T2,
        quality="EMPLOYER_STRUCTURED_ATS",
        generation=3,
    )
    _insert_job(db.conn, "job-native-amb-b")
    _insert_presence(
        db.conn,
        presence_id="p-native-amb-b",
        job_id="job-native-amb-b",
        source_id="src-emp-b",
        native_id="ambiguous-native-b",
        state="ACTIVE",
        at=T2,
        quality="EMPLOYER_STRUCTURED_ATS",
        generation=3,
    )
    db.conn.execute(
        "UPDATE job_sources SET canonical_job_url=? "
        "WHERE id IN ('p-native-amb-a','p-native-amb-b')",
        (shared_url,),
    )
    db.conn.commit()

    assert availability.apply_trusted_detail_closure(
        db.conn,
        source_id="src-emp-b",
        target_reference=shared_url,
        effective_at=T4,
        received_at=T4,
        evidence_ref="request:ambiguous-close",
    ) is None
    assert {
        row[0]
        for row in db.conn.execute(
            "SELECT presence_state FROM job_sources "
            "WHERE id IN ('p-native-amb-a','p-native-amb-b')"
        )
    } == {"ACTIVE"}


def test_weaker_absence_preserves_close_but_advances_order_against_older_positive(db):
    availability = _availability_module()
    _insert_job(db.conn, "job-close-vs-absence")
    _insert_presence(
        db.conn,
        presence_id="p-close-vs-absence",
        job_id="job-close-vs-absence",
        source_id="src-emp-b",
        native_id="close-vs-absence",
        state="ACTIVE",
        at=T0,
        quality="EMPLOYER_STRUCTURED_ATS",
    )
    db.conn.commit()

    close = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-close-vs-absence",
        evidence_kind=availability.EXPLICIT_TRUSTED_CLOSE,
        effective_at=T1,
        received_at=T1,
        evidence_ref="detail:close",
    )
    assert close.accepted and close.new_state == "CLOSED"

    # A later absence is weaker as a state transition, so CLOSED is preserved,
    # but it is still newer current evidence and must advance the temporal
    # order.  Otherwise an older positive worker could reopen incorrectly.
    weaker = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-close-vs-absence",
        evidence_kind=availability.AUTHORITATIVE_ABSENCE,
        effective_at=T3,
        received_at=T3,
        evidence_ref="coverage:later",
        scope_key="full-source",
    )
    assert weaker.accepted is True
    assert weaker.state_changed is False
    assert weaker.new_state == "CLOSED"
    row = db.conn.execute(
        "SELECT presence_state, availability_evidence_kind, availability_effective_at "
        "FROM job_sources WHERE id='p-close-vs-absence'"
    ).fetchone()
    assert tuple(row) == (
        "CLOSED",
        availability.EXPLICIT_TRUSTED_CLOSE,
        T3,
    )

    stale_positive = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-close-vs-absence",
        evidence_kind=availability.ACTIVE_OBSERVATION,
        effective_at=T2,
        received_at=T4,
        evidence_ref="observation:older-than-absence",
    )
    assert stale_positive.accepted is False
    assert stale_positive.reason == "STALE_EVIDENCE"

    newest_positive = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-close-vs-absence",
        evidence_kind=availability.ACTIVE_OBSERVATION,
        effective_at=T4,
        received_at=T4,
        evidence_ref="observation:newest",
    )
    assert newest_positive.accepted is True
    assert newest_positive.new_state == "ACTIVE"


def test_newer_weak_absence_does_not_refresh_older_close_against_other_trusted_active(db):
    availability = _availability_module()
    _insert_job(db.conn, "job-close-strength")
    _insert_presence(
        db.conn,
        presence_id="p-close-strength",
        job_id="job-close-strength",
        source_id="src-emp-b",
        native_id="close-strength-a",
        state="ACTIVE",
        at=T0,
        quality="EMPLOYER_STRUCTURED_ATS",
    )
    _insert_presence(
        db.conn,
        presence_id="p-other-active",
        job_id="job-close-strength",
        source_id="src-emp-a",
        native_id="close-strength-b",
        state="ACTIVE",
        at=T2,
        quality="EMPLOYER_CAREERS_PAGE",
    )
    db.conn.commit()

    # Direct close is older than the other trusted ACTIVE.
    close = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-close-strength",
        evidence_kind=availability.EXPLICIT_TRUSTED_CLOSE,
        effective_at=T1,
        received_at=T1,
        evidence_ref="detail:old-close",
    )
    assert close.accepted and close.new_state == "CLOSED"
    assert reconcile_job(db.conn, "job-close-strength", now=T2) == "ACTIVE"

    # A newer absence must advance same-presence stale ordering but must not
    # falsely refresh the direct-close authority from T1 to T3.
    absence = availability.apply_presence_evidence(
        db.conn,
        presence_id="p-close-strength",
        evidence_kind=availability.AUTHORITATIVE_ABSENCE,
        effective_at=T3,
        received_at=T3,
        evidence_ref="coverage:newer-weak-absence",
        # Direct policy test: the durable coverage FK/pointer is not part of
        # this assertion. Full coverage integration exercises real coverage ids.
        coverage_id=None,
        scope_key="full-source",
    )
    assert absence.accepted and absence.new_state == "CLOSED"
    row = db.conn.execute(
        "SELECT availability_effective_at, availability_evidence_kind, "
        "last_changed_at FROM job_sources WHERE id='p-close-strength'"
    ).fetchone()
    assert tuple(row) == (T3, availability.EXPLICIT_TRUSTED_CLOSE, T1)
    assert reconcile_job(db.conn, "job-close-strength", now=T3) == "ACTIVE"



def test_exact_trusted_active_close_tie_is_conservative_and_revision_is_not_cross_presence_order(db):
    _insert_job(db.conn, "job-exact-tie")
    _insert_presence(
        db.conn,
        presence_id="z-close-tie",
        job_id="job-exact-tie",
        source_id="src-emp-b",
        native_id="close-tie",
        state="CLOSED",
        at=T2,
        quality="EMPLOYER_STRUCTURED_ATS",
    )
    _insert_presence(
        db.conn,
        presence_id="a-active-tie",
        job_id="job-exact-tie",
        source_id="src-emp-b",
        native_id="active-tie",
        state="ACTIVE",
        at=T2,
        quality="EMPLOYER_STRUCTURED_ATS",
    )
    # Per-presence revisions are not globally comparable.  Even an inflated
    # close revision must not decide an otherwise exact ACTIVE/CLOSED tie.
    db.conn.execute(
        "UPDATE job_sources SET availability_revision = 99 WHERE id='z-close-tie'"
    )
    db.conn.execute(
        "UPDATE job_sources SET availability_revision = 1 WHERE id='a-active-tie'"
    )
    db.conn.commit()

    assert reconcile_job(db.conn, "job-exact-tie", now=T3) == "ACTIVE"
