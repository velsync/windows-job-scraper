"""SQLite-consistent backup generations with application manifest.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md section 57
(backups); RUN-22 (application backup/restore referential contract).

A backup generation captures:
  * the primary database via the SQLite Backup API (WAL-consistent);
  * required app-owned durable artifacts (fixtures, retained snapshots,
    recipes embedded in DB) with hashes;
  * DPAPI-protected auth material preserved as protected bytes (never
    decrypted into the backup);
  * external user documents recorded as external references.

Ephemeral runtime locks/markers are excluded. A backup is successful only
when its DB and required manifest artifacts are mutually consistent.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jobscraper.paths import AppPaths
from jobscraper.timeutil import utc_now_s

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class BackupArtifact:
    relative_path: str
    sha256: str
    required: bool
    kind: str


@dataclass(frozen=True)
class BackupManifest:
    manifest_version: int
    app_version: str
    schema_version: int
    created_at_utc: str
    artifacts: tuple[BackupArtifact, ...]
    external_references: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "manifest_version": self.manifest_version,
                "app_version": self.app_version,
                "schema_version": self.schema_version,
                "created_at_utc": self.created_at_utc,
                "artifacts": [a.__dict__ for a in self.artifacts],
                "external_references": list(self.external_references),
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "BackupManifest":
        data = json.loads(text)
        return cls(
            manifest_version=int(data["manifest_version"]),
            app_version=data["app_version"],
            schema_version=int(data["schema_version"]),
            created_at_utc=data["created_at_utc"],
            artifacts=tuple(BackupArtifact(**a) for a in data["artifacts"]),
            external_references=tuple(data["external_references"]),
        )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_backup(source: sqlite3.Connection, dest_path: Path) -> None:
    """WAL-consistent copy using the SQLite Backup API."""
    dest = sqlite3.connect(str(dest_path))
    try:
        source.backup(dest)
        dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        dest.close()


def create_backup_generation(
    paths: AppPaths,
    conn: sqlite3.Connection,
    *,
    kind: str = "USER",
    record_backup: Callable[[str, str, str], None] | None = None,
) -> Path:
    """Create a backup generation directory under ``paths.backups``."""
    from jobscraper.db.migrations import current_schema_version
    from jobscraper.version import APP_VERSION

    generation_id = utc_now_s().replace(":", "").replace("-", "").replace(".", "-") + "-" + secrets.token_hex(4)
    gen_dir = paths.backups / generation_id
    gen_dir.mkdir(parents=True, exist_ok=False)

    db_dest = gen_dir / "jobscraper.sqlite3"
    _sqlite_backup(conn, db_dest)

    artifacts: list[BackupArtifact] = [
        BackupArtifact(
            relative_path="jobscraper.sqlite3",
            sha256=_sha256_file(db_dest),
            required=True,
            kind="DATABASE",
        )
    ]

    # Required app-owned durable artifacts: fixtures corpus and retained
    # snapshots referenced by the database. Retention/pruning coordinates with
    # this capture by writing artifacts into the generation before manifest.
    for sub, kind_name, required in (
        ("fixtures", "FIXTURES", True),
        ("snapshots", "SNAPSHOTS", False),
    ):
        src_dir = getattr(paths, sub)
        if not src_dir.exists():
            continue
        for file in sorted(src_dir.rglob("*")):
            if file.is_file():
                rel = file.relative_to(paths.root).as_posix()
                dest = gen_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(file.read_bytes())
                artifacts.append(
                    BackupArtifact(
                        relative_path=rel, sha256=_sha256_file(dest), required=required, kind=kind_name
                    )
                )

    # Auth material: protected bytes only (never decrypted into the backup).
    if paths.auth.exists():
        for file in sorted(paths.auth.rglob("*")):
            if file.is_file():
                rel = file.relative_to(paths.root).as_posix()
                dest = gen_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(file.read_bytes())
                artifacts.append(
                    BackupArtifact(
                        relative_path=rel, sha256=_sha256_file(dest), required=True, kind="AUTH_PROTECTED"
                    )
                )

    # External references: user documents outside the app-owned data root.
    external: list[str] = []
    try:
        rows = conn.execute("SELECT path FROM documents WHERE external = 1").fetchall()
        external = [row[0] for row in rows]
    except sqlite3.OperationalError:
        external = []

    manifest = BackupManifest(
        manifest_version=MANIFEST_VERSION,
        app_version=APP_VERSION,
        schema_version=current_schema_version(conn),
        created_at_utc=utc_now_s(),
        artifacts=tuple(artifacts),
        external_references=tuple(external),
    )
    (gen_dir / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    if record_backup is not None:
        record_backup(generation_id, str(gen_dir), _sha256_text(manifest.to_json()))
    return gen_dir


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_backup_generation(backup_dir: Path) -> BackupManifest:
    """Verify a backup generation; raises on any inconsistency."""
    backup_dir = Path(backup_dir)
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"backup manifest missing: {manifest_path}")
    manifest = BackupManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    if manifest.manifest_version != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest version {manifest.manifest_version}")
    problems: list[str] = []
    for artifact in manifest.artifacts:
        path = backup_dir / artifact.relative_path
        if not path.is_file():
            if artifact.required:
                problems.append(f"missing required artifact: {artifact.relative_path}")
            continue
        if _sha256_file(path) != artifact.sha256:
            problems.append(f"hash mismatch: {artifact.relative_path}")
    # Verify the DB opens and passes integrity checks.
    db_path = backup_dir / "jobscraper.sqlite3"
    if db_path.is_file():
        try:
            check = sqlite3.connect(str(db_path))
            try:
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    problems.append(f"database integrity_check: {integrity}")
                fk = check.execute("PRAGMA foreign_key_check").fetchall()
                if fk:
                    problems.append(f"database foreign_key_check violations: {len(fk)}")
            finally:
                check.close()
        except sqlite3.DatabaseError as exc:
            problems.append(f"database unreadable: {exc}")
    else:
        problems.append("database artifact missing")
    if problems:
        raise ValueError("backup verification failed: " + "; ".join(problems))
    return manifest


def list_backup_generations(paths: AppPaths) -> list[Path]:
    if not paths.backups.exists():
        return []
    return sorted(
        (d for d in paths.backups.iterdir() if (d / "manifest.json").is_file()),
        key=lambda d: d.name,
    )


def latest_verified_backup(paths: AppPaths) -> tuple[Path, BackupManifest] | None:
    for gen in reversed(list_backup_generations(paths)):
        try:
            return gen, verify_backup_generation(gen)
        except Exception:
            continue
    return None
