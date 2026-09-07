"""IANA zone resolution and DST policy helpers (WIN-03A).

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-03A;
docs/plans/slice-0-worker-implementation-plan-v0313.md (S0.10).

User schedules store IANA timezone identifiers resolved through the pinned
``tzdata`` package — never an ambient system IANA database. The DST policy is
deterministic:

* nonexistent local time (spring gap)   -> first valid instant after the gap;
* ambiguous local time (autumn fold)    -> first occurrence (fold=0), with the
  resolved offset recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobscraper.timeutil.rfc3339 import to_rfc3339

UTC = timezone.utc

__all__ = [
    "LocalOccurrence",
    "ZoneInfoNotFoundError",
    "resolve_local_time",
    "resolve_named_zone",
    "tzdata_version",
]


def resolve_named_zone(name: str) -> ZoneInfo:
    """Resolve an IANA zone identifier via the pinned tzdata package."""
    return ZoneInfo(name)


def tzdata_version() -> str:
    """Best-effort version of the pinned tzdata package."""
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
