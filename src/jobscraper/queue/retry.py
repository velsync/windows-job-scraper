"""Durable rate/circuit state and Retry-After handling.

Authority: module 03 section 15, RUN-10. Restarting the application must not
erase a meaningful Retry-After or active cooldown.
"""

from __future__ import annotations

import re
import sqlite3

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import add_seconds, utc_now_s


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        from email.utils import parsedate_to_datetime

        from jobscraper.timeutil import utc_now

        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            return None
        delta = (dt - utc_now()).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def binding_scope_key(binding_id: str) -> str:
    return f"binding:{binding_id}"


def host_scope_key(host: str) -> str:
    return f"host:{host.lower()}"


def get_policy_state(db: Database, scope_key: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM host_policy_state WHERE scope_key = ?", (scope_key,))


def record_rate_limit(
    db: Database,
    *,
    scope_key: str,
    retry_after_s: float | None = None,
    cooldown_s: float = 300.0,
    now: str | None = None,
) -> None:
    """Persist rate-limit evidence/cooldown (survives restart)."""
    now = now or utc_now_s()
    cooldown_until = add_seconds(now, retry_after_s if retry_after_s is not None else cooldown_s)
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO host_policy_state(scope_key, circuit_state, cooldown_until,"
            " recent_failure_count, recent_success_count, last_retry_after, last_rate_event_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(scope_key) DO UPDATE SET circuit_state=excluded.circuit_state,"
            " cooldown_until=excluded.cooldown_until, recent_failure_count=recent_failure_count+1,"
            " last_retry_after=excluded.last_retry_after, last_rate_event_at=excluded.last_rate_event_at,"
            " updated_at=excluded.updated_at",
            (
                scope_key,
                "OPEN",
                cooldown_until,
                1,
                0,
                f"{retry_after_s}" if retry_after_s is not None else None,
                now,
                now,
            ),
        )


def record_success(db: Database, *, scope_key: str, now: str | None = None) -> None:
    """Record clean success (circuit recovery)."""
    now = now or utc_now_s()
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO host_policy_state(scope_key, circuit_state, recent_success_count,"
            " last_success_at, updated_at) VALUES (?, 'CLOSED', 1, ?, ?)"
            " ON CONFLICT(scope_key) DO UPDATE SET circuit_state='CLOSED',"
            " recent_success_count=recent_success_count+1, last_success_at=excluded.last_success_at,"
            " updated_at=excluded.updated_at",
            (scope_key, now, now),
        )


def cooldown_active(db: Database, scope_key: str, *, now: str | None = None) -> bool:
    now = now or utc_now_s()
    row = get_policy_state(db, scope_key)
    if row is None:
        return False
    cooldown = row["cooldown_until"]
    return bool(cooldown and cooldown > now)


def enforce_min_interval(
    db: Database, *, scope_key: str, min_interval_ms: int, now: str | None = None
) -> float:
    """Return seconds to wait until min inter-request delay elapses (0 = ok)."""
    now = now or utc_now_s()
    row = get_policy_state(db, scope_key)
    if row is None or not row["last_success_at"]:
        return 0.0
    # Use the last event time as pacing reference.
    last = row["last_rate_event_at"] or row["last_success_at"]
    from jobscraper.timeutil import parse_rfc3339, to_rfc3339, utc_now

    try:
        delta = (parse_rfc3339(now) - parse_rfc3339(last)).total_seconds()
    except Exception:
        return 0.0
    required = min_interval_ms / 1000.0
    if delta >= required:
        return 0.0
    return required - delta
