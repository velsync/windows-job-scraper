"""Stopped/isolated restore of a verified backup generation.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md section 57;
RUN-22.

Restore occurs only while the target service is stopped/isolated. It:
  * verifies the backup generation (hashes, integrity, foreign keys);
  * restores into a clean staged data root;
  * excludes ephemeral runtime locks/process markers;
  * validates before activation;
  * atomically activates the staged generation.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from jobscraper.db.backup import BackupManifest, verify_backup_generation
from jobscraper.paths import build_app_paths

# Directories that must never be restored (ephemeral runtime state).
EXCLUDED_RESTORE_DIRS = {"runtime", "logs"}


class RestoreError(Exception):
    pass


def stage_restore(backup_dir: Path, target_root: Path) -> Path:
    """Stage a verified backup into ``target_root``-adjacent staging.

    The live target root is not modified until :func:`activate_staged_restore`
    succeeds. Returns the staged root path.
    """
    backup_dir = Path(backup_dir)
    manifest = verify_backup_generation(backup_dir)
    target_root = Path(target_root)
    staged = target_root.parent / (target_root.name + ".restore-staging")
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    staged_paths = build_app_paths(staged)

    for artifact in manifest.artifacts:
        src = backup_dir / artifact.relative_path
        if not src.is_file():
            if artifact.required:
                raise RestoreError(f"missing required artifact {artifact.relative_path}")
            continue
        rel = Path(artifact.relative_path)
        # Reject path traversal.
        if ".." in rel.parts:
            raise RestoreError(f"invalid artifact path {artifact.relative_path}")
        if artifact.kind == "DATABASE":
            dest = staged_paths.db / "jobscraper.sqlite3"
        else:
            dest = staged / rel
        if any(part in EXCLUDED_RESTORE_DIRS for part in rel.parts):
            raise RestoreError(f"backup unexpectedly contains ephemeral artifact {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    # Create required directories that the backup may not contain.
    for d in (*staged_paths.durable_dirs, *staged_paths.runtime_dirs):
        d.mkdir(parents=True, exist_ok=True)

    # Post-staging verification: DB must open and pass checks.
    db_path = staged_paths.database_file
    if not db_path.is_file():
        raise RestoreError("staged database missing")
    check = sqlite3.connect(str(db_path))
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RestoreError("staged database failed integrity_check")
        if check.execute("PRAGMA foreign_key_check").fetchall():
            raise RestoreError("staged database failed foreign_key_check")
    finally:
        check.close()

    (staged / ".restore_manifest_ok").write_text(manifest.to_json(), encoding="utf-8")
    return staged


def activate_staged_restore(staged_root: Path, target_root: Path) -> None:
    """Atomically activate a staged restore over the target data root.

    The target service MUST be stopped/isolated by the caller.
    """
    staged_root = Path(staged_root)
    target_root = Path(target_root)
    marker = staged_root / ".restore_manifest_ok"
    if not marker.is_file():
        raise RestoreError("staged restore has not been verified; run stage_restore first")

    # Remove any ephemeral directories from staging before activation.
    for name in EXCLUDED_RESTORE_DIRS:
        d = staged_root / name
        if d.exists():
            shutil.rmtree(d)
            (staged_root / name).mkdir(parents=True, exist_ok=True)

    previous = target_root.parent / (target_root.name + ".restore-previous")
    if previous.exists():
        shutil.rmtree(previous)
    if target_root.exists():
        target_root.rename(previous)
    staged_root.rename(target_root)
    if previous.exists():
        shutil.rmtree(previous)
