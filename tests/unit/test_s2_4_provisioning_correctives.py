"""Corrective regressions for S2.4/S2.5 provisioning audit findings."""

from __future__ import annotations

import pytest

from jobscraper.runtime.provisioning import (
    ProvisioningError,
    ensure_builtin_adapter_definition,
    provision_source_and_binding,
)

NOW = "2026-09-09T00:00:00.000000Z"


def _permissions(conn):
    conn.execute(
        "INSERT INTO adapter_permission_profiles (id, display_name, created_at) "
        "VALUES ('perm-corrective', 'corrective', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions "
        "(id, permission_profile_id, revision, policy_json, created_at) "
        "VALUES ('permrev-corrective', 'perm-corrective', 1, '{}', ?)",
        (NOW,),
    )
    conn.commit()


def _greenhouse(conn, board: str, *, entry_url: str | None = None, config=None):
    definition = ensure_builtin_adapter_definition(conn, "greenhouse", now=NOW)
    assert definition.verified
    return provision_source_and_binding(
        conn,
        display_name=f"{board} board",
        source_family="ATS_BOARD",
        entry_url=entry_url or f"https://boards.greenhouse.io/{board}",
        canonical_host="boards.greenhouse.io",
        adapter_id="greenhouse",
        adapter_version=definition.adapter_version,
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        config={"board": board} if config is None else config,
        now=NOW,
    )


def test_two_boards_on_the_same_ats_host_are_distinct_sources(db):
    """A Source is one collection target, not one shared provider hostname."""
    _permissions(db.conn)
    acme = _greenhouse(db.conn, "acme")
    globex = _greenhouse(db.conn, "globex")

    assert acme.source_id != globex.source_id
    assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2


def test_same_board_target_remains_idempotent(db):
    _permissions(db.conn)
    first = _greenhouse(db.conn, "acme")
    second = _greenhouse(db.conn, "acme")

    assert first.source_id == second.source_id
    assert first.binding_id == second.binding_id
    assert first.binding_revision_id == second.binding_revision_id


def test_invalid_greenhouse_config_is_refused_before_activation(db):
    _permissions(db.conn)
    ensure_builtin_adapter_definition(db.conn, "greenhouse", now=NOW)

    with pytest.raises(ProvisioningError):
        _greenhouse(db.conn, "acme", config={"board": "../evil"})

    assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM source_adapter_bindings").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM source_adapter_binding_revisions").fetchone()[0] == 0


def test_missing_greenhouse_config_never_becomes_current_authority(db):
    """Historical empty revisions may be retained, but they must not be runnable."""
    _permissions(db.conn)
    definition = ensure_builtin_adapter_definition(db.conn, "greenhouse", now=NOW)
    result = provision_source_and_binding(
        db.conn,
        display_name="Acme board",
        source_family="ATS_BOARD",
        entry_url="https://boards.greenhouse.io/acme",
        canonical_host="boards.greenhouse.io",
        adapter_id="greenhouse",
        adapter_version=definition.adapter_version,
        strategy="PROVIDER_NATIVE",
        execution_class="HTTP",
        config=None,
        now=NOW,
    )

    binding = db.conn.execute(
        "SELECT current_revision_id FROM source_adapter_bindings WHERE id = ?",
        (result.binding_id,),
    ).fetchone()
    assert binding["current_revision_id"] is None


def test_existing_builtin_identity_with_wrong_manifest_is_explicitly_unverified_and_unusable(db):
    db.conn.execute(
        "INSERT INTO adapter_definitions "
        "(adapter_id, adapter_version, adapter_api_version, manifest_json, is_builtin, created_at) "
        "VALUES ('greenhouse', '1.0.0', '1', '{\"hand\":\"written\"}', 1, ?)",
        (NOW,),
    )
    db.conn.commit()

    definition = ensure_builtin_adapter_definition(db.conn, "greenhouse", now=NOW)
    assert definition.created is False
    assert definition.verified is False
    assert definition.conflict_reason and "manifest" in definition.conflict_reason

    _permissions(db.conn)
    with pytest.raises(ProvisioningError, match="manifest|definition|activated"):
        provision_source_and_binding(
            db.conn,
            display_name="Acme board",
            source_family="ATS_BOARD",
            entry_url="https://boards.greenhouse.io/acme",
            canonical_host="boards.greenhouse.io",
            adapter_id="greenhouse",
            adapter_version="1.0.0",
            strategy="PROVIDER_NATIVE",
            execution_class="HTTP",
            config={"board": "acme"},
            now=NOW,
        )
