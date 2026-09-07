"""UTC RFC3339 timestamps and injectable clocks.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 50
(storage timestamp rule); 05 WIN-03A.

Durable timestamps are UTC RFC 3339 strings; lease/scheduling comparisons use
this one consistent time source. In-process elapsed-time decisions use
injectable monotonic/wall clocks so tests never sleep.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

UTC = timezone.utc


def utc_now() -> datetime:
    """Current UTC time (tz-aware)."""
    return datetime.now(tz=UTC)


def to_rfc3339(dt: datetime) -> str:
    """Render a datetime as an RFC 3339 UTC string with microseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def utc_now_s() -> str:
    return to_rfc3339(utc_now())


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 timestamp produced by :func:`to_rfc3339`."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid timestamp: {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def add_seconds(value: str, seconds: float) -> str:
    return to_rfc3339(parse_rfc3339(value) + timedelta(seconds=seconds))


def compare_timestamps(a: str, b: str) -> int:
    """Compare two RFC3339 timestamps; raises on parse failure (fail closed)."""
    da, db = parse_rfc3339(a), parse_rfc3339(b)
    return (da > db) - (da < db)


class Clock:
    """Injectable clock for deterministic tests."""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        self._now_fn = now_fn or utc_now

    def now(self) -> datetime:
        return self._now_fn()

    def now_s(self) -> str:
        return to_rfc3339(self.now())


class FakeClock(Clock):
    """Deterministic mutable clock for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._current = start or utc_now()
        super().__init__(self._tick)

    def _tick(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        self._current = self._current + timedelta(seconds=seconds)


__all__ = [
    "UTC",
    "Clock",
    "FakeClock",
    "add_seconds",
    "compare_timestamps",
    "parse_rfc3339",
    "to_rfc3339",
    "utc_now",
    "utc_now_s",
]
