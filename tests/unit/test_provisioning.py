"""S2.4 — Runtime provisioning tests (03 §50, RUN-17).

Provisioning idempotently creates only the immutable objects authorized by
a routing decision: adapter definitions, Source, SourceAdapterBinding,
BindingRevision, and fingerprint/route decision evidence rows. Adapters
must not gain direct authority to write these rows — provisioning is a
host-owned surface.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobscraper.adapters.fingerprint import AtsFingerprint
from jobscraper.adapters.router import RouteCandidate, RouteDecision, RouteOutcome
from jobscraper.runtime.provisioning import (
    ProvisioningError,
    provision_source_and_binding,
    record_fingerprint,
    record_route_decision,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def populated_db(db):
    """A database with the minimum rows required for provisioning tests."""
    conn = db.conn
    now = "2026-09-08T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO adapter_definitions (adapter_id, adapter_version,"
        " adapter_api_version, manifest_json, created_at)"
        " VALUES ('json_api_feed', '1.0.0', '1', '{}', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles (id, display_name, created_at)"
        " VALUES ('perm-default', 'default', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions"
        " (id, permission_profile_id, revision, policy_json, created_at)"
        " VALUES ('permrev-1', 'perm-default', 1, '{}', ?)",
        (now,),
    )
    conn.commit()
    return db


# ---------------------------------------------------------------------------
# Fingerprint recording (append-only evidence)
# ---------------------------------------------------------------------------

class TestRecordFingerprint:
    """Fingerprint evidence is append-only in ats_fingerprints."""

    def test_record_fingerprint_inserts_row(self, populated_db):
        conn = populated_db.conn
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=({"kind": "HOST", "value": "boards.greenhouse.io"},),
            recommended_adapter_id="greenhouse",
        )
        row_id = record_fingerprint(
            conn,
            source_id="src-test",
            url="https://boards.greenhouse.io/acme",
            fingerprint=fp,
            now="2026-09-08T00:00:01.000000Z",
        )
        assert row_id is not None
        row = conn.execute(
            "SELECT * FROM ats_fingerprints WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is not None
        assert row["source_id"] == "src-test"
        assert row["family"] == "GREENHOUSE"

    def test_record_fingerprint_is_append_only(self, populated_db):
        conn = populated_db.conn
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(),
            recommended_adapter_id="greenhouse",
        )
        r1 = record_fingerprint(
            conn, source_id="src-1", url="https://boards.greenhouse.io/acme",
            fingerprint=fp, now="2026-09-08T00:00:01.000000Z",
        )
        r2 = record_fingerprint(
            conn, source_id="src-1", url="https://boards.greenhouse.io/acme",
            fingerprint=fp, now="2026-09-08T00:00:02.000000Z",
        )
        assert r1 != r2
        count = conn.execute("SELECT COUNT(*) FROM ats_fingerprints").fetchone()[0]
        assert count == 2

    def test_record_fingerprint_stores_evidence_json(self, populated_db):
        conn = populated_db.conn
        fp = AtsFingerprint(
            family="GREENHOUSE",
            confidence=0.95,
            evidence=(
                {"kind": "HOST", "value": "boards.greenhouse.io", "strength": "STRONG"},
                {"kind": "SCRIPT_URL", "value": "https://boards.greenhouse.io/embed/job_board"},
            ),
            recommended_adapter_id="greenhouse",
        )
        row_id = record_fingerprint(
            conn, source_id="src-1", url="https://boards.greenhouse.io/acme",
            fingerprint=fp, now="2026-09-08T00:00:01.000000Z",
        )
        import json
        row = conn.execute(
            "SELECT evidence_json FROM ats_fingerprints WHERE id = ?", (row_id,)
        ).fetchone()
        evidence = json.loads(row["evidence_json"])
        assert len(evidence) == 2


# ---------------------------------------------------------------------------
# Route decision recording (append-only evidence)
# ---------------------------------------------------------------------------

class TestRecordRouteDecision:
    """Route decision evidence is append-only in source_route_decisions."""

    def test_record_route_decision_inserts_row(self, populated_db):
        conn = populated_db.conn
        fp = AtsFingerprint(
            family="GREENHOUSE", confidence=0.95,
            evidence=(), recommended_adapter_id="greenhouse",
        )
        decision = RouteDecision(
            outcome=RouteOutcome.SPECIALIZED,
            fingerprint_family="GREENHOUSE",
            fingerprint_confidence=0.95,
            candidates=(
                RouteCandidate(
                    strategy="PROVIDER_NATIVE",
                    execution_class="HTTP",
                    adapter_id="greenhouse",
                    adapter_version="1.0.0",
                    priority=0,
                ),
            ),
            unsupported_candidates=(),
            router_version=1,
        )
        row_id = record_route_decision(
            conn,
            source_id="src-test",
            fingerprint=fp,
            decision=decision,
            now="2026-09-08T00:00:01.000000Z",
        )
        assert row_id is not None
        row = conn.execute(
            "SELECT * FROM source_route_decisions WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is not None
        assert row["outcome"] == "SPECIALIZED"

    def test_record_route_decision_is_append_only(self, populated_db):
        conn = populated_db.conn
        fp = AtsFingerprint(
            family="GREENHOUSE", confidence=0.95,
            evidence=(), recommended_adapter_id="greenhouse",
        )
        decision = RouteDecision(
            outcome=RouteOutcome.SPECIALIZED,
            fingerprint_family="GREENHOUSE",
            fingerprint_confidence=0.95,
            candidates=(
                RouteCandidate(
                    strategy="PROVIDER_NATIVE",
                    execution_class="HTTP",
                    adapter_id="greenhouse",
                    adapter_version="1.0.0",
                    priority=0,
                ),
            ),
            unsupported_candidates=(),
            router_version=1,
        )
        r1 = record_route_decision(
            conn, source_id="src-1", fingerprint=fp,
            decision=decision, now="2026-09-08T00:00:01.000000Z",
        )
        r2 = record_route_decision(
            conn, source_id="src-1", fingerprint=fp,
            decision=decision, now="2026-09-08T00:00:02.000000Z",
        )
        assert r1 != r2
        count = conn.execute(
            "SELECT COUNT(*) FROM source_route_decisions"
        ).fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# Source and binding provisioning (idempotent)
# ---------------------------------------------------------------------------

class TestProvisionSourceAndBinding:
    """Provisioning creates Source + Binding + BindingRevision idempotently."""

    def test_provision_creates_source(self, populated_db):
        conn = populated_db.conn
        result = provision_source_and_binding(
            conn,
            display_name="Acme Careers",
            source_family="ATS",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="PROVIDER_NATIVE",
            execution_class="HTTP",
            now="2026-09-08T00:00:01.000000Z",
        )
        assert result.source_id is not None
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (result.source_id,)
        ).fetchone()
        assert row is not None
        assert row["display_name"] == "Acme Careers"
        assert row["canonical_host"] == "boards.greenhouse.io"

    def test_provision_creates_binding_and_revision(self, populated_db):
        conn = populated_db.conn
        result = provision_source_and_binding(
            conn,
            display_name="Acme Careers",
            source_family="ATS",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            now="2026-09-08T00:00:01.000000Z",
        )
        assert result.binding_id is not None
        assert result.binding_revision_id is not None
        rev = conn.execute(
            "SELECT * FROM source_adapter_binding_revisions WHERE id = ?",
            (result.binding_revision_id,),
        ).fetchone()
        assert rev is not None
        assert rev["adapter_id"] == "json_api_feed"
        assert rev["strategy"] == "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT"

    def test_provision_is_idempotent_for_same_source(self, populated_db):
        conn = populated_db.conn
        r1 = provision_source_and_binding(
            conn,
            display_name="Acme Careers",
            source_family="ATS",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            now="2026-09-08T00:00:01.000000Z",
        )
        r2 = provision_source_and_binding(
            conn,
            display_name="Acme Careers",
            source_family="ATS",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            now="2026-09-08T00:00:02.000000Z",
        )
        # Same canonical_host → same source
        assert r1.source_id == r2.source_id
        # Same binding
        assert r1.binding_id == r2.binding_id
        # Same adapter+strategy → same revision (idempotent)
        assert r1.binding_revision_id == r2.binding_revision_id
        # No extra source rows
        count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        assert count == 1

    def test_different_strategy_creates_new_revision(self, populated_db):
        conn = populated_db.conn
        r1 = provision_source_and_binding(
            conn,
            display_name="Acme Careers",
            source_family="ATS",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            now="2026-09-08T00:00:01.000000Z",
        )
        r2 = provision_source_and_binding(
            conn,
            display_name="Acme Careers",
            source_family="ATS",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="HTTP_HTML",
            execution_class="HTTP",
            now="2026-09-08T00:00:02.000000Z",
        )
        # Same source and binding
        assert r1.source_id == r2.source_id
        assert r1.binding_id == r2.binding_id
        # But different revision
        assert r1.binding_revision_id != r2.binding_revision_id

    def test_provision_requires_adapter_definition(self, populated_db):
        """A binding revision references adapter_definitions FK."""
        conn = populated_db.conn
        with pytest.raises(ProvisioningError):
            provision_source_and_binding(
                conn,
                display_name="Acme",
                source_family="ATS",
                entry_url="https://boards.greenhouse.io/acme",
                canonical_host="boards.greenhouse.io",
                adapter_id="nonexistent_adapter",
                adapter_version="1.0.0",
                strategy="PROVIDER_NATIVE",
                execution_class="HTTP",
                now="2026-09-08T00:00:01.000000Z",
            )

    def test_provision_records_fingerprint_decision(self, populated_db):
        """Provisioning can optionally link a fingerprint evidence row."""
        conn = populated_db.conn
        fp = AtsFingerprint(
            family="GREENHOUSE", confidence=0.95,
            evidence=(), recommended_adapter_id="greenhouse",
        )
        decision = RouteDecision(
            outcome=RouteOutcome.SPECIALIZED,
            fingerprint_family="GREENHOUSE",
            fingerprint_confidence=0.95,
            candidates=(
                RouteCandidate(
                    strategy="PROVIDER_NATIVE",
                    execution_class="HTTP",
                    adapter_id="json_api_feed",
                    adapter_version="1.0.0",
                    priority=0,
                ),
            ),
            unsupported_candidates=(),
            router_version=1,
        )
        result = provision_source_and_binding(
            conn,
            display_name="Acme Careers",
            source_family="ATS",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            now="2026-09-08T00:00:01.000000Z",
            fingerprint=fp,
            decision=decision,
        )
        assert result.fingerprint_id is not None
        assert result.route_decision_id is not None
