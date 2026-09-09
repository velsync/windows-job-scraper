"""S2.4 — Runtime provisioning tests (03 §50, RUN-17).

Provisioning idempotently creates only the immutable objects authorized by
a routing decision: adapter definitions, Source, SourceAdapterBinding,
BindingRevision, and fingerprint/route decision evidence rows. Adapters
must not gain direct authority to write these rows — provisioning is a
host-owned surface.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from jobscraper.adapters.contract import validate_manifest
from jobscraper.adapters.fingerprint import AtsFingerprint
from jobscraper.adapters.router import (
    RouteCandidate,
    RouteDecision,
    RouteOutcome,
    plan_routes,
)
from jobscraper.runtime.provisioning import (
    ProvisioningError,
    ensure_builtin_adapter_definition,
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


@pytest.fixture()
def greenhouse_db(db):
    """populated_db plus the built-in Greenhouse adapter identity."""
    from jobscraper.runtime.provisioning import ensure_builtin_adapter_definition

    conn = db.conn
    conn.execute(
        "INSERT INTO adapter_definitions (adapter_id, adapter_version,"
        " adapter_api_version, manifest_json, created_at)"
        " VALUES ('json_api_feed', '1.0.0', '1', '{}',"
        " '2026-09-08T00:00:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles (id, display_name, created_at)"
        " VALUES ('perm-default', 'default', '2026-09-08T00:00:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions"
        " (id, permission_profile_id, revision, policy_json, created_at)"
        " VALUES ('permrev-1', 'perm-default', 1, '{}', '2026-09-08T00:00:00.000000Z')"
    )
    conn.commit()
    ensure_builtin_adapter_definition(
        conn, "greenhouse", now="2026-09-08T00:00:00.000000Z"
    )
    return db


def _seed_source(conn, source_id: str) -> None:
    now = "2026-09-08T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO sources "
        "(id, display_name, source_family, entry_url, canonical_host, created_at, updated_at) "
        "VALUES (?, ?, 'ATS_BOARD', ?, 'boards.greenhouse.io', ?, ?)",
        (source_id, f"Evidence {source_id}", f"https://boards.greenhouse.io/{source_id}", now, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fingerprint recording (append-only evidence)
# ---------------------------------------------------------------------------

class TestRecordFingerprint:
    """Fingerprint evidence is append-only in ats_fingerprints."""

    def test_record_fingerprint_inserts_row(self, populated_db):
        conn = populated_db.conn
        _seed_source(conn, "src-test")
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
        _seed_source(conn, "src-1")
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
        _seed_source(conn, "src-1")
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
        _seed_source(conn, "src-test")
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
        record_fingerprint(
            conn, source_id="src-test", url="https://boards.greenhouse.io/acme",
            fingerprint=fp, now="2026-09-08T00:00:00.500000Z",
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
        _seed_source(conn, "src-1")
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
        record_fingerprint(
            conn, source_id="src-1", url="https://boards.greenhouse.io/acme",
            fingerprint=fp, now="2026-09-08T00:00:00.500000Z",
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

    def test_a_family_less_fallback_decision_can_be_recorded(self, populated_db):
        """S2.8 acceptance finding F1 (red-first).

        A generic careers page classifies to ``family=None``; the router's
        honest answer is ``GENERIC_DISCOVERY_FALLBACK`` with no runnable
        candidate.  That decision is exactly the durable evidence 02 §12.1
        requires for the fallback — but the prior-fingerprint lookup compared
        ``family = ?``, and ``family = NULL`` matches no row in SQL, so
        recording the fallback crashed with ``ProvisioningError`` instead of
        recording it.  The no-family case must be recordable like any other.
        """
        conn = populated_db.conn
        _seed_source(conn, "src-none")
        fp = AtsFingerprint(
            family=None, confidence=0.0, evidence=(), recommended_adapter_id=None,
        )
        decision = plan_routes(
            fingerprint=fp,
            supported_execution_classes=frozenset({"HTTP"}),
        )
        assert decision.outcome is RouteOutcome.GENERIC_DISCOVERY_FALLBACK

        record_fingerprint(
            conn, source_id="src-none", url="https://careers.acme.example/jobs",
            fingerprint=fp, now="2026-09-10T00:00:00.500000Z",
        )
        row_id = record_route_decision(
            conn, source_id="src-none", fingerprint=fp,
            decision=decision, now="2026-09-10T00:00:01.000000Z",
        )
        row = conn.execute(
            "SELECT * FROM source_route_decisions WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["outcome"] == "GENERIC_DISCOVERY_FALLBACK"
        assert row["fingerprint_family"] is None


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


# ---------------------------------------------------------------------------
# S2.5 — adapter config pinning (02 §8/§9, 03 §50, RUN-17)
# ---------------------------------------------------------------------------

def _provision_greenhouse(conn, *, config=None, now="2026-09-08T00:00:01.000000Z"):
    return provision_source_and_binding(
        conn,
        display_name="Acme Careers",
        source_family="ATS",
        entry_url="https://boards.greenhouse.io/acme",
        canonical_host="boards.greenhouse.io",
        adapter_id="greenhouse",
        adapter_version="1.0.0",
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        config=config,
        now=now,
    )


class TestBindingRevisionConfig:
    """The adapter config a run will use is pinned into the immutable revision.

    A provider adapter cannot run without one (which board? what bounds?), and
    the config must be part of the revision identity — not mutable state read
    at run time — so an executed run always replays against exactly the config
    it was authorized with (ARC-04.3, 03 §50).
    """

    def test_config_is_stored_on_the_revision(self, greenhouse_db):
        conn = greenhouse_db.conn
        result = _provision_greenhouse(conn, config={"board": "acme"})
        rev = conn.execute(
            "SELECT config_json FROM source_adapter_binding_revisions WHERE id = ?",
            (result.binding_revision_id,),
        ).fetchone()
        assert json.loads(rev["config_json"]) == {"board": "acme"}

    def test_config_absent_defaults_to_empty_object(self, greenhouse_db):
        """Back-compat: existing callers pass no config and get ``{}``."""
        conn = greenhouse_db.conn
        result = _provision_greenhouse(conn)
        rev = conn.execute(
            "SELECT config_json FROM source_adapter_binding_revisions WHERE id = ?",
            (result.binding_revision_id,),
        ).fetchone()
        assert json.loads(rev["config_json"]) == {}

    def test_same_config_reuses_the_same_revision(self, greenhouse_db):
        conn = greenhouse_db.conn
        config = {"board": "acme", "detail_fetch": True}
        r1 = _provision_greenhouse(conn, config=config)
        r2 = _provision_greenhouse(
            conn, config=dict(config), now="2026-09-08T00:00:02.000000Z"
        )
        assert r1.binding_revision_id == r2.binding_revision_id
        assert r1.source_id == r2.source_id
        assert conn.execute(
            "SELECT COUNT(*) FROM source_adapter_binding_revisions"
        ).fetchone()[0] == 1

    def test_config_key_order_does_not_change_revision_identity(self, greenhouse_db):
        """Serialization is deterministic, so equality is not spelling luck."""
        conn = greenhouse_db.conn
        r1 = _provision_greenhouse(conn, config={"board": "acme", "detail_fetch": True})
        r2 = _provision_greenhouse(
            conn,
            config={"detail_fetch": True, "board": "acme"},
            now="2026-09-08T00:00:02.000000Z",
        )
        assert r1.binding_revision_id == r2.binding_revision_id

    def test_changed_config_creates_a_new_revision_and_never_mutates_the_old(
        self, greenhouse_db
    ):
        """Immutability: re-pointing a board is a new authorization, not an edit."""
        conn = greenhouse_db.conn
        r1 = _provision_greenhouse(conn, config={"board": "acme"})
        r2 = _provision_greenhouse(
            conn, config={"board": "globex"}, now="2026-09-08T00:00:02.000000Z"
        )
        assert r1.binding_id == r2.binding_id
        assert r1.binding_revision_id != r2.binding_revision_id
        old = conn.execute(
            "SELECT revision, config_json FROM source_adapter_binding_revisions"
            " WHERE id = ?",
            (r1.binding_revision_id,),
        ).fetchone()
        new = conn.execute(
            "SELECT revision, config_json FROM source_adapter_binding_revisions"
            " WHERE id = ?",
            (r2.binding_revision_id,),
        ).fetchone()
        assert json.loads(old["config_json"]) == {"board": "acme"}
        assert json.loads(new["config_json"]) == {"board": "globex"}
        assert new["revision"] == old["revision"] + 1

    def test_the_revision_is_pinned_as_the_bindings_current_one(self, greenhouse_db):
        """A provisioned revision must be runnable, not just recorded.

        The host's run planner selects plans through
        ``source_adapter_bindings.current_revision_id``; provisioning is the
        surface that authorized this adapter+config, so it is also the surface
        that points the binding at it.
        """
        conn = greenhouse_db.conn
        result = _provision_greenhouse(conn, config={"board": "acme"})
        binding = conn.execute(
            "SELECT current_revision_id FROM source_adapter_bindings WHERE id = ?",
            (result.binding_id,),
        ).fetchone()
        assert binding["current_revision_id"] == result.binding_revision_id

    def test_reprovisioning_moves_the_pin_and_keeps_the_old_revision(self, greenhouse_db):
        conn = greenhouse_db.conn
        first = _provision_greenhouse(conn, config={"board": "acme"})
        second = _provision_greenhouse(
            conn, config={"board": "globex"}, now="2026-09-08T00:00:02.000000Z"
        )
        binding = conn.execute(
            "SELECT current_revision_id FROM source_adapter_bindings WHERE id = ?",
            (second.binding_id,),
        ).fetchone()
        assert binding["current_revision_id"] == second.binding_revision_id
        # the superseded authorization stays inspectable and unchanged
        old = conn.execute(
            "SELECT config_json, superseded_at FROM source_adapter_binding_revisions"
            " WHERE id = ?",
            (first.binding_revision_id,),
        ).fetchone()
        assert json.loads(old["config_json"]) == {"board": "acme"}
        assert old["superseded_at"] is None

    def test_pinning_is_idempotent(self, greenhouse_db):
        conn = greenhouse_db.conn
        config = {"board": "acme"}
        first = _provision_greenhouse(conn, config=config)
        again = _provision_greenhouse(
            conn, config=dict(config), now="2026-09-08T00:00:03.000000Z"
        )
        assert again.binding_revision_id == first.binding_revision_id
        assert conn.execute(
            "SELECT COUNT(*) FROM source_adapter_bindings"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT current_revision_id FROM source_adapter_bindings"
        ).fetchone()["current_revision_id"] == first.binding_revision_id

    def test_config_reaching_the_driver_is_the_pinned_one(self, greenhouse_db):
        """The driver reads config from the revision — one source of truth."""
        from jobscraper.pipeline.driver import _plan_config

        conn = greenhouse_db.conn
        result = _provision_greenhouse(conn, config={"board": "acme", "max_detail_requests": 7})
        plan_row = conn.execute(
            "SELECT ? AS binding_revision_id", (result.binding_revision_id,)
        ).fetchone()
        assert _plan_config(conn, plan_row) == {
            "board": "acme",
            "max_detail_requests": 7,
        }


# ---------------------------------------------------------------------------
# S2.5 — built-in adapter definitions come from the registry manifest
# ---------------------------------------------------------------------------

class TestEnsureBuiltinAdapterDefinition:
    """A built-in adapter's durable identity row is derived from its manifest.

    The registry is the only place an adapter identity is declared in code, so
    provisioning must read the manifest rather than accept a hand-written
    identity: a durable row that disagrees with the code it authorizes would
    make provenance unverifiable (02 §8, ARC-04.3).
    """

    def test_ensure_registers_greenhouse_from_the_manifest(self, db):
        from jobscraper.adapters.greenhouse import MANIFEST

        conn = db.conn
        definition = ensure_builtin_adapter_definition(
            conn, "greenhouse", now="2026-09-08T00:00:01.000000Z"
        )
        assert definition.created is True
        assert definition.adapter_id == "greenhouse"
        assert definition.adapter_version == MANIFEST.version
        row = conn.execute(
            "SELECT * FROM adapter_definitions WHERE adapter_id = 'greenhouse'"
        ).fetchone()
        assert row["adapter_api_version"] == MANIFEST.adapter_api_version
        assert row["is_builtin"] == 1
        assert row["created_at"] == "2026-09-08T00:00:01.000000Z"
        stored = json.loads(row["manifest_json"])
        assert stored["id"] == "greenhouse"
        assert stored["version"] == MANIFEST.version
        assert list(stored["capabilities"]) == list(MANIFEST.capabilities)
        # the durable row must round-trip through the same validation the
        # registry applies, so a corrupt row cannot be read back as an adapter
        assert validate_manifest(stored).id == "greenhouse"

    def test_ensure_is_idempotent(self, db):
        conn = db.conn
        first = ensure_builtin_adapter_definition(
            conn, "greenhouse", now="2026-09-08T00:00:01.000000Z"
        )
        second = ensure_builtin_adapter_definition(
            conn, "greenhouse", now="2026-09-08T00:00:05.000000Z"
        )
        assert first.created is True
        assert second.created is False
        assert second.adapter_version == first.adapter_version
        assert conn.execute("SELECT COUNT(*) FROM adapter_definitions").fetchone()[0] == 1
        row = conn.execute(
            "SELECT created_at FROM adapter_definitions WHERE adapter_id = 'greenhouse'"
        ).fetchone()
        assert row["created_at"] == "2026-09-08T00:00:01.000000Z"

    def test_ensure_never_overwrites_an_existing_identity_row(self, db):
        """Append-only identity: a pre-existing row wins, it is not rewritten."""
        conn = db.conn
        conn.execute(
            "INSERT INTO adapter_definitions (adapter_id, adapter_version,"
            " adapter_api_version, manifest_json, is_builtin, created_at)"
            " VALUES ('greenhouse', '1.0.0', '1', '{\"hand\":\"written\"}', 1,"
            " '2026-01-01T00:00:00.000000Z')"
        )
        conn.commit()
        definition = ensure_builtin_adapter_definition(
            conn, "greenhouse", now="2026-09-08T00:00:01.000000Z"
        )
        assert definition.created is False
        row = conn.execute(
            "SELECT manifest_json, created_at FROM adapter_definitions"
            " WHERE adapter_id = 'greenhouse'"
        ).fetchone()
        assert json.loads(row["manifest_json"]) == {"hand": "written"}
        assert row["created_at"] == "2026-01-01T00:00:00.000000Z"

    def test_ensure_refuses_an_adapter_the_registry_does_not_define(self, db):
        conn = db.conn
        with pytest.raises(ProvisioningError):
            ensure_builtin_adapter_definition(conn, "workday")
        assert conn.execute("SELECT COUNT(*) FROM adapter_definitions").fetchone()[0] == 0

    def test_ensure_pins_the_requested_version_when_given(self, db):
        conn = db.conn
        definition = ensure_builtin_adapter_definition(
            conn, "greenhouse", adapter_version="1.0.0", now="2026-09-08T00:00:01.000000Z"
        )
        assert definition.adapter_version == "1.0.0"
        with pytest.raises(ProvisioningError):
            # a version the manifest does not declare is not this adapter's identity
            ensure_builtin_adapter_definition(conn, "greenhouse", adapter_version="9.9.9")

    def test_ensure_then_provision_produces_a_runnable_binding(self, db):
        """The two host surfaces compose: define the adapter, then bind it."""
        conn = db.conn
        ensure_builtin_adapter_definition(
            conn, "greenhouse", now="2026-09-08T00:00:01.000000Z"
        )
        conn.execute(
            "INSERT INTO adapter_permission_profiles (id, display_name, created_at)"
            " VALUES ('perm-default', 'default', '2026-09-08T00:00:01.000000Z')"
        )
        conn.execute(
            "INSERT INTO adapter_permission_profile_revisions"
            " (id, permission_profile_id, revision, policy_json, created_at)"
            " VALUES ('permrev-1', 'perm-default', 1, '{}', '2026-09-08T00:00:01.000000Z')"
        )
        conn.commit()
        result = _provision_greenhouse(conn, config={"board": "acme"})
        assert result.binding_revision_id
        assert conn.execute(
            "SELECT adapter_id FROM source_adapter_binding_revisions WHERE id = ?",
            (result.binding_revision_id,),
        ).fetchone()["adapter_id"] == "greenhouse"
