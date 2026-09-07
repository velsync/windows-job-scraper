"""Database maintenance: retention pruning, WAL checkpointing, optimize.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 47
(snapshot and retention policy), section 50; module 05 section 46.

Slice 0 scope: event retention, WAL checkpointing and stale runtime-marker
detection. Snapshot retention joins when snapshots exist (later slices).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobscraper.paths import AppPaths
from jobscraper.timeutil import add_seconds, parse_rfc3339, utc_now_s


@dataclass
class MaintenanceReport:
    events_pruned: int = 0
    wal_checkpointed: bool = False
    optimized: bool = False


def prune_events(conn: sqlite3.Connection, *, retention_days: int, now: str | None = None) -> int:
    """Delete events older than the retention window (bounded retention)."""
    now = now or utc_now_s()
    cutoff = add_seconds(now, -retention_days * 86400)
    cur = conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
    return cur.rowcount


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
    conn: sqlite3.Connection, *, event_retention_days: int
) -> MaintenanceReport:
    report = MaintenanceReport()
    report.events_pruned = prune_events(conn, retention_days=event_retention_days)
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
    "run_maintenance",
    "stale_runtime_markers",
    "parse_rfc3339",
]
