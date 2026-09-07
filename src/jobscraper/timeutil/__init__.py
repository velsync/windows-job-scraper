"""Time and timezone utilities.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-03A;
docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md sections 49/50.

Durable timestamps are UTC RFC 3339 strings (``rfc3339``). Local schedules use
IANA identifiers resolved through the pinned ``tzdata`` package (``zones``),
with deterministic DST policies:

* nonexistent local time (spring gap)   -> first valid instant after the gap;
* ambiguous local time (autumn fold)    -> first occurrence (fold=0), with the
  resolved offset recorded.
"""

from jobscraper.timeutil.rfc3339 import (
    UTC,
    Clock,
    FakeClock,
    add_seconds,
    compare_timestamps,
    parse_rfc3339,
    to_rfc3339,
    utc_now,
    utc_now_s,
)
from jobscraper.timeutil.zones import (
    LocalOccurrence,
    ZoneInfoNotFoundError,
    resolve_local_time,
    resolve_named_zone,
    tzdata_version,
)

__all__ = [
    "UTC",
    "Clock",
    "FakeClock",
    "LocalOccurrence",
    "ZoneInfoNotFoundError",
    "add_seconds",
    "compare_timestamps",
    "parse_rfc3339",
    "resolve_local_time",
    "resolve_named_zone",
    "to_rfc3339",
    "tzdata_version",
    "utc_now",
    "utc_now_s",
]
