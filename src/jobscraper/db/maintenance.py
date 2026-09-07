"""Database maintenance: retention pruning, WAL checkpointing, optimize.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 47
(snapshot and retention policy), section 50.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobscraper.paths import AppPaths
from jobscraper.timeutil import add_seconds, parse_rfc3339, utc_now_s


@dataclass
class MaintenanceReport:
    events_pruned: int = 0
    snapshots_pruned: int = 0
    wal_checkpointed: bool = False
    optimized: bool = False


def prune_events(conn: sqlite3.Connection, *, retention_days: int, now: str | None = None) -> int:
    """Delete events older than the retention window (bounded retention)."""
    now = now or utc_now_s()
    cutoff = add_seconds(now, -retention_days * 86400)
    cur = conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
    return cur.rowcount


def prune_snapshots(
    conn: sqlite3.Connection, paths: AppPaths, *, retention_days: int, now: str | None = None
) -> int:
    """Prune low-retention snapshots whose expiry has elapsed.

    High-value jobs (shortlisted/applied) receive longer retention by the
    retention_class assigned at capture time; only STANDARD-class snapshots
    are pruned here. Files referenced by the DB are deleted only after the
    row is removed, in the same maintenance pass.
    """
    now = now or utc_now_s()
    cutoff = add_seconds(now, -retention_days * 86400)
    rows = conn.execute(
        "SELECT id, storage_path FROM snapshots WHERE retention_class = 'STANDARD' "
        "AND created_at < ?",
        (cutoff,),
    ).fetchall()
    pruned = 0
    for row in rows:
        path = Path(row["storage_path"])
        if not path.is_absolute():
            path = paths.snapshots / path
        conn.execute("DELETE FROM snapshots WHERE id = ?", (row["id"],))
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        pruned += 1
    return pruned


def checkpoint_wal(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.Error:  # pragma: no cover
        return False


def optimize(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("PRAGMA optimize")
        return True
    except sqlite3.Error:  # pragma: no cover
        return False


def run_maintenance(
    conn: sqlite3.Connection, paths: AppPaths, *, event_retention_days: int, snapshot_retention_days: int
) -> MaintenanceReport:
    report = MaintenanceReport()
    report.events_pruned = prune_events(conn, retention_days=event_retention_days)
    report.snapshots_pruned = prune_snapshots(
        conn, paths, retention_days=snapshot_retention_days
    )
    report.wal_checkpointed = checkpoint_wal(conn)
    report.optimized = optimize(conn)
    return report


def stale_runtime_markers(paths: AppPaths, *, service_pid_alive=None) -> list[str]:
    """List runtime markers considered stale (dead PID / corrupt descriptor)."""
    markers: list[str] = []
    runtime = paths.runtime
    if not runtime.exists():
        return markers
    descriptor = runtime / "service_descriptor.json"
    if descriptor.is_file():
        import json

        try:
            data = json.loads(descriptor.read_text(encoding="utf-8"))
            pid = int(data.get("pid", -1))
            if service_pid_alive is not None and not service_pid_alive(pid):
                markers.append(f"service_descriptor.json:stale-pid:{pid}")
        except Exception:
            markers.append("service_descriptor.json:corrupt")
    return markers


__all__ = [
    "MaintenanceReport",
    "checkpoint_wal",
    "optimize",
    "prune_events",
    "prune_snapshots",
    "run_maintenance",
    "stale_runtime_markers",
    "parse_rfc3339",
]
