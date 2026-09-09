"""Corrective regressions for S2.4 fingerprint/route evidence causality.

The released v14 schema deliberately remains permissive for historical and
provisional rows.  These tests pin the stricter *runtime write boundary*:
new evidence must name a durable Source, and a route decision must be caused
by a matching fingerprint that was persisted first.
"""

from __future__ import annotations

import pytest

from jobscraper.adapters.fingerprint import AtsFingerprint
from jobscraper.adapters.router import RouteCandidate, RouteDecision, RouteOutcome
from jobscraper.runtime.provisioning import (
    ProvisioningError,
    provision_source_and_binding,
    record_fingerprint,
    record_route_decision,
)

NOW = "2026-09-09T00:00:00.000000Z"


def _source(conn, source_id: str = "src-evidence") -> str:
    conn.execute(
        "INSERT INTO sources "
        "(id, display_name, source_family, entry_url, canonical_host, created_at, updated_at) "
        "VALUES (?, 'Evidence Source', 'ATS_BOARD', 'https://boards.greenhouse.io/acme', "
        "'boards.greenhouse.io', ?, ?)",
        (source_id, NOW, NOW),
    )
    conn.commit()
    return source_id


def _fingerprint(*, family: str = "GREENHOUSE", confidence: float = 0.95):
    return AtsFingerprint(
        family=family,
        confidence=confidence,
        evidence=({"kind": "HOST", "value": "boards.greenhouse.io"},),
        recommended_adapter_id="greenhouse",
    )


def _decision(*, family: str = "GREENHOUSE", confidence: float = 0.95):
    return RouteDecision(
        outcome=RouteOutcome.SPECIALIZED,
        fingerprint_family=family,
        fingerprint_confidence=confidence,
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
        router_version=2,
    )


def _provisioning_prereqs(conn):
    conn.execute(
        "INSERT INTO adapter_definitions "
        "(adapter_id, adapter_version, adapter_api_version, manifest_json, is_builtin, created_at) "
        "VALUES ('json_api_feed', '1.0.0', '1', '{}', 1, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles (id, display_name, created_at) "
        "VALUES ('perm-evidence', 'evidence', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions "
        "(id, permission_profile_id, revision, policy_json, created_at) "
        "VALUES ('permrev-evidence', 'perm-evidence', 1, '{}', ?)",
        (NOW,),
    )
    conn.commit()


def test_new_fingerprint_write_requires_existing_source(db):
    with pytest.raises(ProvisioningError, match="Source|source"):
        record_fingerprint(
            db.conn,
            source_id="src-missing",
            url="https://boards.greenhouse.io/acme",
            fingerprint=_fingerprint(),
            now=NOW,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM ats_fingerprints").fetchone()[0] == 0


def test_fingerprint_for_existing_source_persists(db):
    source_id = _source(db.conn)
    row_id = record_fingerprint(
        db.conn,
        source_id=source_id,
        url="https://boards.greenhouse.io/acme",
        fingerprint=_fingerprint(),
        now=NOW,
    )
    row = db.conn.execute(
        "SELECT source_id, family FROM ats_fingerprints WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["source_id"] == source_id
    assert row["family"] == "GREENHOUSE"


def test_route_decision_requires_fingerprint_argument(db):
    source_id = _source(db.conn)
    with pytest.raises(ProvisioningError, match="fingerprint"):
        record_route_decision(
            db.conn,
            source_id=source_id,
            fingerprint=None,
            decision=_decision(),
            now=NOW,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM source_route_decisions").fetchone()[0] == 0


def test_route_decision_requires_existing_source(db):
    with pytest.raises(ProvisioningError, match="Source|source"):
        record_route_decision(
            db.conn,
            source_id="src-missing",
            fingerprint=_fingerprint(),
            decision=_decision(),
            now=NOW,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM source_route_decisions").fetchone()[0] == 0


def test_route_decision_requires_prior_durable_matching_fingerprint(db):
    source_id = _source(db.conn)
    with pytest.raises(ProvisioningError, match="persist|fingerprint"):
        record_route_decision(
            db.conn,
            source_id=source_id,
            fingerprint=_fingerprint(),
            decision=_decision(),
            now=NOW,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM source_route_decisions").fetchone()[0] == 0


def test_route_decision_rejects_family_or_confidence_mismatch(db):
    source_id = _source(db.conn)
    fp = _fingerprint()
    record_fingerprint(
        db.conn,
        source_id=source_id,
        url="https://boards.greenhouse.io/acme",
        fingerprint=fp,
        now=NOW,
    )

    with pytest.raises(ProvisioningError, match="fingerprint|family"):
        record_route_decision(
            db.conn,
            source_id=source_id,
            fingerprint=fp,
            decision=_decision(family="LEVER"),
            now=NOW,
        )
    with pytest.raises(ProvisioningError, match="fingerprint|confidence"):
        record_route_decision(
            db.conn,
            source_id=source_id,
            fingerprint=fp,
            decision=_decision(confidence=0.94),
            now=NOW,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM source_route_decisions").fetchone()[0] == 0


def test_matching_persisted_fingerprint_allows_route_decision(db):
    source_id = _source(db.conn)
    fp = _fingerprint()
    record_fingerprint(
        db.conn,
        source_id=source_id,
        url="https://boards.greenhouse.io/acme",
        fingerprint=fp,
        now=NOW,
    )
    row_id = record_route_decision(
        db.conn,
        source_id=source_id,
        fingerprint=fp,
        decision=_decision(),
        now=NOW,
    )
    assert db.conn.execute(
        "SELECT source_id FROM source_route_decisions WHERE id = ?", (row_id,)
    ).fetchone()["source_id"] == source_id


def test_provisioning_decision_without_fingerprint_refuses_before_mutation(db):
    _provisioning_prereqs(db.conn)
    with pytest.raises(ProvisioningError, match="fingerprint"):
        provision_source_and_binding(
            db.conn,
            display_name="Acme feed",
            source_family="ATS_BOARD",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="PROVIDER_NATIVE",
            execution_class="HTTP",
            config=None,
            fingerprint=None,
            decision=_decision(),
            now=NOW,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM source_adapter_bindings").fetchone()[0] == 0


def test_provisioning_mismatched_decision_refuses_before_mutation(db):
    _provisioning_prereqs(db.conn)
    with pytest.raises(ProvisioningError, match="fingerprint|family"):
        provision_source_and_binding(
            db.conn,
            display_name="Acme feed",
            source_family="ATS_BOARD",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="json_api_feed",
            adapter_version="1.0.0",
            strategy="PROVIDER_NATIVE",
            execution_class="HTTP",
            config=None,
            fingerprint=_fingerprint(),
            decision=_decision(family="LEVER"),
            now=NOW,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM source_adapter_bindings").fetchone()[0] == 0
