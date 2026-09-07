"""Tests for IANA zone resolution through the pinned tzdata package (S0.10/WIN-03A)."""

from datetime import datetime

import pytest

from jobscraper.timeutil import resolve_local_time, resolve_named_zone, tzdata_version
from jobscraper.timeutil.zones import LocalOccurrence


def test_module_layout_zones_owns_zone_logic():
    # The plan file map requires timeutil/zones.py to own zone resolution.
    from jobscraper.timeutil import zones

    assert zones.resolve_named_zone is resolve_named_zone
    assert zones.LocalOccurrence is LocalOccurrence


def test_named_zone_resolves_with_pinned_tzdata():
    zone = resolve_named_zone("Europe/Berlin")
    assert zone.key == "Europe/Berlin"
    # tzdata version is recorded (Doctor requirement), not "unknown".
    assert tzdata_version() not in ("", "unknown")


def test_unknown_zone_raises():
    with pytest.raises(Exception):
        resolve_named_zone("Not/ARealZone")


def test_spring_gap_advances_to_first_valid_instant():
    # Europe/Berlin 2026-03-29 02:30 local does not exist (clocks jump 02:00->03:00).
    occ = resolve_local_time("Europe/Berlin", datetime(2026, 3, 29, 2, 30))
    assert occ.policy == "SPRING_GAP_ADVANCED"
    assert occ.utc == "2026-03-29T01:00:00.000000Z"  # first valid instant: 03:00 CEST (UTC+2)
    assert occ.resolved_offset_seconds == 7200


def test_autumn_fold_uses_first_occurrence():
    # Europe/Berlin 2026-10-25 02:30 local occurs twice (03:00 CEST -> 02:00 CET).
    occ = resolve_local_time("Europe/Berlin", datetime(2026, 10, 25, 2, 30))
    assert occ.policy == "AUTUMN_FOLD_FIRST"
    assert occ.resolved_offset_seconds == 7200  # first occurrence is CEST (UTC+2)
    assert occ.fold == 0


def test_exact_local_time():
    occ = resolve_local_time("Europe/Berlin", datetime(2026, 6, 1, 12, 0))
    assert occ.policy == "EXACT"
    assert occ.utc == "2026-06-01T10:00:00.000000Z"
