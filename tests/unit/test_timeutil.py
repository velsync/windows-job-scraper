"""Tests for time utilities and DST policy (WIN-03A)."""

from datetime import datetime

import pytest

from jobscraper.timeutil import (
    FakeClock,
    add_seconds,
    compare_timestamps,
    parse_rfc3339,
    resolve_local_time,
    resolve_named_zone,
    to_rfc3339,
    tzdata_version,
    utc_now,
    utc_now_s,
)


def test_rfc3339_roundtrip():
    now = utc_now()
    s = to_rfc3339(now)
    assert s.endswith("Z")
    back = parse_rfc3339(s)
    assert abs((back - now).total_seconds()) < 1e-3


def test_parse_rejects_garbage():
    for bad in ["", "not-a-time", "2026-13-01T00:00:00Z", None]:
        with pytest.raises(ValueError):
            parse_rfc3339(bad)


def test_compare_and_add():
    a = utc_now_s()
    b = add_seconds(a, 5)
    assert compare_timestamps(a, b) == -1
    assert compare_timestamps(b, a) == 1
    assert compare_timestamps(a, a) == 0


def test_named_zone_resolves():
    zone = resolve_named_zone("Europe/Berlin")
    assert zone is not None
    assert tzdata_version() != "unknown"


def test_spring_gap_advances():
    # 2027-03-28 02:30 Europe/Berlin does not exist (clocks jump 02:00->03:00).
    # Policy: advance to the first valid local instant after the gap (03:00 CEST).
    occ = resolve_local_time("Europe/Berlin", datetime(2027, 3, 28, 2, 30))
    assert occ.policy == "SPRING_GAP_ADVANCED"
    assert occ.utc == "2027-03-28T01:00:00.000000Z"
    assert occ.resolved_offset_seconds == 7200


def test_autumn_fold_uses_first_occurrence():
    # 2027-10-31 02:30 Europe/Berlin occurs twice.
    occ = resolve_local_time("Europe/Berlin", datetime(2027, 10, 31, 2, 30))
    assert occ.policy == "AUTUMN_FOLD_FIRST"
    assert occ.fold == 0
    # First occurrence: CEST (UTC+2) -> 00:30Z.
    assert occ.utc == "2027-10-31T00:30:00.000000Z"
    assert occ.resolved_offset_seconds == 7200


def test_exact_occurrence():
    occ = resolve_local_time("Europe/Berlin", datetime(2027, 6, 1, 12, 0))
    assert occ.policy == "EXACT"
    assert occ.resolved_offset_seconds == 7200


def test_fake_clock_advance():
    clock = FakeClock(start=parse_rfc3339("2026-01-01T00:00:00.000000Z"))
    assert clock.now_s() == "2026-01-01T00:00:00.000000Z"
    clock.advance(90)
    assert clock.now_s() == "2026-01-01T00:01:30.000000Z"
