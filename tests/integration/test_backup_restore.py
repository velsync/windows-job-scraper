"""Integration tests for backup generations and staged restore (S0.3)."""

import json

import pytest

from jobscraper.db.backup import (
    create_backup_generation,
    list_backup_generations,
    verify_backup_generation,
)
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.db.restore import RestoreError, activate_staged_restore, stage_restore
from jobscraper.paths import build_app_paths


@pytest.fixture()
def seeded_db(data_root):
    paths = build_app_paths(data_root)
    db = Database(paths.database_file)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    # Insert a committed row *while the WAL is open* to prove consistency.
    db.conn.execute(
        "INSERT INTO app_meta(key, value, updated_at) VALUES ('seed', 'wal-row', '2026-01-01T00:00:00.000000Z')"
    )
    # An app-owned artifact + external document reference.
    (paths.fixtures / "demo.json").write_text("{}", encoding="utf-8")
    db.conn.execute(
        "INSERT INTO documents(id, kind, label, path, external, created_at) "
        "VALUES ('d1','RESUME','resume','C:/Users/me/resume.pdf',1,'2026-01-01T00:00:00.000000Z')"
    )
    yield db, paths
    db.close()


def test_backup_of_open_wal_db_contains_committed_row(seeded_db):
    db, paths = seeded_db
    gen = create_backup_generation(paths, db.conn)
    manifest = verify_backup_generation(gen)
    assert any(a.kind == "DATABASE" and a.required for a in manifest.artifacts)
    assert "C:/Users/me/resume.pdf" in manifest.external_references
    # The backup DB contains the committed WAL row.
    import sqlite3

    check = sqlite3.connect(str(gen / "jobscraper.sqlite3"))
    assert check.execute("SELECT value FROM app_meta WHERE key='seed'").fetchone()[0] == "wal-row"
    check.close()


def test_manifest_hash_mismatch_rejected(seeded_db):
    db, paths = seeded_db
    gen = create_backup_generation(paths, db.conn)
    # Corrupt the DB artifact.
    (gen / "jobscraper.sqlite3").write_bytes(b"corrupted")
    with pytest.raises(ValueError):
        verify_backup_generation(gen)


def test_missing_required_artifact_rejected(seeded_db):
    db, paths = seeded_db
    gen = create_backup_generation(paths, db.conn)
    (gen / "jobscraper.sqlite3").unlink()
    with pytest.raises(ValueError):
        verify_backup_generation(gen)


def test_runtime_markers_not_restorable(data_root, seeded_db):
    db, paths = seeded_db
    # Simulate ephemeral runtime markers; they must not appear in backup.
    (paths.runtime / "service.lock").write_text("lock", encoding="utf-8")
    gen = create_backup_generation(paths, db.conn)
    manifest = verify_backup_generation(gen)
    assert all(not a.relative_path.startswith("runtime/") for a in manifest.artifacts)


def test_staged_restore_does_not_touch_live_target(seeded_db):
    db, paths = seeded_db
    gen = create_backup_generation(paths, db.conn)
    staged = stage_restore(gen, paths.root)
    assert (staged / ".restore_manifest_ok").is_file()
    # Live target untouched.
    assert paths.database_file.is_file()
    assert not (paths.root / ".restore_manifest_ok").exists()


def test_full_restore_into_clean_isolated_root(seeded_db, tmp_path):
    db, paths = seeded_db
    gen = create_backup_generation(paths, db.conn)
    target = tmp_path / "isolated-target"
    staged = stage_restore(gen, target)
    activate_staged_restore(staged, target)

    from jobscraper.db.connection import connect_db
    from jobscraper.db.migrations import current_schema_version, run_database_checks

    conn = connect_db(build_app_paths(target).database_file)
    assert current_schema_version(conn) == LATEST_SCHEMA_VERSION
    checks = run_database_checks(conn)
    assert checks["ok"], checks["problems"]
    assert conn.execute("SELECT value FROM app_meta WHERE key='seed'").fetchone()[0] == "wal-row"
    conn.close()


def test_activate_without_verification_rejected(seeded_db, tmp_path):
    db, paths = seeded_db
    gen = create_backup_generation(paths, db.conn)
    fake_staged = tmp_path / "fake-staging"
    fake_staged.mkdir()
    with pytest.raises(RestoreError):
        activate_staged_restore(fake_staged, tmp_path / "target")


def test_list_generations(seeded_db):
    db, paths = seeded_db
    create_backup_generation(paths, db.conn)
    create_backup_generation(paths, db.conn)
    gens = list_backup_generations(paths)
    assert len(gens) == 2
