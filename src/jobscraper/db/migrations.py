"""Forward-only schema migrations with the mandatory backup gate.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md section 57
(migration); module 03 section 50.

Migration gate sequence:

    verify DB -> create backup -> run ordered migration -> integrity check +
    foreign_key_check + application consistency checks -> verify PRAGMAs ->
    record schema version.

Correction versus the donor branch: the production open path can no longer
silently bypass backup-before-migration. Migrating an *existing* database to a
newer schema version requires a backup callable; there is no default that
skips it. A brand-new (schema version 0) database has nothing to back up and
is created directly.

FTS5 capability is reported separately from hard integrity problems so health
surfaces (Doctor) can treat it as its own check instead of a corrupted
``problems`` list entry that ``ok`` did not reflect.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jobscraper.db.connection import (
    Database,
    fts5_available,
    immediate_transaction,
    verify_sqlite_settings,
)
from jobscraper.db.schema_sql import (
    LATEST_SCHEMA_VERSION,
    MIGRATION_STEPS,
    REBUILD_STEPS,
)
from jobscraper.timeutil import utc_now_s


class MigrationError(Exception):
    """Raised when a migration cannot be applied safely."""


def current_schema_version(conn: sqlite3.Connection) -> int:
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not has_table:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def _migration_sql_map() -> dict[int, tuple[str, str]]:
    return {version: (name, sql) for version, name, sql in MIGRATION_STEPS}


def migrate_schema(conn: sqlite3.Connection, target_version: int) -> list[int]:
    """Apply forward-only migrations up to ``target_version``.

    Returns the list of applied versions. Refuses downgrades. Each step runs
    inside one explicit ``BEGIN IMMEDIATE ... COMMIT`` block carried by the
    script itself (sqlite3 ``executescript`` performs no implicit transaction
    control in autocommit connections), so a crash mid-step leaves either the
    fully applied step (tables + version row) or none of it.
    """
    applied: list[int] = []
    steps = _migration_sql_map()
    if target_version > LATEST_SCHEMA_VERSION:
        raise MigrationError(f"unknown target version {target_version}")
    current = current_schema_version(conn)
    if target_version < current:
        raise MigrationError(
            f"downgrade refused: current={current} target={target_version} "
            "(forward-only migrations)"
        )
    for version in range(current + 1, target_version + 1):
        if version not in steps:
            raise MigrationError(f"missing migration step {version}")
        name, sql = steps[version]
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        bookkeeping = (
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);\n"
            if has_table is None
            else ""
        )
        insert_version_row = (
            f"\nINSERT INTO schema_migrations(version, name, applied_at) "
            f"VALUES ({int(version)}, '{name.replace(chr(39), chr(39)*2)}', '{utc_now_s()}');\n"
        )
        try:
            if version in REBUILD_STEPS:
                _executes_rebuild_step(conn, bookkeeping + sql + insert_version_row, version)
            else:
                script = "BEGIN IMMEDIATE;\n" + bookkeeping + sql + insert_version_row + "COMMIT;\n"
                conn.executescript(script)
        except sqlite3.Error:
            # Ensure no half-open transaction survives a failed script.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        applied.append(version)
    return applied


def _executes_rebuild_step(
    conn: sqlite3.Connection, step_sql: str, version: int
) -> None:
    """Apply a table-rebuild step under SQLite's documented 12-step procedure.

    Dropping a table that other tables' foreign keys reference cannot run
    under immediate FK enforcement: the implicit DELETE records a violation
    that re-inserting the rows into the rebuilt table does not clear, so even
    a correct rebuild would fail COMMIT. SQLite's documented procedure is to
    disable FK enforcement for the rebuild and verify with
    ``PRAGMA foreign_key_check`` before committing.

    Fail-closed discipline: the check runs INSIDE the step transaction, so a
    violation aborts the step (nothing commits) and FK enforcement is always
    restored for the connection afterwards.
    """
    fk_was_on = int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    try:
        if fk_was_on:
            # Must happen outside a transaction (no-op otherwise); the
            # connection is in autocommit mode between steps.
            conn.execute("PRAGMA foreign_keys=OFF")
        try:
            # No trailing COMMIT: the foreign-key gate below commits.
            conn.executescript("BEGIN IMMEDIATE;\n" + step_sql)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationError(
                    f"rebuild migration step {version} left"
                    f" foreign key violations: {violations[:5]}"
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        if fk_was_on:
            conn.execute("PRAGMA foreign_keys=ON")


def _application_consistency_checks(conn: sqlite3.Connection) -> list[str]:
    """Application-level consistency checks beyond SQLite's own checks.

    Slice 0 owns only the event log; its consistency invariants are enforced
    by schema constraints. Later slices extend this list with their owning
    checks (they must never be weakened).
    """
    problems: list[str] = []
    checks = [
        # (name, sql, must_be_zero)
        ("events with empty kind", "SELECT COUNT(*) FROM events WHERE kind = ''", True),
        ("events with empty message", "SELECT COUNT(*) FROM events WHERE message = ''", True),
    ]
    for name, sql, must_be_zero in checks:
        try:
            count = int(conn.execute(sql).fetchone()[0])
        except sqlite3.OperationalError:
            continue  # table absent in this schema version
        if must_be_zero and count:
            problems.append(f"{name}: {count}")
    return problems


def run_database_checks(conn: sqlite3.Connection) -> dict[str, object]:
    """Run integrity, foreign-key, FTS-capability and consistency checks.

    ``ok`` reflects hard integrity problems only. FTS5 availability is a
    *capability* result (``fts5``): consumers such as Doctor turn it into the
    appropriate health status; it must never silently masquerade as either an
    integrity failure or a success flag.
    """
    result: dict[str, object] = {"ok": True, "problems": []}
    problems: list[str] = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    result["integrity_check"] = integrity
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")
        result["ok"] = False
    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    result["foreign_key_check"] = len(fk_rows)
    if fk_rows:
        problems.append(f"foreign_key_check violations: {len(fk_rows)}")
        result["ok"] = False
    app_problems = _application_consistency_checks(conn)
    if app_problems:
        problems.extend(app_problems)
        result["ok"] = False
    result["fts5"] = fts5_available(conn)
    result["fts5_available"] = result["fts5"]
    try:
        result["pragmas"] = verify_sqlite_settings(conn).as_dict()
    except sqlite3.DatabaseError as exc:
        problems.append(f"pragma verification failed: {exc}")
        result["ok"] = False
    result["problems"] = problems
    return result


def migrate_database_with_backup(
    db: Database,
    *,
    create_backup,  # required: callable(kind) -> Path (no silent bypass)
    target_version: int = LATEST_SCHEMA_VERSION,
) -> dict[str, object]:
    """Full migration gate with backup-before-mutation.

    ``create_backup`` is a required callable returning the backup directory
    Path (provided by :mod:`jobscraper.db.backup` to avoid a circular import).
    Migrating an existing database without a backup callable is an error.
    """
    if create_backup is None:
        raise MigrationError(
            "refusing to migrate without a backup callable (backup-before-migration)"
        )
    report: dict[str, object] = {
        "from_version": current_schema_version(db.conn),
        "to_version": target_version,
        "backup": None,
        "applied": [],
        "checks": {},
        "ok": False,
    }
    pre_checks = run_database_checks(db.conn)
    report["pre_checks"] = pre_checks
    if not pre_checks["ok"]:
        raise MigrationError(f"pre-migration checks failed: {pre_checks['problems']}")

    if current_schema_version(db.conn) < target_version:
        if report["from_version"] > 0:
            backup_dir = create_backup(kind="PRE_MIGRATION")
            report["backup"] = str(backup_dir)
        else:
            # Fresh database: nothing to back up yet.
            report["backup"] = None
        applied = migrate_schema(db.conn, target_version)
        report["applied"] = applied
    else:
        report["applied"] = []

    post_checks = run_database_checks(db.conn)
    report["checks"] = post_checks
    report["ok"] = bool(post_checks["ok"])
    if not post_checks["ok"]:
        raise MigrationError(f"post-migration checks failed: {post_checks['problems']}")
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO app_meta(key, value, updated_at) VALUES ('schema_version', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (str(target_version), utc_now_s()),
        )
    return report


def open_database_at_latest(
    path: Path,
    *,
    busy_timeout_ms: int = 5000,
    create_backup=None,
) -> Database:
    """Open (creating if needed) a database at the latest schema version.

    The production caller must pass ``create_backup`` (from
    :mod:`jobscraper.db.backup`). Without it, an *existing* database that needs
    migration is refused — backup-before-migration cannot be silently skipped.
    """
    db = Database(path, busy_timeout_ms=busy_timeout_ms)
    try:
        current = current_schema_version(db.conn)
        if current > 0 and current < LATEST_SCHEMA_VERSION and create_backup is None:
            raise MigrationError(
                "existing database requires migration but no backup callable was provided"
            )
        if create_backup is not None:
            migrate_database_with_backup(db, create_backup=create_backup)
        else:
            migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
            checks = run_database_checks(db.conn)
            if not checks["ok"]:
                raise MigrationError(f"checks failed: {checks['problems']}")
    except BaseException:
        db.close()
        raise
    return db
