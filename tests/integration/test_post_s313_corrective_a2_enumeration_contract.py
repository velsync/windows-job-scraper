"""Post-S3.13 Corrective A2 — pinned EnumerationContract (RED-first).

Authority: docs/reviews/post-s3.13-corrective-a-enumeration-lifecycle-authority-2026-09-13.md
Plan: 2026-09-13-post-s313-corrective-a-a0-a5-implementation-plan.md §5 (A2).

RED rule: every test here must fail by behavioral assertion before A2
production changes (no collection/import/setup failures), and pass after.
New-symbol imports are guarded so a missing feature yields pytest.fail()
(assertion failure, exit 1) rather than ImportError/collection ERROR.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import threading

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.runs import create_run

NOW = "2026-09-13T00:00:00.000000Z"

try:  # guarded: missing feature must be assertion failure, not import ERROR
    from jobscraper.adapters.contract import EnumerationContract  # type: ignore

    HAS_CONTRACT = True
except Exception:  # noqa: BLE001 - intentional RED guard
    HAS_CONTRACT = False
    EnumerationContract = None  # type: ignore

try:
    from jobscraper.adapters.registry import resolve_enumeration_contract  # type: ignore

    HAS_RESOLVER = True
except Exception:  # noqa: BLE001 - intentional RED guard
    HAS_RESOLVER = False
    resolve_enumeration_contract = None  # type: ignore


# ---------------------------------------------------------------- helpers

_FEED_FIELDS = {
    "source_job_id": {"path": "id", "required": True},
    "title": {"path": "title", "required": True},
}


def _base_config(port: int) -> dict:
    return {
        "url_template": f"http://127.0.0.1:{port}/jobs?page={{page}}",
        "items_path": "jobs",
        "fields": _FEED_FIELDS,
    }


def _init_db(db_path, *, feed_config: dict) -> Database:
    database = Database(db_path)
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    conn = database.conn
    conn.executescript(
        f"""
        INSERT INTO sources (id, display_name, source_family, entry_url, created_at, updated_at)
        VALUES ('src-1','Fixture Feed','PUBLIC_FEED','http://127.0.0.1:9/jobs','{NOW}','{NOW}');
        INSERT INTO adapter_definitions (adapter_id, adapter_version, adapter_api_version, manifest_json, created_at)
        VALUES ('json_api_feed','1.0.0','1','{{}}','{NOW}');
        INSERT INTO adapter_permission_profiles (id, display_name, created_at) VALUES ('perm-1','d','{NOW}');
        INSERT INTO adapter_permission_profile_revisions (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-1','perm-1',1,'{{}}','{NOW}');
        INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)
        VALUES ('bnd-1','src-1','api','{NOW}');
        """
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id, binding_id, revision, adapter_id, adapter_version, strategy,"
        " execution_class, permission_profile_id, permission_profile_revision,"
        " config_json, created_at)"
        " VALUES ('bndrev-1','bnd-1',1,'json_api_feed','1.0.0',"
        " 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','perm-1',1,?,?)",
        (json.dumps(feed_config), NOW),
    )
    conn.commit()
    from jobscraper.runtime.clock import begin_service_epoch

    begin_service_epoch(conn)
    return database


def _plan(**overrides) -> dict:
    base = dict(
        source_id="src-1",
        source_plan_group_id="grp-1",
        fallback_rank=0,
        binding_id="bnd-1",
        binding_revision_id="bndrev-1",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        adapter_api_version="1",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        execution_class="HTTP",
        permission_profile_id="perm-1",
        permission_profile_revision=1,
    )
    base.update(overrides)
    return base


# ------------------------------------------------------------- A2.1 value object


def test_a21_rejects_authoritative_full_source_with_unknown_stability():
    if not HAS_CONTRACT:
        pytest.fail("EnumerationContract value object missing")
    with pytest.raises(ValueError):
        EnumerationContract(  # type: ignore
            version=1,
            coverage_authority="AUTHORITATIVE_FULL_SOURCE",
            scope_key="full-source",
            pagination_stability="UNKNOWN",
            listing_identity_sufficient=True,
        )


def test_a21_rejects_authoritative_declared_scope_with_unknown_stability():
    if not HAS_CONTRACT:
        pytest.fail("EnumerationContract value object missing")
    with pytest.raises(ValueError):
        EnumerationContract(  # type: ignore
            version=1,
            coverage_authority="AUTHORITATIVE_DECLARED_SCOPE",
            scope_key="declared",
            pagination_stability="UNKNOWN",
            listing_identity_sufficient=True,
        )


def test_a21_accepts_reviewed_valid_contracts_and_plan_row_roundtrip(tmp_path):
    if not HAS_CONTRACT:
        pytest.fail("EnumerationContract value object missing")
    good = EnumerationContract(  # type: ignore
        version=1,
        coverage_authority="AUTHORITATIVE_FULL_SOURCE",
        scope_key="full-source",
        pagination_stability="STABLE_SNAPSHOT",
        listing_identity_sufficient=True,
    )
    assert good.version == 1
    # from_plan_row must accept the exact v22 column layout (0/1 -> bool)
    row = {
        "enumeration_contract_version": 1,
        "coverage_authority": "NO_ABSENCE_INFERENCE",
        "coverage_scope_key": "full-source",
        "pagination_stability": "UNKNOWN",
        "listing_identity_sufficient": 0,
    }
    back = EnumerationContract.from_plan_row(row)  # type: ignore
    assert back.coverage_authority == "NO_ABSENCE_INFERENCE"
    assert back.scope_key == "full-source"
    assert back.pagination_stability == "UNKNOWN"
    assert back.listing_identity_sufficient is False


# ------------------------------------------------------------- A2.2 feed adapter


def test_a22_feed_manifest_does_not_claim_incremental():
    from jobscraper.adapters.feed_api import MANIFEST

    assert "incremental" not in tuple(MANIFEST.capabilities)


def test_a22_feed_config_exposes_stable_flag():
    from jobscraper.adapters.feed_api import FeedApiConfig

    names = {f.name for f in dataclasses.fields(FeedApiConfig)}
    if "stable_full_source_enumeration" not in names:
        pytest.fail("stable_full_source_enumeration declaration missing")


def test_a22_feed_config_rejects_non_boolean_stable():
    from jobscraper.adapters.feed_api import FeedApiConfig

    names = {f.name for f in dataclasses.fields(FeedApiConfig)}
    if "stable_full_source_enumeration" not in names:
        pytest.fail("stable_full_source_enumeration declaration missing")
    with pytest.raises(ValueError):
        FeedApiConfig(
            url_template="http://127.0.0.1:9/f?page={page}",
            items_path="jobs",
            fields=_FEED_FIELDS,
            stable_full_source_enumeration="yes",  # type: ignore
        )


def test_json_feed_without_stability_declaration_has_no_absence_authority():
    if not HAS_RESOLVER:
        pytest.fail("registry enumeration resolver missing")
    contract = resolve_enumeration_contract("json_api_feed", dict(_base_config(9)))  # type: ignore
    assert contract.coverage_authority == "NO_ABSENCE_INFERENCE"
    assert contract.pagination_stability == "UNKNOWN"
    assert contract.scope_key == "full-source"
    assert contract.listing_identity_sufficient is True


def test_a22_stable_feed_resolves_authoritative_snapshot():
    if not HAS_RESOLVER:
        pytest.fail("registry enumeration resolver missing")
    cfg = dict(_base_config(9))
    cfg["stable_full_source_enumeration"] = True
    contract = resolve_enumeration_contract("json_api_feed", cfg)  # type: ignore
    assert contract.coverage_authority == "AUTHORITATIVE_FULL_SOURCE"
    assert contract.pagination_stability == "STABLE_SNAPSHOT"
    assert contract.listing_identity_sufficient is True


def test_a22_provider_contracts_preserve_reviewed_authority():
    if not HAS_RESOLVER:
        pytest.fail("registry enumeration resolver missing")
    gh = resolve_enumeration_contract("greenhouse", {})  # type: ignore
    assert gh.coverage_authority == "AUTHORITATIVE_FULL_SOURCE"
    assert gh.pagination_stability == "SINGLE_RESPONSE"
    assert gh.listing_identity_sufficient is True
    ab = resolve_enumeration_contract("ashby", {})  # type: ignore
    assert ab.coverage_authority == "AUTHORITATIVE_FULL_SOURCE"
    assert ab.pagination_stability == "SINGLE_RESPONSE"
    lv = resolve_enumeration_contract("lever", {})  # type: ignore
    assert lv.coverage_authority == "AUTHORITATIVE_FULL_SOURCE"
    assert lv.pagination_stability == "STABLE_SNAPSHOT"
    gd = resolve_enumeration_contract("generic_discovery", {})  # type: ignore
    assert gd.coverage_authority == "NO_ABSENCE_INFERENCE"


def test_a23_unknown_adapter_resolves_conservative():
    if not HAS_RESOLVER:
        pytest.fail("registry enumeration resolver missing")
    contract = resolve_enumeration_contract("does-not-exist", {})  # type: ignore
    assert contract.coverage_authority == "NO_ABSENCE_INFERENCE"
    assert contract.pagination_stability == "UNKNOWN"
    assert contract.listing_identity_sufficient is False


# ------------------------------------------------------------- A2.4 run pinning


def test_a24_new_generic_plan_pins_conservative_contract(tmp_path):
    database = _init_db(tmp_path / "a24.db", feed_config=_base_config(9))
    try:
        _, plans = create_run(database.conn, profile_id=None, plans=[_plan()], now=NOW)
        row = database.conn.execute(
            "SELECT enumeration_contract_version, coverage_authority,"
            " coverage_scope_key, pagination_stability,"
            " listing_identity_sufficient FROM run_source_plans WHERE id = ?",
            (plans[0],),
        ).fetchone()
        assert int(row["enumeration_contract_version"]) == 1
        assert row["coverage_authority"] == "NO_ABSENCE_INFERENCE"
        assert row["coverage_scope_key"] == "full-source"
        assert row["pagination_stability"] == "UNKNOWN"
        assert int(row["listing_identity_sufficient"]) == 1
    finally:
        database.close()


def test_a24_stable_feed_plan_pins_authoritative_contract(tmp_path):
    cfg = _base_config(9)
    cfg["stable_full_source_enumeration"] = True
    database = _init_db(tmp_path / "a24s.db", feed_config=cfg)
    try:
        _, plans = create_run(database.conn, profile_id=None, plans=[_plan()], now=NOW)
        row = database.conn.execute(
            "SELECT coverage_authority, pagination_stability,"
            " listing_identity_sufficient FROM run_source_plans WHERE id = ?",
            (plans[0],),
        ).fetchone()
        assert row["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
        assert row["pagination_stability"] == "STABLE_SNAPSHOT"
        assert int(row["listing_identity_sufficient"]) == 1
    finally:
        database.close()


def test_a24_plan_identity_drift_from_binding_revision_is_refused(tmp_path):
    database = _init_db(tmp_path / "a24d.db", feed_config=_base_config(9))
    try:
        with pytest.raises((ValueError, RuntimeError)):
            create_run(
                database.conn,
                profile_id=None,
                plans=[_plan(binding_id="bnd-OTHER")],
                now=NOW,
            )
    finally:
        database.close()


# ------------------------------------------------------------- A2.6 recovery


def test_a26_recovery_rejects_non_sentinel_contract_drift(tmp_path):
    from jobscraper.runtime.recovery import _validate_resumable_plan_contract

    cfg = _base_config(9)
    cfg["stable_full_source_enumeration"] = True
    database = _init_db(tmp_path / "a26.db", feed_config=cfg)
    try:
        _, plans = create_run(database.conn, profile_id=None, plans=[_plan()], now=NOW)
        plan_id = plans[0]
        # Drift the historical binding config away from the pinned contract:
        # stable declaration removed, so live resolution is now conservative.
        database.conn.execute(
            "UPDATE source_adapter_binding_revisions SET config_json = ? WHERE id = 'bndrev-1'",
            (json.dumps(_base_config(9)),),
        )
        database.conn.commit()
        with pytest.raises(RuntimeError):
            _validate_resumable_plan_contract(database.conn, plan_id)
    finally:
        database.close()


def test_a26_v22_sentinel_resumes_non_authoritatively(tmp_path):
    from jobscraper.runtime.recovery import _validate_resumable_plan_contract

    database = _init_db(tmp_path / "a26s.db", feed_config=_base_config(9))
    try:
        _, plans = create_run(database.conn, profile_id=None, plans=[_plan()], now=NOW)
        plan_id = plans[0]
        # Simulate the exact migrated A1/v22 sentinel: pre-v22 plans migrate
        # to NO_ABSENCE/UNKNOWN with listing=0. Force the sentinel explicitly
        # so this test proves the migration exception even after A2 pinning
        # resolves new plans to listing=1.
        database.conn.execute(
            "UPDATE run_source_plans SET enumeration_contract_version=1,"
            " coverage_authority='NO_ABSENCE_INFERENCE',"
            " coverage_scope_key='full-source', pagination_stability='UNKNOWN',"
            " listing_identity_sufficient=0 WHERE id = ?",
            (plan_id,),
        )
        database.conn.commit()
        # Installed adapter now resolves stronger, but the sentinel must still
        # resume non-authoritatively rather than fail or gain authority.
        cfg = _base_config(9)
        cfg["stable_full_source_enumeration"] = True
        database.conn.execute(
            "UPDATE source_adapter_binding_revisions SET config_json = ? WHERE id = 'bndrev-1'",
            (json.dumps(cfg),),
        )
        database.conn.commit()
        listing = _validate_resumable_plan_contract(database.conn, plan_id)
        assert listing is False
    finally:
        database.close()


# ------------------------------------------------- P0.3a A0-locked names
# These exact pytest names are frozen by the A0 contract lock. They assert
# the already-implemented A2 behavior; the `test_a2*` tests above remain as
# extra coverage. No production change accompanies this naming conformance.


def test_greenhouse_enumeration_contract_remains_full_source():
    if not HAS_RESOLVER:
        pytest.fail("registry enumeration resolver missing")
    contract = resolve_enumeration_contract("greenhouse", {})  # type: ignore
    assert contract.version == 1
    assert contract.coverage_authority == "AUTHORITATIVE_FULL_SOURCE"
    assert contract.scope_key == "full-source"
    assert contract.pagination_stability == "SINGLE_RESPONSE"
    assert contract.listing_identity_sufficient is True


def test_ashby_enumeration_contract_remains_full_source():
    if not HAS_RESOLVER:
        pytest.fail("registry enumeration resolver missing")
    contract = resolve_enumeration_contract("ashby", {})  # type: ignore
    assert contract.version == 1
    assert contract.coverage_authority == "AUTHORITATIVE_FULL_SOURCE"
    assert contract.scope_key == "full-source"
    assert contract.pagination_stability == "SINGLE_RESPONSE"
    assert contract.listing_identity_sufficient is True


def test_lever_enumeration_contract_remains_full_source():
    if not HAS_RESOLVER:
        pytest.fail("registry enumeration resolver missing")
    contract = resolve_enumeration_contract("lever", {})  # type: ignore
    assert contract.version == 1
    assert contract.coverage_authority == "AUTHORITATIVE_FULL_SOURCE"
    assert contract.scope_key == "full-source"
    assert contract.pagination_stability == "STABLE_SNAPSHOT"
    assert contract.listing_identity_sufficient is True


def test_run_source_plan_pins_enumeration_contract(tmp_path):
    database = _init_db(tmp_path / "a2pin.db", feed_config=_base_config(9))
    try:
        _, plans = create_run(database.conn, profile_id=None, plans=[_plan()], now=NOW)
        row = database.conn.execute(
            "SELECT enumeration_contract_version, coverage_authority,"
            " coverage_scope_key, pagination_stability,"
            " listing_identity_sufficient FROM run_source_plans WHERE id = ?",
            (plans[0],),
        ).fetchone()
        assert (
            int(row["enumeration_contract_version"]),
            row["coverage_authority"],
            row["coverage_scope_key"],
            row["pagination_stability"],
            int(row["listing_identity_sufficient"]),
        ) == (1, "NO_ABSENCE_INFERENCE", "full-source", "UNKNOWN", 1)
        if not HAS_RESOLVER:
            pytest.fail("registry enumeration resolver missing")
        resolved = resolve_enumeration_contract(  # type: ignore
            "json_api_feed", dict(_base_config(9))
        )
        assert (
            resolved.version,
            resolved.coverage_authority,
            resolved.scope_key,
            resolved.pagination_stability,
            resolved.listing_identity_sufficient,
        ) == (1, "NO_ABSENCE_INFERENCE", "full-source", "UNKNOWN", True)
    finally:
        database.close()
