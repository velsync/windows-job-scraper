"""Canonical SQLite schema (forward-only migrations) — Slice 0 baseline.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 50;
docs/plans/slice-0-worker-implementation-plan-v0313.md S0.2/S0.4.

Slice 0 creates only the foundation tables: schema bookkeeping/application
metadata (v1) and the unified event log (v2). No product/job/crawler tables
are created in Slice 0 (plan section 7); later slices append new steps.

Each step is forward-only; a step may never be edited after being committed to
a release (append a new step instead).
"""

from __future__ import annotations

# (version, name, sql)
MIGRATION_STEPS: list[tuple[int, str, str]] = []

_STEP: dict[int, tuple[str, str]] = {}


def _step(version: int, name: str):
    def deco(fn):
        sql = fn()
        if not isinstance(sql, str) or not sql.strip():
            raise AssertionError(f"migration step {version} produced no SQL")
        _STEP[version] = (name, sql.strip())
        return fn

    return deco


# ------------------------------------------------- v1 schema bookkeeping / meta
@_step(1, "foundation_meta")
def _(sql: str = """
CREATE TABLE app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""") -> None:
    return sql


# --------------------------------------------------------- v2 unified event log
@_step(2, "events")
def _(sql: str = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    source_id TEXT,
    binding_id TEXT,
    request_id TEXT
);
CREATE INDEX idx_events_at ON events(at);
CREATE INDEX idx_events_kind ON events(kind, at);
CREATE INDEX idx_events_run ON events(run_id);
""") -> None:
    return sql


def _finalize() -> None:
    global MIGRATION_STEPS
    MIGRATION_STEPS = sorted((version, *_STEP[version]) for version in _STEP)


_finalize()

LATEST_SCHEMA_VERSION = MIGRATION_STEPS[-1][0] if MIGRATION_STEPS else 0

assert LATEST_SCHEMA_VERSION == 2, "Slice 0 baseline schema is versions 1-2"
