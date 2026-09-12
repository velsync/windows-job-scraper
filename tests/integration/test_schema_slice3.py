"""Slice 3 S3.0 schema gate — append-only v15 runtime foundation.

The promoted Slice-2 schema ends at v14. S3.0 appends the first Slice-3
runtime-foundation migration and must not pre-create crawler/coverage schema
owned by later S3 packages, including S3.9 fallback/group state (R2-F1).
The Batch-A audit corrects the unpromoted v15; later packages append versions.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import (
    LATEST_SCHEMA_VERSION,
    current_schema_version,
    migrate_database_with_backup,
    migrate_schema,
)
from jobscraper.db.schema_sql import MIGRATION_STEPS
from jobscraper.version import SCHEMA_VERSION

PROMOTED_SLICE2_SCHEMA_VERSION = 14
NOW = "2026-09-10T15:00:00.000000Z"

# Exact released bytes through the promoted Slice-2 candidate. S3.0 must not
# edit any of them; future Slice-3 packages append new versions instead.
PROMOTED_STEP_SHA256 = {
    1: "a9d335d680150958aee9d8169034721c24d171696b42c6260052e5c62ba87550",
    2: "22ff8b0feb9bed5e132a5b3d393faee60f5cfcb0df671c00fd1b764dd5e6312e",
    3: "e96853bf636e090f626b8013c23153a5fdbc746e9de02bd93d0b0ad7da5ae1f1",
    4: "2ed4e2c33778dbb320794da4d9e5499a06d103526e804e85e20c9b57a2e0e7f8",
    5: "7e8fbdf2e13b2820017bb27bf55ec9a6fc8e6ed46eaa1315dfb3a6cbf226f77e",
    6: "886c588512ef16dbb5a06c00fc5d11063fabb193315d6f06c3f902eb4d91e9e6",
    7: "2a125e1e573b1d5001f1e742cc628f9432d43fab0fa6da40e670cbb44d1e7333",
    8: "a8401fddd96de6b4e5d1f25724e62fa1148ccc616fec488e3f711502f59f0ceb",
    9: "2085d628491a5cac0044f9195c0c3c43fa5dce4927b43d2db0ba567aae5455b0",
    10: "053df479f2c0f1dcf431b73e38aa000c2fa8878b630552c528787759534f5136",
    11: "dc3a29389a35b1e24396ac9d662cd6c6dfd460341d95125a3171a504f65e8ddb",
    12: "912888da60343657e561504d7458e9e45707029a3c8e67399f2157f1b9faef60",
    13: "bc4c4da5fadfb9a9a1f4d99a3153cc03e1351c10378ab06568b3e222e5d0d24f",
    14: "eeffc8fe0178518b9b8129fe7739579f94084db3fe72c4389fbfab5146c0f18f",
}

# S3.0 freezes the first Slice-3 migration at package completion. This digest
# is over schema_sql.MIGRATION_STEPS' stripped SQL text.
S3_0_STEP_SHA256 = {
    15: "1848e82890da89863de913cee74a039485bf0939ba4536b607e2f94b58a902bc",
}

# S3.5 freezes the binding-revision cursor rebuild. v1-v15 remain immutable.
S3_5_STEP_SHA256 = {
    16: "176f8fac243b988bd5bd1180048ec8ee897184a3ceb741f6a7b6ffd2d397acd0",
}

# S3.7 appends the next unused migration per R2-F1. v16 stays byte-frozen.
S3_7_STEP_SHA256 = {
    17: "485d0f05caf5b9b85231e7d5aa9e55890183ee735d0c015d78b0e4507179ed31",
}


def _digest(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


def _step(version: int) -> tuple[str, str]:
    return next((name, sql) for v, name, sql in MIGRATION_STEPS if v == version)


def test_promoted_v1_through_v14_bytes_are_untouched():
    assert set(PROMOTED_STEP_SHA256) == set(range(1, 15))
    for version, pinned in PROMOTED_STEP_SHA256.items():
        _name, sql = _step(version)
        assert _digest(sql) == pinned, f"promoted migration {version} was edited"


def test_s30_v15_is_sequential_and_pinned():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions == list(range(1, max(versions) + 1))
    assert 15 in versions
    assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION == max(versions)
    name, sql = _step(15)
    assert name == "s3_0_runtime_foundation"
    assert _digest(sql) == S3_0_STEP_SHA256[15]


def test_s35_v16_is_sequential_and_pinned():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:16] == list(range(1, 17))
    name, sql = _step(16)
    assert name == "s3_5_binding_revision_crawl_cursor"
    assert _digest(sql) == S3_5_STEP_SHA256[16]
    # Later packages append only; v15/v16 remain exact accepted bytes.
    assert _digest(_step(15)[1]) == S3_0_STEP_SHA256[15]


def test_s37_v17_is_frozen_after_later_slice3_migrations():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:17] == list(range(1, 18))
    name, sql = _step(17)
    assert name == "s3_7_revalidation_cache"
    assert _digest(sql) == S3_7_STEP_SHA256[17]
    assert _digest(_step(16)[1]) == S3_5_STEP_SHA256[16]


def test_s38_v18_is_frozen_after_s39_append():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:18] == list(range(1, 19))
    name, sql = _step(18)
    assert name == "s3_8_coverage_authority_and_scope_membership"
    assert _digest(sql) == "6075470c2667d581472d8c47ef1baaa8e30fda87d05dbfe996c2c948d4e2c3cf"
    assert _digest(_step(17)[1]) == S3_7_STEP_SHA256[17]


def test_s39_v19_is_frozen_after_s310_append():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:19] == list(range(1, 20))
    name, sql = _step(19)
    assert name == "s3_9_logical_fallback_group_state"
    assert _digest(sql) == "e7028ec8fd11ba751bfc3a3e0145d3f1df31f34bb257c38f6fa00979f671ff68"
    assert _digest(_step(18)[1]) == "6075470c2667d581472d8c47ef1baaa8e30fda87d05dbfe996c2c948d4e2c3cf"


def test_s310_v20_is_frozen_after_s311_append():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions[:20] == list(range(1, 21))
    assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION == max(versions)
    name, sql = _step(20)
    assert name == "s3_10_presence_availability_order"
    assert _digest(sql) == "bd4c690eab18938604f49fd894c4b25c2cdda1a29f62d14d6c678f211faf6c9b"
    assert _digest(_step(19)[1]) == "e7028ec8fd11ba751bfc3a3e0145d3f1df31f34bb257c38f6fa00979f671ff68"
    assert "coverage_presence_application" in sql
    assert "last_absence_coverage_id IS NOT NULL" in sql
    assert "THEN 'LEGACY_ACTIVE'" in sql

    # The db/schema_sql.py migration owner is allowed to backfill canonical
    # projections during an exclusive upgrade, but v20 must touch only the five
    # availability-order columns it adds. This keeps the runtime single-writer
    # contract strong rather than granting migrations an unbounded exception.
    update = sql.split("UPDATE job_sources", 1)[1].split("CREATE INDEX", 1)[0]
    assigned = set(
        re.findall(r"(?m)^\s*(?:SET\s+)?([a-z_][a-z0-9_]*)\s*=", update)
    )
    assert assigned == {
        "availability_effective_at",
        "availability_received_at",
        "availability_evidence_kind",
        "availability_evidence_ref",
        "availability_revision",
    }


def test_s310_v20_adds_only_presence_order_metadata(tmp_path):
    db = Database(tmp_path / "s310-v20.db")
    try:
        migrate_schema(db.conn, 19)
        before = {
            row[1] for row in db.conn.execute("PRAGMA table_info(job_sources)")
        }
        migrate_schema(db.conn, 20)
        after = {
            row[1] for row in db.conn.execute("PRAGMA table_info(job_sources)")
        }
        assert after - before == {
            "availability_effective_at",
            "availability_received_at",
            "availability_evidence_kind",
            "availability_evidence_ref",
            "availability_revision",
        }
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()

def test_v16_rebuild_preserves_legacy_cursor_without_guessing_provenance(tmp_path):
    db = Database(tmp_path / "cursor-v16.db")
    try:
        migrate_schema(db.conn, 15)
        db.conn.execute(
            "INSERT INTO sources(id,display_name,source_family,entry_url,created_at,updated_at)"
            " VALUES ('src','Fixture','CAREERS','https://jobs.example.test',?,?)",
            (NOW, NOW),
        )
        db.conn.execute(
            "INSERT INTO source_adapter_bindings(id,source_id,display_name,created_at)"
            " VALUES ('bnd','src','fixture',?)", (NOW,),
        )
        db.conn.execute(
            "INSERT INTO crawl_cursors(id,source_id,binding_id,adapter_id,adapter_version,"
            " cursor_schema_version,state_json,checkpoint_at)"
            " VALUES ('cur-old','src','bnd','fixture','1.0.0',1,'{\"page\":2}',?)",
            (NOW,),
        )
        db.conn.commit()
        migrate_schema(db.conn, 16)
        row = db.conn.execute("SELECT * FROM crawl_cursors WHERE id='cur-old'").fetchone()
        assert row["binding_revision_id"] is None
        assert row["checkpoint_run_source_plan_id"] is None
        assert row["state_json"] == '{"page":2}'
        assert row["guard_state_json"] == '{}'
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_v16_shares_one_compatible_cursor_row_with_plan_provenance(tmp_path):
    db = Database(tmp_path / "cursor-identity-v16.db")
    try:
        migrate_schema(db.conn, 16)
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(crawl_cursors)")}
        assert {"binding_revision_id", "guard_state_json", "checkpoint_run_source_plan_id"} <= columns
        # Partial unique index exists on the compatible lookup identity:
        # legacy NULL rows may coexist, while S3.5 rows share exactly one
        # cursor per compatible binding revision + code pins.
        indexes = db.conn.execute("PRAGMA index_list(crawl_cursors)").fetchall()
        assert any(row[1] == "idx_crawl_cursors_compatible_identity" and row[2] == 1 for row in indexes)
    finally:
        db.close()


def test_v15_contains_only_s31_to_s34_runtime_foundation_storage():
    _name, sql = _step(15)
    assert "CREATE TABLE service_clock_epochs" in sql
    assert "ALTER TABLE request_attempts ADD COLUMN service_epoch_id" in sql
    assert "CREATE TABLE binding_host_rate_state" in sql
    for required in (
        "circuit_state",
        "cooldown_until",
        "recent_failure_count",
        "recent_success_count",
        "last_retry_after",
        "last_rate_event_at",
        "last_success_at",
        "egress_identity",
    ):
        assert required in sql

    # R2-F1: do not steal crawler/coverage/future-slice schema into v15.
    for forbidden in (
        "source_plan_group_state",
        "active_fallback_rank",
        "group_outcome",
        "CREATE TABLE cache_representation",
        "CREATE TABLE coverage_scope_membership",
        "CREATE TABLE recipes",
        "CREATE TABLE recipe_versions",
        "CREATE TABLE navigation_plans",
        "CREATE TABLE contacts",
        "CREATE TABLE fx_rates",
        "CREATE TABLE user_feedback",
        "ALTER TABLE enumeration_coverage",
        "ALTER TABLE job_sources",
        "ALTER TABLE job_eligibility",
        "ALTER TABLE job_scores",
    ):
        assert forbidden not in sql, forbidden


def test_promoted_v14_migrates_through_backup_gate_with_existing_rows_preserved(tmp_path):
    db = Database(tmp_path / "from-v14.db")
    migrate_schema(db.conn, PROMOTED_SLICE2_SCHEMA_VERSION)
    db.conn.execute(
        "INSERT INTO events(at, level, kind, message) VALUES (?, 'INFO', 'S3_FIXTURE', 'keep me')",
        (NOW,),
    )
    db.conn.commit()
    assert current_schema_version(db.conn) == 14

    calls: list[str] = []

    def create_backup(*, kind: str):
        calls.append(kind)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir(exist_ok=True)
        return backup_dir

    report = migrate_database_with_backup(
        db,
        create_backup=create_backup,
        target_version=15,
    )
    assert calls == ["PRE_MIGRATION"]
    assert report["applied"] == [15]
    assert report["ok"] is True
    assert current_schema_version(db.conn) == 15
    assert db.conn.execute(
        "SELECT message FROM events WHERE kind='S3_FIXTURE'"
    ).fetchone()[0] == "keep me"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_v15_service_epoch_is_durable_and_request_attempts_reference_it(tmp_path):
    db = Database(tmp_path / "epoch.db")
    migrate_schema(db.conn, 15)
    tables = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "service_clock_epochs" in tables
    columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(request_attempts)").fetchall()
    }
    assert "service_epoch_id" in columns
    fks = db.conn.execute("PRAGMA foreign_key_list(request_attempts)").fetchall()
    assert any(
        row[2] == "service_clock_epochs" and row[3] == "service_epoch_id"
        for row in fks
    )
    db.close()


def test_v15_rate_state_identity_is_binding_host_and_optional_egress(tmp_path):
    db = Database(tmp_path / "rate.db")
    migrate_schema(db.conn, 15)
    db.conn.execute(
        "INSERT INTO sources(id, display_name, source_family, entry_url, created_at, updated_at)"
        " VALUES ('src-1','Fixture','CAREERS','https://jobs.example.test',?,?)",
        (NOW, NOW),
    )
    db.conn.execute(
        "INSERT INTO source_adapter_bindings(id, source_id, display_name, created_at)"
        " VALUES ('bnd-1','src-1','fixture',?)",
        (NOW,),
    )
    db.conn.execute(
        "INSERT INTO binding_host_rate_state(id, binding_id, host, updated_at)"
        " VALUES ('rate-1','bnd-1','jobs.example.test',?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO binding_host_rate_state(id, binding_id, host, updated_at)"
            " VALUES ('rate-2','bnd-1','jobs.example.test',?)",
            (NOW,),
        )
    db.conn.execute(
        "INSERT INTO binding_host_rate_state(id, binding_id, host, egress_identity, updated_at)"
        " VALUES ('rate-3','bnd-1','jobs.example.test','egress-approved-1',?)",
        (NOW,),
    )
    row = db.conn.execute(
        "SELECT circuit_state, recent_failure_count, recent_success_count"
        " FROM binding_host_rate_state WHERE id='rate-1'"
    ).fetchone()
    assert tuple(row) == ("CLOSED", 0, 0)
    db.close()


def test_v15_does_not_precreate_s39_group_storage(tmp_path):
    db = Database(tmp_path / "no-group-state.db")
    try:
        migrate_schema(db.conn, 14)
        before = {row[0] for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        migrate_schema(db.conn, 15)
        after = {row[0] for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert after - before == {"service_clock_epochs", "binding_host_rate_state"}
        assert "source_plan_group_state" not in after
    finally:
        db.close()


def test_s311_v21_is_next_unused_sequential_and_pinned():
    versions = [version for version, _name, _sql in MIGRATION_STEPS]
    assert versions == list(range(1, 22))
    assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION == 21
    name, sql = _step(21)
    assert name == "s3_11_evaluation_input_order"
    assert _digest(sql) == "d340b7c34362b2b4b2d5b7fd6aaa229a2460a98bc81dc817a6c8ba26db2b2305"
    assert _digest(_step(20)[1]) == "bd4c690eab18938604f49fd894c4b25c2cdda1a29f62d14d6c678f211faf6c9b"
    assert "ADD COLUMN evaluation_revision" in sql
    assert "ADD COLUMN eligibility_evaluator_version" in sql

    # The db/schema_sql.py migration owner is allowed to backfill the
    # newly-added coordinate during an exclusive upgrade, but v21 must touch
    # only jobs.evaluation_revision. This keeps the runtime single-writer
    # contract strong rather than granting migrations an unbounded exception.
    update = sql.split("UPDATE jobs", 1)[1].split("ALTER TABLE", 1)[0]
    assigned = set(
        re.findall(r"(?m)^\s*(?:SET\s+)?([a-z_][a-z0-9_]*)\s*=", update)
    )
    assert assigned == {"evaluation_revision"}


def test_s311_v21_adds_only_evaluation_order_coordinates(tmp_path):
    db = Database(tmp_path / "s311-v21.db")
    try:
        migrate_schema(db.conn, 20)
        jobs_before = {
            row[1] for row in db.conn.execute("PRAGMA table_info(jobs)")
        }
        scores_before = {
            row[1] for row in db.conn.execute("PRAGMA table_info(job_scores)")
        }
        eligibility_before = {
            row[1] for row in db.conn.execute("PRAGMA table_info(job_eligibility)")
        }
        migrate_schema(db.conn, 21)
        jobs_after = {
            row[1] for row in db.conn.execute("PRAGMA table_info(jobs)")
        }
        scores_after = {
            row[1] for row in db.conn.execute("PRAGMA table_info(job_scores)")
        }
        eligibility_after = {
            row[1] for row in db.conn.execute("PRAGMA table_info(job_eligibility)")
        }
        assert jobs_after - jobs_before == {"evaluation_revision"}
        assert scores_after - scores_before == {"eligibility_evaluator_version"}
        assert eligibility_after == eligibility_before
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
# --- S3.13 final promoted-v14 -> Slice-3 migration acceptance proof ---

def test_s313_promoted_v14_migrates_to_latest_with_integrity_and_effective_pragmas(
    tmp_path,
):
    """Prove promoted v14 data crosses every Slice-3 migration intact.

    The fixture deliberately occupies state touched by later Slice-3 migrations:
    a legacy cursor (v16 rebuild), a coverage/run-plan row (v18 backfill and v19
    logical-group derivation), an ACTIVE presence (v20 availability ordering),
    and a canonical job whose presence revision seeds v21 evaluation ordering.
    """
    from jobscraper.db.connection import verify_sqlite_settings

    db = Database(tmp_path / "s313-from-promoted-v14.db")
    try:
        migrate_schema(db.conn, PROMOTED_SLICE2_SCHEMA_VERSION)
        assert current_schema_version(db.conn) == PROMOTED_SLICE2_SCHEMA_VERSION

        # Real promoted-v14 authority graph.  Keep this fixture deliberately
        # small, but place rows in every pre-existing table that a later
        # Slice-3 migration rebuilds or backfills.
        db.conn.execute(
            "INSERT INTO sources("
            "id,display_name,source_family,entry_url,created_at,updated_at"
            ") VALUES('s313-src','Promoted v14 source','PUBLIC_FEED',"
            "'https://example.test/jobs',?,?)",
            (NOW, NOW),
        )
        db.conn.execute(
            "INSERT INTO adapter_definitions("
            "adapter_id,adapter_version,adapter_api_version,manifest_json,created_at"
            ") VALUES('s313-adapter','1.0.0','1','{}',?)",
            (NOW,),
        )
        db.conn.execute(
            "INSERT INTO adapter_permission_profiles(id,display_name,created_at) "
            "VALUES('s313-perm','Promoted v14 permission',?)",
            (NOW,),
        )
        db.conn.execute(
            "INSERT INTO adapter_permission_profile_revisions("
            "id,permission_profile_id,revision,policy_json,created_at"
            ") VALUES('s313-permrev','s313-perm',1,'{}',?)",
            (NOW,),
        )
        db.conn.execute(
            "INSERT INTO source_adapter_bindings("
            "id,source_id,display_name,created_at"
            ") VALUES('s313-bnd','s313-src','Promoted v14 binding',?)",
            (NOW,),
        )
        db.conn.execute(
            "INSERT INTO source_adapter_binding_revisions("
            "id,binding_id,revision,adapter_id,adapter_version,strategy,"
            "execution_class,permission_profile_id,permission_profile_revision,"
            "config_json,created_at"
            ") VALUES('s313-bndrev','s313-bnd',1,'s313-adapter','1.0.0',"
            "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP','s313-perm',1,'{}',?)",
            (NOW,),
        )
        db.conn.execute(
            "INSERT INTO scrape_runs(id,status,created_at,started_at,finished_at) "
            "VALUES('s313-run','SUCCEEDED',?,?,?)",
            (NOW, NOW, NOW),
        )
        db.conn.execute(
            "INSERT INTO run_source_plans("
            "id,run_id,source_id,source_plan_group_id,fallback_rank,binding_id,"
            "binding_revision_id,adapter_id,adapter_version,adapter_api_version,"
            "strategy,execution_class,cursor_schema_version,"
            "crawl_policy_snapshot_json,rate_policy_snapshot_json,"
            "permission_profile_id,permission_profile_revision,group_outcome,created_at"
            ") VALUES('s313-plan','s313-run','s313-src','s313-group',0,'s313-bnd',"
            "'s313-bndrev','s313-adapter','1.0.0','1',"
            "'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT','HTTP',1,'{}','{}',"
            "'s313-perm',1,'SATISFIED',?)",
            (NOW,),
        )

        # Pre-v18 coverage intentionally omits the denormalized group/revision
        # pointers so the v18 migration must derive them from the immutable plan.
        db.conn.execute(
            "INSERT INTO enumeration_coverage("
            "id,run_source_plan_id,source_plan_group_id,source_id,binding_id,"
            "binding_revision_id,scope_key,generation_key,started_at,finished_at,"
            "completion_state,coverage_authority,pages_completed,items_observed,"
            "cursor_terminal,terminal_enumeration_proven,absence_inference_allowed,"
            "finalized_at,applied_at,created_at"
            ") VALUES('s313-cov','s313-plan',NULL,'s313-src','s313-bnd',NULL,"
            "'FULL_SOURCE','legacy-generation',?,?,"
            "'COMPLETE','AUTHORITATIVE_FULL_SOURCE',1,1,1,1,1,?,?,?)",
            (NOW, NOW, NOW, NOW, NOW),
        )

        # v16 rebuilds this legacy cursor.  Its opaque state must survive while
        # new provenance/guard fields are introduced conservatively.
        db.conn.execute(
            "INSERT INTO crawl_cursors("
            "id,source_id,binding_id,adapter_id,adapter_version,"
            "cursor_schema_version,state_json,checkpoint_at"
            ") VALUES('s313-cursor','s313-src','s313-bnd','s313-adapter',"
            "'1.0.0',1,?,?)",
            ('{"page":4}', NOW),
        )

        db.conn.execute(
            "INSERT INTO companies("
            "id,name,normalized_name,domain,careers_url,ats_provider,ats_board,"
            "first_seen_at,created_at,updated_at"
            ") VALUES('s313-co','Promoted Co','promoted co','example.test',"
            "'https://example.test/jobs','custom','board',?,?,?)",
            (NOW, NOW, NOW),
        )
        db.conn.execute(
            "INSERT INTO jobs("
            "id,company_id,title,normalized_title,description_md,description_text,"
            "discovered_at,first_seen_at,last_seen_at,listing_status,created_at,updated_at"
            ") VALUES('s313-job','s313-co','Promoted Engineer','promoted engineer',"
            "'# Promoted Engineer','Promoted Engineer',?,?,?,'ACTIVE',?,?)",
            (NOW, NOW, NOW, NOW, NOW),
        )
        db.conn.execute(
            "INSERT INTO job_sources("
            "id,job_id,source_id,binding_id,source_job_id,discovery_url,raw_source_url,"
            "first_seen_at,last_seen_at,presence_state,content_revision,"
            "last_observation_id,source_rank,created_at,updated_at"
            ") VALUES('s313-js','s313-job','s313-src','s313-bnd','native-1',"
            "'https://example.test/jobs','https://example.test/jobs/native-1',"
            "?,?,'ACTIVE',7,NULL,0,?,?)",
            (NOW, NOW, NOW, NOW),
        )
        db.conn.execute(
            "INSERT INTO events(at,level,kind,message) "
            "VALUES(?,'INFO','S313_V14_FIXTURE','preserve across Slice 3')",
            (NOW,),
        )
        db.conn.commit()

        # Capture promoted values before migration so preservation is checked on
        # content, not merely on row counts/schema_version.
        cursor_before = tuple(
            db.conn.execute(
                "SELECT source_id,binding_id,adapter_id,adapter_version,"
                "cursor_schema_version,state_json,checkpoint_at "
                "FROM crawl_cursors WHERE id='s313-cursor'"
            ).fetchone()
        )
        presence_before = tuple(
            db.conn.execute(
                "SELECT job_id,source_id,binding_id,source_job_id,presence_state,"
                "content_revision,last_seen_at,created_at,updated_at "
                "FROM job_sources WHERE id='s313-js'"
            ).fetchone()
        )

        backup_kinds: list[str] = []

        def create_backup(*, kind: str):
            backup_kinds.append(kind)
            path = tmp_path / "s313-pre-migration-backup"
            path.mkdir(exist_ok=True)
            return path

        report = migrate_database_with_backup(
            db,
            create_backup=create_backup,
            target_version=LATEST_SCHEMA_VERSION,
        )

        assert backup_kinds == ["PRE_MIGRATION"]
        assert report["ok"] is True
        assert report["pre_checks"]["ok"] is True
        assert report["checks"]["ok"] is True
        assert report["checks"]["integrity_check"] == "ok"
        assert report["checks"]["foreign_key_check"] == 0
        assert report["applied"] == list(
            range(
                PROMOTED_SLICE2_SCHEMA_VERSION + 1,
                LATEST_SCHEMA_VERSION + 1,
            )
        )
        assert current_schema_version(db.conn) == LATEST_SCHEMA_VERSION
        assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION

        # v16: legacy cursor payload survives; new provenance remains unknown
        # rather than being guessed during migration.
        cursor = db.conn.execute(
            "SELECT source_id,binding_id,adapter_id,adapter_version,"
            "cursor_schema_version,state_json,checkpoint_at,binding_revision_id,"
            "guard_state_json,checkpoint_run_source_plan_id "
            "FROM crawl_cursors WHERE id='s313-cursor'"
        ).fetchone()
        assert tuple(cursor[:7]) == cursor_before
        assert cursor["binding_revision_id"] is None
        assert cursor["guard_state_json"] == "{}"
        assert cursor["checkpoint_run_source_plan_id"] is None

        # v18: denormalized coverage identity comes only from its immutable
        # RunSourcePlan and the historical COMPLETE result remains complete.
        coverage = db.conn.execute(
            "SELECT source_plan_group_id,source_id,binding_id,binding_revision_id,"
            "generation_order_key,listing_identity_sufficient,completion_state,"
            "coverage_authority,finalized_at "
            "FROM enumeration_coverage WHERE id='s313-cov'"
        ).fetchone()
        assert tuple(coverage[:4]) == (
            "s313-group",
            "s313-src",
            "s313-bnd",
            "s313-bndrev",
        )
        assert coverage["generation_order_key"] == f"{NOW}|s313-cov"
        assert coverage["listing_identity_sufficient"] == 0
        assert coverage["completion_state"] == "COMPLETE"
        assert coverage["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
        assert coverage["finalized_at"] == NOW

        # v19: pre-existing plan truth is deterministically materialized into
        # the new logical fallback-group owner.
        group = db.conn.execute(
            "SELECT active_fallback_rank,group_outcome "
            "FROM source_plan_group_state "
            "WHERE run_id='s313-run' AND source_plan_group_id='s313-group'"
        ).fetchone()
        assert tuple(group) == (0, "SATISFIED")

        # v20: historical ACTIVE presence remains ACTIVE and receives a
        # comparable legacy evidence coordinate without losing old columns.
        presence = db.conn.execute(
            "SELECT job_id,source_id,binding_id,source_job_id,presence_state,"
            "content_revision,last_seen_at,created_at,updated_at,"
            "availability_effective_at,availability_received_at,"
            "availability_evidence_kind,availability_evidence_ref,"
            "availability_revision FROM job_sources WHERE id='s313-js'"
        ).fetchone()
        assert tuple(presence[:9]) == presence_before
        assert presence["availability_effective_at"] == NOW
        assert presence["availability_received_at"] == NOW
        assert presence["availability_evidence_kind"] == "LEGACY_ACTIVE"
        assert presence["availability_evidence_ref"] is None
        assert presence["availability_revision"] == 1

        # v21: canonical evaluation ordering starts at the maximum historical
        # per-presence content revision, not an arbitrary reset to one.
        job = db.conn.execute(
            "SELECT title,normalized_title,listing_status,evaluation_revision "
            "FROM jobs WHERE id='s313-job'"
        ).fetchone()
        assert tuple(job[:3]) == (
            "Promoted Engineer",
            "promoted engineer",
            "ACTIVE",
        )
        assert job["evaluation_revision"] == 7

        event = db.conn.execute(
            "SELECT message FROM events WHERE kind='S313_V14_FIXTURE'"
        ).fetchone()
        assert event[0] == "preserve across Slice 3"

        # Re-check effective settings on the live migrated connection.  The
        # production migration report's pre/post `ok` flags also include its
        # application-consistency checks.
        settings = verify_sqlite_settings(db.conn)
        assert settings.foreign_keys == 1
        assert settings.journal_mode == "WAL"
        assert settings.synchronous == 2
        assert settings.busy_timeout_ms > 0
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []

        # S3.13 re-proves the immutable promoted migration bytes at the final
        # boundary rather than trusting an earlier package's report.
        for version, pinned in PROMOTED_STEP_SHA256.items():
            assert _digest(_step(version)[1]) == pinned
    finally:
        db.close()

# --- end S3.13 final promoted-v14 -> Slice-3 migration acceptance proof ---
