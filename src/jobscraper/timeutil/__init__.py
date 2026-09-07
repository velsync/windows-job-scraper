"""Time and timezone utilities.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-03A;
docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 49/50.

Durable timestamps are UTC RFC 3339 strings. Lease comparisons use the same
database-consistent time source. Local schedules use IANA identifiers resolved
through the pinned ``tzdata`` package, with deterministic DST policies:

* nonexistent local time (spring gap)   -> first valid instant after the gap;
* ambiguous local time (autumn fold)    -> first occurrence (fold=0), with the
  resolved offset recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


def utc_now() -> datetime:
    """Current UTC time (naive of local zone, tzinfo=UTC)."""
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


def resolve_named_zone(name: str) -> ZoneInfo:
    """Resolve an IANA zone identifier via the pinned tzdata package."""
    return ZoneInfo(name)


def tzdata_version() -> str:
    """Best-effort version of the bundled tzdata package."""
    try:
        import importlib.metadata as im

        return im.version("tzdata")
    except Exception:  # pragma: no cover - defensive
        return "unknown"


@dataclass(frozen=True)
class LocalOccurrence:
    """A resolved local-time occurrence with DST fold/gap policy evidence."""

    zone: str
    local_naive: str  # ISO local wall clock input
    utc: str  # resolved UTC RFC3339
    policy: str  # "EXACT" | "SPRING_GAP_ADVANCED" | "AUTUMN_FOLD_FIRST"
    resolved_offset_seconds: int
    fold: int = 0


def resolve_local_time(zone_name: str, local_naive: datetime) -> LocalOccurrence:
    """Resolve a naive local datetime in ``zone_name`` under the DST policy.

    Spring gap: the nonexistent local time advances to the first valid local
    instant after the gap. Autumn fold: the first occurrence (fold=0) is used
    and the resolved offset is recorded.
    """
    zone = resolve_named_zone(zone_name)
    aware = local_naive.replace(tzinfo=zone)
    # Detect fold/gap by checking whether the round trip is stable.
    utc_value = aware.astimezone(UTC)
    back = utc_value.astimezone(zone)
    if back.replace(tzinfo=None) != local_naive:
        # Gap: nonexistent local time. Advance to the first valid instant after
        # the gap by stepping forward in 15-minute increments (bounded).
        candidate = local_naive
        for _ in range(4 * 24):
            candidate = candidate + timedelta(minutes=15)
            probe = candidate.replace(tzinfo=zone)
            utc_probe = probe.astimezone(UTC)
            if utc_probe.astimezone(zone).replace(tzinfo=None) == candidate:
                offset = utc_probe.astimezone(zone).utcoffset()
                return LocalOccurrence(
                    zone=zone_name,
                    local_naive=local_naive.isoformat(),
                    utc=to_rfc3339(utc_probe),
                    policy="SPRING_GAP_ADVANCED",
                    resolved_offset_seconds=int(offset.total_seconds()) if offset else 0,
                    fold=0,
                )
        raise ValueError(f"could not resolve local time {local_naive} in {zone_name}")
    fold = getattr(aware, "fold", 0)
    # Detect ambiguity: if fold=1 gives a different UTC instant, the time is
    # ambiguous; policy chooses the first occurrence (fold=0).
    aware_fold1 = local_naive.replace(tzinfo=zone, fold=1)
    if aware_fold1.astimezone(UTC) != utc_value:
        offset = aware.utcoffset()
        return LocalOccurrence(
            zone=zone_name,
            local_naive=local_naive.isoformat(),
            utc=to_rfc3339(utc_value),
            policy="AUTUMN_FOLD_FIRST",
            resolved_offset_seconds=int(offset.total_seconds()) if offset else 0,
            fold=0,
        )
    offset = aware.utcoffset()
    return LocalOccurrence(
        zone=zone_name,
        local_naive=local_naive.isoformat(),
        utc=to_rfc3339(utc_value),
        policy="EXACT",
        resolved_offset_seconds=int(offset.total_seconds()) if offset else 0,
        fold=0,
    )


class Clock:
    """Injectable clock for deterministic tests."""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        self._now_fn = now_fn or utc_now

    def now(self) -> datetime:
        return self._now_fn()

    def now_s(self) -> str:
        return to_rfc3339(self.now())

    def advance(self, seconds: float) -> None:
        """For fake clocks: advance the injected time by ``seconds``."""
        base = self._now_fn
        current = base()

        def advanced() -> datetime:
            return current + timedelta(seconds=seconds)

        self._now_fn = advanced


class FakeClock(Clock):
    """Deterministic mutable clock for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._current = start or utc_now()
        super().__init__(self._tick)

    def _tick(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:  # type: ignore[override]
        from datetime import timedelta as _td

        self._current = self._current + _td(seconds=seconds)


__all__ = [
    "UTC",
    "Clock",
    "FakeClock",
    "LocalOccurrence",
    "add_seconds",
    "compare_timestamps",
    "parse_rfc3339",
    "resolve_local_time",
    "resolve_named_zone",
    "to_rfc3339",
    "tzdata_version",
    "utc_now",
    "utc_now_s",
    "ZoneInfoNotFoundError",
]
