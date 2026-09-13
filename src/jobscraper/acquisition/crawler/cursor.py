"""RunSourcePlan-scoped crawler cursor persistence."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping

from jobscraper.adapters.contract import CrawlCursor
from jobscraper.ids import new_id
from .pagination import PaginationGuardState


class CursorCompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedCursor:
    cursor: CrawlCursor
    guard_state: PaginationGuardState


def _row_get(row: Mapping[str, object], key: str):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise CursorCompatibilityError(f"plan is missing {key}") from exc


def _identity(plan_row: Mapping[str, object]) -> dict[str, object]:
    """Immutable cursor compatibility pins owned by one RunSourcePlan."""
    return {
        "binding_revision_id": _row_get(plan_row, "binding_revision_id"),
        "adapter_id": _row_get(plan_row, "adapter_id"),
        "adapter_version": _row_get(plan_row, "adapter_version"),
        "cursor_schema_version": int(_row_get(plan_row, "cursor_schema_version")),
    }


def _validate_row_owner(
    row: sqlite3.Row | Mapping[str, object],
    *,
    run_source_plan_id: str,
    plan_row: Mapping[str, object],
) -> None:
    expected = {
        "checkpoint_run_source_plan_id": run_source_plan_id,
        "source_id": _row_get(plan_row, "source_id"),
        "binding_id": _row_get(plan_row, "binding_id"),
        "binding_revision_id": _row_get(plan_row, "binding_revision_id"),
        "adapter_id": _row_get(plan_row, "adapter_id"),
        "adapter_version": _row_get(plan_row, "adapter_version"),
        "cursor_schema_version": int(_row_get(plan_row, "cursor_schema_version")),
    }
    for key, value in expected.items():
        actual = row[key]
        if key == "cursor_schema_version":
            actual = int(actual)
        if actual != value:
            raise CursorCompatibilityError(
                f"cursor {key} does not match immutable RunSourcePlan"
            )


def _loaded(row: sqlite3.Row | Mapping[str, object]) -> LoadedCursor:
    return LoadedCursor(
        CrawlCursor(
            source_id=row["source_id"],
            binding_id=row["binding_id"],
            adapter_id=row["adapter_id"],
            adapter_version=row["adapter_version"],
            cursor_schema_version=int(row["cursor_schema_version"]),
            state_json=row["state_json"],
            checkpoint_at=row["checkpoint_at"],
        ),
        PaginationGuardState.from_json(row["guard_state_json"]),
    )


def load_cursor(
    conn: sqlite3.Connection,
    *,
    run_source_plan_id: str,
    plan_row: Mapping[str, object],
) -> LoadedCursor | None:
    identity = _identity(plan_row)
    row = conn.execute(
        "SELECT * FROM crawl_cursors"
        " WHERE checkpoint_run_source_plan_id = ? AND adapter_id = ?"
        " AND adapter_version = ? AND cursor_schema_version = ?",
        (
            run_source_plan_id,
            identity["adapter_id"],
            identity["adapter_version"],
            identity["cursor_schema_version"],
        ),
    ).fetchone()
    if row is not None:
        _validate_row_owner(
            row,
            run_source_plan_id=run_source_plan_id,
            plan_row=plan_row,
        )
        return _loaded(row)

    legacy = conn.execute(
        """
        SELECT id FROM crawl_cursors
         WHERE binding_revision_id IS NULL
           AND source_id = ? AND binding_id = ?
           AND adapter_id = ? AND adapter_version = ?
           AND cursor_schema_version = ?
         LIMIT 1
        """,
        (
            _row_get(plan_row, "source_id"), _row_get(plan_row, "binding_id"),
            identity["adapter_id"], identity["adapter_version"],
            identity["cursor_schema_version"],
        ),
    ).fetchone()
    if legacy is not None:
        raise CursorCompatibilityError(
            "legacy pre-v16 cursor is preserved but is not bound to a compatible binding revision"
        )
    return None


def save_cursor(
    conn: sqlite3.Connection,
    *,
    run_source_plan_id: str,
    plan_row: Mapping[str, object],
    cursor: CrawlCursor,
    guard_state: PaginationGuardState,
    now: str,
) -> None:
    identity = _identity(plan_row)
    # Code provenance: the stored state blob must come from the pinned
    # adapter build. Source/binding provenance is host-owned and comes from
    # the immutable plan; adapter/version/schema drift is refused.
    if cursor.adapter_id != identity["adapter_id"]:
        raise CursorCompatibilityError(
            "cursor proposal adapter_id does not match immutable RunSourcePlan"
        )
    if cursor.adapter_version != identity["adapter_version"]:
        raise CursorCompatibilityError(
            "cursor proposal adapter_version does not match immutable RunSourcePlan"
        )
    if int(cursor.cursor_schema_version) != identity["cursor_schema_version"]:
        raise CursorCompatibilityError(
            "cursor proposal cursor_schema_version does not match immutable RunSourcePlan"
        )

    existing = conn.execute(
        "SELECT * FROM crawl_cursors"
        " WHERE checkpoint_run_source_plan_id = ? AND adapter_id = ?"
        " AND adapter_version = ? AND cursor_schema_version = ?",
        (
            run_source_plan_id,
            identity["adapter_id"],
            identity["adapter_version"],
            identity["cursor_schema_version"],
        ),
    ).fetchone()
    if existing is not None:
        _validate_row_owner(
            existing,
            run_source_plan_id=run_source_plan_id,
            plan_row=plan_row,
        )
        conn.execute(
            "UPDATE crawl_cursors SET state_json = ?, guard_state_json = ?,"
            " checkpoint_at = ? WHERE id = ?",
            (
                cursor.state_json,
                guard_state.to_json(),
                now,
                existing["id"],
            ),
        )
        return

    conn.execute(
        """
        INSERT INTO crawl_cursors(
            id, source_id, binding_id, binding_revision_id,
            adapter_id, adapter_version, cursor_schema_version, state_json,
            guard_state_json, checkpoint_run_source_plan_id, checkpoint_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("cur"), _row_get(plan_row, "source_id"),
            _row_get(plan_row, "binding_id"),
            identity["binding_revision_id"], identity["adapter_id"],
            identity["adapter_version"],
            identity["cursor_schema_version"], cursor.state_json,
            guard_state.to_json(), run_source_plan_id, now,
        ),
    )


__all__ = ["CursorCompatibilityError", "LoadedCursor", "load_cursor", "save_cursor"]
