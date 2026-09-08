"""Corrective regressions for the reviewed S2.2 resolution edge cases.

These tests deliberately stay small: each one pins a single architecture
invariant that was missing from the original S2.2 coverage.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import jobscraper.pipeline.ingest as ingest_module
from jobscraper.acquisition.origin import OriginResolution, OriginStatus
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.companies import CompanySignals, resolve_company
from jobscraper.pipeline.entity import EntityResolution, resolve_entity

NOW = "2026-09-08T09:00:00.000000Z"


def test_shared_ats_platform_hosts_are_not_company_merge_identifiers():
    """01 §33.1: shared ATS infrastructure is not employer identity.

    Different Greenhouse tenants share boards.greenhouse.io.  The provider +
    board token is strong; the platform host itself must remain ordinary
    evidence rather than a globally unique company merge key.
    """
    acme = CompanySignals(
        name="Acme",
        ats_provider="GREENHOUSE",
        ats_board="acme",
        application_host="boards.greenhouse.io",
        careers_url="https://boards.greenhouse.io/acme",
    )
    beta = CompanySignals(
        name="Beta",
        ats_provider="GREENHOUSE",
        ats_board="beta",
        application_host="boards.greenhouse.io",
        careers_url="https://boards.greenhouse.io/beta",
    )

    acme_keys = set(acme.identifier_keys())
    beta_keys = set(beta.identifier_keys())

    assert ("ATS_BOARD", "GREENHOUSE/acme") in acme_keys
    assert ("ATS_BOARD", "GREENHOUSE/beta") in beta_keys
    assert ("APP_HOST", "boards.greenhouse.io") not in acme_keys
    assert ("APP_HOST", "boards.greenhouse.io") not in beta_keys
    assert ("CAREERS_HOST", "boards.greenhouse.io") not in acme_keys
    assert ("CAREERS_HOST", "boards.greenhouse.io") not in beta_keys
    assert acme_keys.isdisjoint(beta_keys)

    employer_owned = CompanySignals(
        name="Acme",
        application_host="jobs.acme.example",
        careers_url="https://careers.acme.example/jobs",
    )
    assert ("APP_HOST", "jobs.acme.example") in employer_owned.identifier_keys()
    assert ("CAREERS_HOST", "careers.acme.example") in employer_owned.identifier_keys()


def test_two_greenhouse_tenants_on_the_same_platform_host_do_not_merge(tmp_path):
    """Regression for the actual v1 failure mode, not only its key-generation cause."""
    db = Database(tmp_path / "platform-host-company-resolution.db")
    try:
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
        acme = resolve_company(
            db.conn,
            signals=CompanySignals(
                name="Acme",
                ats_provider="GREENHOUSE",
                ats_board="acme",
                application_host="boards.greenhouse.io",
                careers_url="https://boards.greenhouse.io/acme",
            ),
            observed_at=NOW,
            now=NOW,
        )
        beta = resolve_company(
            db.conn,
            signals=CompanySignals(
                name="Beta",
                ats_provider="GREENHOUSE",
                ats_board="beta",
                application_host="boards.greenhouse.io",
                careers_url="https://boards.greenhouse.io/beta",
            ),
            observed_at=NOW,
            now=NOW,
        )

        assert acme.company_id != beta.company_id
        assert acme.decision == "CREATED"
        assert beta.decision == "CREATED"
        assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2
        keys = {
            (row["kind"], row["value"])
            for row in db.conn.execute(
                "SELECT kind, value FROM company_identifiers ORDER BY kind, value"
            )
        }
        assert keys == {
            ("ATS_BOARD", "GREENHOUSE/acme"),
            ("ATS_BOARD", "GREENHOUSE/beta"),
        }
    finally:
        db.close()


def _origin_match_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            company_id TEXT,
            title TEXT
        );
        CREATE TABLE companies (
            id TEXT PRIMARY KEY,
            normalized_name TEXT
        );
        CREATE TABLE job_sources (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            origin_provider TEXT,
            origin_board TEXT,
            origin_job_id TEXT,
            last_seen_at TEXT NOT NULL,
            source_identity_generation INTEGER NOT NULL
        );
        CREATE TABLE job_locations (
            job_id TEXT NOT NULL,
            city TEXT,
            country TEXT
        );
        INSERT INTO jobs (id, company_id, title)
            VALUES ('job-a', NULL, 'Staff Backend Engineer');
        INSERT INTO job_sources (
            id, job_id, source_id, origin_provider, origin_board, origin_job_id,
            last_seen_at, source_identity_generation
        ) VALUES (
            'presence-a', 'job-a', 'source-a', 'GREENHOUSE', 'acme', '1234567',
            '2026-09-08T09:00:00.000000Z', 7
        );
        """
    )
    return conn


def test_cross_source_origin_attach_starts_its_own_identity_generation():
    """RUN-15 generation belongs to the new source/native identity, not its peer."""
    conn = _origin_match_connection()
    try:
        normalized = SimpleNamespace(
            normalized_company=None,
            title="Staff Backend Engineer",
            location_records=(),
        )
        origin = OriginResolution(
            status=OriginStatus.RESOLVED,
            confidence=0.95,
            origin_provider="GREENHOUSE",
            origin_board="acme",
            origin_job_id="1234567",
        )
        result = resolve_entity(
            conn,
            source_id="source-b",
            source_job_id="other-native-id",
            normalized=normalized,
            observed_at="2026-09-08T10:00:00.000000Z",
            existing=None,
            origin=origin,
        )
    finally:
        conn.close()

    assert result.decision == "MATCHED_ORIGIN"
    assert result.job_id == "job-a"
    assert result.generation == 1


def test_entity_resolution_event_stage_matches_the_actual_resolution_stage():
    """Durable stage labels must agree with the evidence/decision that produced them."""
    stage_for = getattr(ingest_module, "_resolution_event_stage", None)
    assert callable(stage_for), "ingest must centrally map resolution evidence to durable stage"

    assert stage_for(
        EntityResolution(
            "job-a", "MATCHED_ORIGIN", 1, False, {"stage": "origin_identity"}
        )
    ) == "origin_identity_stage2"
    assert stage_for(
        EntityResolution(
            "job-a", "MATCHED_URL", 1, False, {"stage": "canonical_url", "url": "x"}
        )
    ) == "canonical_url_stage3"
    assert stage_for(
        EntityResolution("job-a", "MATCHED", 1, False, {})
    ) == "native_identity_stage1"
    assert stage_for(
        EntityResolution(None, "SPLIT_REUSE", 2, True, {"company_incompatible": True})
    ) == "native_identity_reuse_guard"
