"""S2.2 integration: company resolution with strong signals only (01 §33.1).

"Weak evidence must not aggressively merge companies."  These tests pin the
resolution rules: an attach needs a strong identifier (same ATS board, or same
application/careers host); a bare normalized name is *never* enough; a
strong-identifier attach with a conflicting display name is attached and
recorded for review instead of silently renaming anyone; every decision is
durable in ``company_resolution_events``.
"""

from __future__ import annotations

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.companies import (
    COMPANY_RESOLUTION_VERSION,
    CompanySignals,
    resolve_company,
)

NOW = "2026-09-08T09:00:00.000000Z"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "companies.db")
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    yield database
    database.close()


def _signals(**kwargs) -> CompanySignals:
    # no hidden defaults beyond the display name: every strong signal has to
    # be stated, so a test cannot accidentally "attach" by construction
    name = kwargs.pop("name", "Acme Data")
    return CompanySignals(name=name, **kwargs)


def _resolve(conn, *, observation_id=None, **kwargs):
    return resolve_company(
        conn,
        signals=_signals(**kwargs),
        observation_id=observation_id,
        observed_at=NOW,
        now=NOW,
    )


def test_first_sighting_creates_a_company_with_its_identifiers(db):
    result = _resolve(
        db.conn,
        ats_provider="GREENHOUSE",
        ats_board="acme",
        careers_url="https://boards.greenhouse.io/acme",
        application_host="boards.greenhouse.io",
    )
    assert result.decision == "CREATED"
    company = db.conn.execute(
        "SELECT * FROM companies WHERE id = ?", (result.company_id,)
    ).fetchone()
    assert company["name"] == "Acme Data"
    assert company["normalized_name"] == "acme data"
    assert company["ats_provider"] == "GREENHOUSE"
    assert company["ats_board"] == "acme"
    assert company["careers_url"] == "https://boards.greenhouse.io/acme"
    kinds = {
        row["kind"]
        for row in db.conn.execute(
            "SELECT kind FROM company_identifiers WHERE company_id = ?",
            (result.company_id,),
        )
    }
    assert {"ATS_BOARD", "APP_HOST"} <= kinds
    assert result.resolution_version == COMPANY_RESOLUTION_VERSION


def test_same_ats_board_attaches_even_when_the_display_name_differs(db):
    first = _resolve(db.conn, ats_provider="GREENHOUSE", ats_board="acme")
    second = _resolve(
        db.conn,
        name="Acme Analytics GmbH",
        ats_provider="GREENHOUSE",
        ats_board="acme",
    )
    assert second.company_id == first.company_id
    assert second.decision == "ATTACHED"
    assert second.matched_on == "ATS_BOARD"
    assert second.name_conflict is True
    # the recorded display name is never rewritten by a later sighting
    assert db.conn.execute(
        "SELECT name FROM companies WHERE id = ?", (first.company_id,)
    ).fetchone()["name"] == "Acme Data"


def test_bare_normalized_name_alone_never_merges(db):
    first = _resolve(db.conn, ats_provider="GREENHOUSE", ats_board="acme")
    # same normalized name, no board, an unrelated careers host
    second = _resolve(db.conn, application_host="other-careers.example")
    assert second.company_id != first.company_id
    assert second.decision == "NAME_ONLY_NEW_COMPANY"
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2


def test_same_application_host_is_a_strong_signal(db):
    first = _resolve(db.conn, application_host="jobs.acme.example", ats_provider=None, ats_board=None)
    second = _resolve(
        db.conn,
        name="ACME",
        application_host="www.jobs.acme.example",
        ats_provider=None,
        ats_board=None,
    )
    assert second.company_id == first.company_id
    assert second.matched_on == "APP_HOST"


def test_different_company_sharing_a_provider_board_is_impossible_by_construction(db):
    """The board key is provider-scoped, so boards never collide across ATSes."""
    greenhouse = _resolve(
        db.conn, ats_provider="GREENHOUSE", ats_board="widgets",
        application_host="boards.greenhouse.io",
    )
    lever = _resolve(
        db.conn,
        name="Other Widgets",
        ats_provider="LEVER",
        ats_board="widgets",
        application_host="api.lever.co",
    )
    assert lever.company_id != greenhouse.company_id
    assert lever.decision == "CREATED"


def test_missing_company_fields_are_filled_in_non_destructively(db):
    first = _resolve(db.conn, ats_provider="GREENHOUSE", ats_board="acme")
    assert db.conn.execute(
        "SELECT country FROM companies WHERE id = ?", (first.company_id,)
    ).fetchone()["country"] is None
    _resolve(
        db.conn,
        ats_provider="GREENHOUSE",
        ats_board="acme",
        country="DE",
        careers_url="https://boards.greenhouse.io/acme",
    )
    company = db.conn.execute(
        "SELECT * FROM companies WHERE id = ?", (first.company_id,)
    ).fetchone()
    assert company["country"] == "DE"
    assert company["last_posting_at"] == NOW
    # a later sighting without a country must not clear it
    _resolve(db.conn, ats_provider="GREENHOUSE", ats_board="acme", country=None)
    again = db.conn.execute(
        "SELECT country FROM companies WHERE id = ?", (first.company_id,)
    ).fetchone()
    assert again["country"] == "DE"


def test_no_usable_signal_yields_no_company_and_records_the_reason(db):
    result = _resolve(db.conn, name=None)
    assert result.company_id is None
    assert result.decision == "NO_SIGNAL"
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0


def test_every_decision_is_durable_evidence(db):
    """The refused merge is as durable as the accepted one.

    The ``observation_id`` link itself is exercised end-to-end in
    ``test_company_location_provenance.py`` (a real ingest creates the
    observation), so this test pins the event content without hand-building
    foreign-key parents.
    """
    first = _resolve(db.conn, ats_provider="GREENHOUSE", ats_board="acme")
    _resolve(db.conn, name="Acme Analytics", ats_provider="GREENHOUSE", ats_board="acme")
    # an unrelated employer that merely normalizes to the same name
    _resolve(db.conn, application_host="other-careers.example")
    events = db.conn.execute(
        "SELECT * FROM company_resolution_events ORDER BY created_at, id"
    ).fetchall()
    assert [e["decision"] for e in events] == [
        "CREATED",
        "ATTACHED",
        "NAME_ONLY_NEW_COMPANY",
    ]
    assert events[0]["company_id"] == first.company_id
    assert events[0]["observation_id"] is None
    assert events[1]["matched_on"] == "ATS_BOARD"
    assert events[1]["name_conflict"] == 1
    assert events[1]["reason_code"] == "NAME_CONFLICT_REVIEW"
    assert events[2]["reason_code"] == "WEAK_EVIDENCE_NO_MERGE"
    assert events[2]["company_id"] != first.company_id
    import json

    assert json.loads(events[1]["signals_json"])["ats_board"] == "acme"
    assert all(e["resolution_version"] == COMPANY_RESOLUTION_VERSION for e in events)


def test_resolution_is_idempotent_and_does_not_duplicate_identifiers(db):
    first = _resolve(db.conn, ats_provider="GREENHOUSE", ats_board="acme")
    for _ in range(3):
        again = _resolve(db.conn, ats_provider="GREENHOUSE", ats_board="acme")
        assert again.company_id == first.company_id
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(DISTINCT kind || value) FROM company_identifiers"
    ).fetchone()[0] == db.conn.execute(
        "SELECT COUNT(*) FROM company_identifiers"
    ).fetchone()[0]


def test_structured_organization_domains_are_honoured(db):
    first = _resolve(db.conn, organization_domains=("jobs.acme.example",))
    second = _resolve(db.conn, name="Acme", organization_domains=("jobs.acme.example",))
    assert second.company_id == first.company_id
    assert second.matched_on == "ORG_DOMAIN"


def test_late_older_sighting_never_moves_last_posting_backwards(db):
    """``last_posting_at`` is a *last* posting stamp, not a write stamp.

    Sources are re-observed out of order (a low-priority page arriving late),
    so an attach carrying an older observation must not walk the company's
    recency backwards — a monotone ``MAX`` keeps the freshest evidence.
    """
    fresh = resolve_company(
        db.conn,
        signals=_signals(careers_url="https://careers.acme.example/jobs"),
        observation_id=None,
        observed_at="2026-09-08T18:00:00.000000Z",
        now="2026-09-08T18:00:00.000000Z",
    )
    assert fresh.decision == "CREATED"
    later = resolve_company(
        db.conn,
        signals=_signals(careers_url="https://careers.acme.example/jobs"),
        observation_id=None,
        observed_at=NOW,  # *older* than the first sighting's timestamp
        now="2026-09-08T19:00:00.000000Z",
    )
    assert later.decision == "ATTACHED"
    row = db.conn.execute(
        "SELECT last_posting_at FROM companies WHERE id = ?", (fresh.company_id,)
    ).fetchone()
    assert row["last_posting_at"] == "2026-09-08T18:00:00.000000Z"


def test_an_unresolved_origin_never_mints_a_board_identity():
    """02 §32: only a *resolved* origin identity may become an ``ATS_BOARD`` key.

    The resolver reports partial matches (board seen, confidence too low) as
    ``UNRESOLVED`` while still carrying the candidate values, and the substring
    ``UNRESOLVED`` ends with ``RESOLVED`` — so the comparison must be explicit,
    or a refused resolution would merge companies on evidence the resolver said
    was insufficient.
    """
    from types import SimpleNamespace

    from jobscraper.acquisition.origin import OriginResolution, OriginStatus
    from jobscraper.pipeline.companies import signals_from

    normalized = SimpleNamespace(
        company_name="Acme Data",
        company_country=None,
        careers_url="https://careers.acme.example/jobs",
        organization_domains=(),
    )
    partial = OriginResolution(
        status=OriginStatus.UNRESOLVED,
        confidence=0.4,
        origin_provider="GREENHOUSE",
        origin_board="acme",
        origin_job_id="1234567",
    )
    signals = signals_from(normalized=normalized, origin=partial)
    assert signals.ats_provider is None and signals.ats_board is None
    assert ("ATS_BOARD", "GREENHOUSE/acme") not in signals.identifier_keys()

    resolved = OriginResolution(
        status=OriginStatus.RESOLVED,
        confidence=0.95,
        origin_provider="GREENHOUSE",
        origin_board="acme",
        origin_job_id="1234567",
    )
    strong = signals_from(normalized=normalized, origin=resolved)
    assert strong.identifier_keys()[0] == ("ATS_BOARD", "GREENHOUSE/acme")
