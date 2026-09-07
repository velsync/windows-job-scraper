"""Unit tests: entity resolution stages, presence ordering, absence
idempotence, inbox triggers and obligations (§38-42, RUN-11/13/15)."""

import secrets

import pytest

from jobscraper.domain.profiles import create_profile
from jobscraper.domain.sources import create_source
from jobscraper.normalize.company import resolve_company
from jobscraper.runtime import obligations as obl
from jobscraper.runtime.entity_resolution import (
    merge_jobs,
    resolve_observation,
    undo_merge,
)
from jobscraper.runtime.presence import (
    derive_listing_status,
    upsert_presence,
)
from jobscraper.timeutil import utc_now_s
from jobscraper.workflow import inbox as inbox_mod


def _mk_source(db, name=None):
    return create_source(
        db, display_name=name or f"S{secrets.token_hex(3)}",
        entry_url=f"https://boards.example/{secrets.token_hex(4)}",
        canonical_host="boards.example", source_family="greenhouse",
    )


def _observation(source_id, job_id, canonical=None):
    return {
        "source_id": source_id,
        "source_job_id": job_id,
        "canonical_url_candidate": canonical,
        "observed_at": utc_now_s(),
    }


def _normalized(title="Senior Python Engineer", company="Acme", canonical=None, company_normalized=None):
    return {
        "title": title,
        "company": company,
        "company_normalized": company_normalized or (company or "").lower(),
        "canonical_url": canonical,
    }


def _mk_job(db, *, title="Senior Python Engineer", company=None):
    """Create a real canonical job through the public resolution path."""
    company = company or f"Co{secrets.token_hex(4)}"
    company_id = resolve_company(db, name=company, domain=None)
    decision = resolve_observation(
        db,
        observation_row=_observation(_mk_source(db), f"sid-{secrets.token_hex(4)}"),
        normalized=_normalized(title=title, company=company),
        company_id=company_id,
    )
    assert decision.decision == "CREATE"
    return decision.job_id


def _mk_profile(db, name="P"):
    return create_profile(db, name=name + secrets.token_hex(3))


def _event_kinds(db, job_id, profile_id):
    return [
        r["event_kind"]
        for r in db.query(
            "SELECT event_kind FROM job_profile_inbox_events WHERE job_id=? AND profile_id=?"
            " ORDER BY created_at",
            (job_id, profile_id),
        )
    ]


# ------------------------------------------------------------- resolution
def test_stage1_source_native_id_reused(db):
    src1 = _mk_source(db)
    company_id = resolve_company(db, name="Acme", domain=None)
    norm = _normalized(company="Acme")
    d1 = resolve_observation(
        db, observation_row=_observation(src1, "j-1"), normalized=norm, company_id=company_id
    )
    assert d1.decision == "CREATE"
    # The pipeline registers presence after resolving identity.
    upsert_presence(
        db, job_id=d1.job_id, source_id=src1, binding_id=None, source_job_id="j-1",
        observed_at=utc_now_s(),
    )
    d2 = resolve_observation(
        db, observation_row=_observation(src1, "j-1"), normalized=_normalized(company="Acme"),
        company_id=company_id,
    )
    assert d2.decision == "SELECT" and d2.job_id == d1.job_id
    assert d2.stage == "stage1_source_native_id"


def test_stage3_canonical_url_match(db):
    src1 = _mk_source(db)
    src2 = _mk_source(db)
    company_id = resolve_company(db, name="Acme", domain=None)
    url = "https://x.example/jobs/9"
    d1 = resolve_observation(
        db, observation_row=_observation(src1, None, canonical=url),
        normalized=_normalized(canonical=url, company="Acme"), company_id=company_id,
    )
    assert d1.decision == "CREATE"
    upsert_presence(
        db, job_id=d1.job_id, source_id=src1, binding_id=None, source_job_id="a-1",
        observed_at=utc_now_s(), canonical_job_url=url,
    )
    d2 = resolve_observation(
        db, observation_row=_observation(src2, None, canonical=url),
        normalized=_normalized(canonical=url, company="Acme"), company_id=company_id,
    )
    assert d2.decision == "SELECT" and d2.job_id == d1.job_id
    assert d2.stage == "stage3_canonical_url"


def test_stage4_company_title_similarity_merge(db):
    company_id = resolve_company(db, name="Acme", domain=None)
    d1 = resolve_observation(
        db, observation_row=_observation(_mk_source(db), None), normalized=_normalized(company="Acme"),
        company_id=company_id,
    )
    assert d1.decision == "CREATE"
    d2 = resolve_observation(
        db, observation_row=_observation(_mk_source(db), None),
        normalized=_normalized(title="senior python engineer", company="Acme"),
        company_id=company_id,
    )
    assert d2.decision == "SELECT" and d2.job_id == d1.job_id
    assert d2.stage == "stage4_content_similarity"


def test_different_titles_create_separate_identities(db):
    company_id = resolve_company(db, name="Acme", domain=None)
    d1 = resolve_observation(
        db, observation_row=_observation(_mk_source(db), None), normalized=_normalized(company="Acme"),
        company_id=company_id,
    )
    d2 = resolve_observation(
        db, observation_row=_observation(_mk_source(db), None),
        normalized=_normalized(title="Product Designer", company="Acme"), company_id=company_id,
    )
    assert d1.decision == "CREATE" and d2.decision == "CREATE"
    assert d2.job_id != d1.job_id


def test_source_id_reuse_after_closed_splits_identity(db):
    src_r = _mk_source(db)
    company_id = resolve_company(db, name="Acme", domain=None)
    d1 = resolve_observation(
        db, observation_row=_observation(src_r, "n-1"),
        normalized=_normalized(title="Senior Python Engineer", company="Acme"),
        company_id=company_id,
    )
    upsert_presence(
        db, job_id=d1.job_id, source_id=src_r, binding_id=None, source_job_id="n-1",
        observed_at=utc_now_s(),
    )
    # The old listing closed on the source.
    db.execute(
        "UPDATE job_sources SET presence_state='CLOSED' WHERE source_id=? AND source_job_id='n-1'",
        (src_r,),
    )
    # Same native ID returns with an incompatible title/company: reuse guard.
    d2 = resolve_observation(
        db, observation_row=_observation(src_r, "n-1"),
        normalized=_normalized(title="Office Manager", company="Other Corp",
                               company_normalized="other corp"),
        company_id=resolve_company(db, name="Other Corp", domain=None),
    )
    assert d2.decision == "SPLIT"
    assert d2.job_id != d1.job_id
    assert d2.stage == "stage1_reuse_guard"


# ------------------------------------------------------------------ merge
def test_merge_and_undo_restores_ledger(db):
    job_a = _mk_job(db, title="Alpha Engineer")
    job_b = _mk_job(db, title="Beta Engineer")
    profile_id = _mk_profile(db)
    inbox_mod.emit_inbox_event(
        db, job_id=job_b, profile_id=profile_id, event_kind="NEW_ELIGIBLE_APPEARANCE",
        dedupe_key=f"first-eligible:{job_b}:{profile_id}",
    )
    merge_id = merge_jobs(db, kept_job_id=job_a, absorbed_job_id=job_b, stage="user", reason_code="duplicate")
    # Disposition state migrated to kept job.
    kept_state = db.query_one(
        "SELECT * FROM job_profile_state WHERE job_id=? AND profile_id=?", (job_a, profile_id)
    )
    assert kept_state is not None
    # Absorbed job hidden, not destroyed.
    assert db.query_one("SELECT listing_status FROM jobs WHERE id=?", (job_b,))["listing_status"] == "WITHDRAWN"
    assert db.query_one("SELECT * FROM job_aliases WHERE job_id=?", (job_b,)) is not None

    assert undo_merge(db, merge_id) is True
    # Absorbed job restored with its own state.
    restored = db.query_one(
        "SELECT * FROM job_profile_state WHERE job_id=? AND profile_id=?", (job_b, profile_id)
    )
    assert restored is not None
    assert db.query_one("SELECT listing_status FROM jobs WHERE id=?", (job_b,))["listing_status"] == "ACTIVE"
    assert db.query_one("SELECT * FROM job_aliases WHERE job_id=?", (job_b,)) is None
    assert undo_merge(db, merge_id) is False  # second undo is a no-op


# --------------------------------------------------------------- presence
def test_presence_evidence_ordering_older_cannot_regress(db):
    job_id = _mk_job(db)
    src = _mk_source(db)
    js1 = upsert_presence(
        db, job_id=job_id, source_id=src, binding_id=None, source_job_id="x",
        observed_at="2026-09-01T00:00:00Z", content_revision=5, source_rank=10,
    )
    # Newer evidence arrives.
    upsert_presence(
        db, job_id=job_id, source_id=src, binding_id=None, source_job_id="x",
        observed_at="2026-09-05T00:00:00Z", content_revision=6, source_rank=10,
    )
    row = db.query_one("SELECT * FROM job_sources WHERE id=?", (js1,))
    assert row["content_revision"] == 6
    # An OLDER observation completing later must not regress the projection
    # NOR the verification clock (09-02 < last verified 09-05: no change).
    upsert_presence(
        db, job_id=job_id, source_id=src, binding_id=None, source_job_id="x",
        observed_at="2026-09-02T00:00:00Z", content_revision=7, source_rank=10,
    )
    row = db.query_one("SELECT * FROM job_sources WHERE id=?", (js1,))
    assert row["content_revision"] == 6
    assert row["last_verified_at"] == "2026-09-05T00:00:00Z"


def test_listing_status_truth_table(db):
    job_id = _mk_job(db)
    src = _mk_source(db)
    upsert_presence(
        db, job_id=job_id, source_id=src, binding_id=None, source_job_id="a",
        observed_at="2026-09-01T00:00:00Z", source_rank=10,
    )
    assert derive_listing_status(db, job_id, now="2026-09-07T00:00:00Z") == "ACTIVE"
    db.execute("UPDATE job_sources SET presence_state='UNCERTAIN' WHERE job_id=?", (job_id,))
    assert derive_listing_status(db, job_id, now="2026-09-07T00:00:00Z") == "UNCERTAIN"
    db.execute("UPDATE job_sources SET last_seen_at='2026-01-01T00:00:00Z' WHERE job_id=?", (job_id,))
    assert derive_listing_status(db, job_id, now="2026-09-07T00:00:00Z") == "EXPIRED"


def test_trusted_closed_wins_over_aggregator_active(db):
    job_id = _mk_job(db)
    src_agg = _mk_source(db, name="Aggregator")
    src_emp = _mk_source(db, name="Employer")
    upsert_presence(
        db, job_id=job_id, source_id=src_agg, binding_id=None, source_job_id="b1",
        observed_at="2026-09-01T00:00:00Z", source_rank=50,
    )
    upsert_presence(
        db, job_id=job_id, source_id=src_emp, binding_id=None, source_job_id="b2",
        observed_at="2026-09-01T00:00:00Z", source_rank=10,
    )
    db.execute(
        "UPDATE job_sources SET presence_state='CLOSED' WHERE source_id=? AND job_id=?",
        (src_emp, job_id),
    )
    assert derive_listing_status(db, job_id) == "CLOSED"


# ------------------------------------------------------------------ inbox
def test_disposition_optimistic_concurrency(db):
    job_id = _mk_job(db)
    profile_id = _mk_profile(db)
    inbox_mod.emit_inbox_event(
        db, job_id=job_id, profile_id=profile_id, event_kind="NEW_ELIGIBLE_APPEARANCE",
        dedupe_key=f"first-eligible:{job_id}:{profile_id}",
    )
    disposition, rev = inbox_mod.set_disposition(db, job_id=job_id, profile_id=profile_id, disposition="SHORTLISTED")
    assert disposition == "SHORTLISTED" and rev >= 2
    with pytest.raises(inbox_mod.StaleRowRevision):
        inbox_mod.set_disposition(
            db, job_id=job_id, profile_id=profile_id, disposition="DISMISSED",
            expected_row_revision=rev - 1,
        )


def test_trigger_dedupe_exactly_once(db):
    job_id = _mk_job(db)
    profile_id = _mk_profile(db)
    first = inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=50.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=1, profile_revision_id="r1",
        listing_status="ACTIVE",
    )
    assert first is not None
    assert _event_kinds(db, job_id, profile_id) == ["NEW_ELIGIBLE_APPEARANCE"]
    # Re-evaluating the same revision (refresh/restart) emits nothing.
    again = inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=50.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=1, profile_revision_id="r1",
        listing_status="ACTIVE",
    )
    assert again is None
    assert _event_kinds(db, job_id, profile_id) == ["NEW_ELIGIBLE_APPEARANCE"]


def test_meaningful_change_retrigger(db):
    job_id = _mk_job(db)
    profile_id = _mk_profile(db)
    inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=50.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=1, profile_revision_id="r1",
        listing_status="ACTIVE",
    )
    changed = inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=60.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=2, profile_revision_id="r1",
        listing_status="ACTIVE",
    )
    assert changed is not None
    kinds = _event_kinds(db, job_id, profile_id)
    assert kinds == ["NEW_ELIGIBLE_APPEARANCE", "MEANINGFUL_CHANGE"]
    # Same revision again: nothing.
    repeat = inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=60.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=2, profile_revision_id="r1",
        listing_status="ACTIVE",
    )
    assert repeat is None


def test_snooze_expiry_trigger_and_sticky_dismiss(db):
    job_id = _mk_job(db)
    profile_id = _mk_profile(db)
    inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=50.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=1, profile_revision_id="r1",
        listing_status="ACTIVE",
    )
    inbox_mod.set_disposition(
        db, job_id=job_id, profile_id=profile_id, disposition="SNOOZED",
        snoozed_until="2026-09-10T00:00:00Z", now="2026-09-07T00:00:00Z",
    )
    # Not inbox-eligible while snoozed.
    state = db.query_one(
        "SELECT * FROM job_profile_state WHERE job_id=? AND profile_id=?", (job_id, profile_id)
    )
    assert not inbox_mod.inbox_eligible(
        score=50, min_score_inbox=10, eligibility="ELIGIBLE",
        disposition=state["disposition"], snoozed_until=state["snoozed_until"],
        now="2026-09-08T00:00:00Z",
    )
    expired = inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=50.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=1, profile_revision_id="r1",
        listing_status="ACTIVE", now="2026-09-11T00:00:00Z",
    )
    assert expired is not None
    assert "SNOOZE_EXPIRED" in _event_kinds(db, job_id, profile_id)
    # Dismissed stays sticky: no further triggers, even on new revisions.
    inbox_mod.set_disposition(db, job_id=job_id, profile_id=profile_id, disposition="DISMISSED")
    events_before = db.query_one(
        "SELECT COUNT(*) c FROM job_profile_inbox_events WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    )["c"]
    result = inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=99.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=5, profile_revision_id="r1",
        listing_status="ACTIVE", now="2026-09-12T00:00:00Z",
    )
    assert result is None
    events_after = db.query_one(
        "SELECT COUNT(*) c FROM job_profile_inbox_events WHERE job_id=? AND profile_id=?",
        (job_id, profile_id),
    )["c"]
    assert events_after == events_before


def test_ineligible_below_threshold_emit_nothing(db):
    job_id = _mk_job(db)
    profile_id = _mk_profile(db)
    assert inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=5.0, eligibility="ELIGIBLE",
        min_score_inbox=10, content_revision=1, profile_revision_id="r1",
        listing_status="ACTIVE",
    ) is None
    assert inbox_mod.evaluate_inbox_triggers(
        db, job_id=job_id, profile_id=profile_id, score=50.0, eligibility="INELIGIBLE",
        min_score_inbox=10, content_revision=1, profile_revision_id="r1",
        listing_status="ACTIVE",
    ) is None
    assert _event_kinds(db, job_id, profile_id) == []


def test_inbox_queue_orders_and_filters(db):
    job_id = _mk_job(db)
    profile_id = _mk_profile(db)
    inbox_mod.emit_inbox_event(
        db, job_id=job_id, profile_id=profile_id, event_kind="NEW_ELIGIBLE_APPEARANCE",
        dedupe_key=f"first-eligible:{job_id}:{profile_id}",
    )
    db.execute(
        "INSERT INTO job_scores(job_id, profile_id, profile_revision_id, rules_revision_id,"
        " job_content_revision, normalization_version, scorer_version, score, breakdown_json,"
        " rule_version, scored_at) VALUES (?,?,NULL,'rules-v1',1,'normalize-v1',"
        " 'scorer-v1', 55.0, '[]', 'scoring-rules-v1', ?)",
        (job_id, profile_id, utc_now_s()),
    )
    queue = inbox_mod.inbox_queue(db, profile_id)
    assert [row["id"] for row in queue] == [job_id]
    assert queue[0]["score"] == 55.0
    # Below-threshold filter via min_score.
    assert inbox_mod.inbox_queue(db, profile_id, min_score=60.0) == []


# -------------------------------------------------------------- obligations
def test_obligation_lifecycle(db):
    from jobscraper.db.connection import immediate_transaction
    from jobscraper.runtime.obligations import create_obligation

    with immediate_transaction(db.conn) as tx:
        oid = create_obligation(tx, kind="PROCESS_OBSERVATION", observation_id="obs-1",
                                run_id="run-1", request_id="req-1")
    assert obl.pending_count(db, "run-1") == 1
    items = obl.claim_pending(db, limit=5)
    assert len(items) == 1 and items[0]["id"] == oid
    assert obl.pending_count(db, "run-1") == 1  # RUNNING still counts as pending work
    assert obl.satisfy(db, oid, claim_token=items[0]["claim_token"]) is True
    assert obl.pending_count(db, "run-1") == 0
    # Satisfy is fenced by claim token.
    assert obl.satisfy(db, oid, claim_token="wrong") is False


def test_obligation_failure_requeues(db):
    from jobscraper.db.connection import immediate_transaction
    from jobscraper.runtime.obligations import create_obligation

    with immediate_transaction(db.conn) as tx:
        create_obligation(tx, kind="PROCESS_OBSERVATION", observation_id="obs-2",
                          run_id=None, request_id=None)
    items = obl.claim_pending(db, limit=5)
    assert obl.fail(db, items[0]["id"], claim_token=items[0]["claim_token"]) is True
    assert obl.pending_count(db) == 1  # back to PENDING for retry
