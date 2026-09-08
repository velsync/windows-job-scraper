"""Unit tests for the deterministic multi-location normalizer (01 §33.2).

The canonical model must keep a *set* of locations with structured fields —
never a single `location_text`.  Every rule here is versioned and
deterministic, unrecognized input is preserved at low confidence rather than
dropped, and nothing is guessed (no country inference from a bare city name,
no timezone without a zone in the evidence).
"""

from __future__ import annotations

import pytest

from jobscraper.pipeline.locations import (
    LOCATION_RULES_VERSION,
    LocationRecord,
    location_summary,
    normalize_locations,
)


def test_bare_remote_is_not_treated_as_worldwide():
    from jobscraper.pipeline.locations import remote_worldwide

    rows = normalize_locations(["Remote"])
    assert rows[0].remote == 1
    assert remote_worldwide(rows) is False  # no country proven -> no worldwide claim
    explicit = normalize_locations(["Remote — worldwide"])
    assert remote_worldwide(explicit) is True


def test_rules_are_versioned():
    assert LOCATION_RULES_VERSION == "location-rules-v1"


def test_city_country_string_is_split_and_country_mapped():
    rows = normalize_locations(["Berlin, Germany / Paris, France"])
    assert len(rows) == 2
    assert rows[0].city == "Berlin"
    assert rows[0].country == "DE"
    assert rows[1].city == "Paris"
    assert rows[1].country == "FR"
    assert all(r.confidence >= 0.8 for r in rows)
    # raw text is preserved exactly as the source said it
    assert rows[0].raw_text == "Berlin, Germany"


def test_city_only_string_keeps_city_without_inventing_a_country():
    (row,) = normalize_locations(["Oradea"])
    assert row.city == "Oradea"
    assert row.country is None
    assert row.region is None
    assert row.confidence < 0.8  # weaker evidence, still recorded


def test_unknown_input_is_never_dropped():
    (row,) = normalize_locations(["Somewhere weird § — see posting"])
    assert row.raw_text == "Somewhere weird § — see posting"
    assert row.confidence == pytest.approx(0.3)
    assert row.city is None and row.country is None


def test_structured_dict_location_carries_all_fields():
    (row,) = normalize_locations(
        [
            {
                "city": "Cluj-Napoca",
                "region": "Cluj",
                "country": "RO",
                "remote": True,
                "id": "loc-77",
                "timezone": "Europe/Bucharest",
            }
        ],
        reference_at="2026-06-15T09:00:00Z",
    )
    assert row.city == "Cluj-Napoca"
    assert row.region == "Cluj"
    assert row.country == "RO"
    assert row.remote == 1
    assert row.source_location_id == "loc-77"
    # timezone offsets come only from the zone in the evidence
    assert row.timezone_min == row.timezone_max == 3 * 60
    assert row.confidence >= 0.9


def test_greenhouse_applicant_location_requirements_are_honoured():
    rows = normalize_locations(
        [],
        applicant_location_requirements=[
            {"location_type": "Remote", "country": {"name": "Germany"}},
            {
                "location_type": "Hybrid or Remote",
                "state": {"name": "Bavaria"},
                "country": {"name": "Germany"},
                "city": "Munich",
            },
        ],
    )
    assert len(rows) == 2
    assert {r.country for r in rows} == {"DE"}
    assert rows[0].city is None  # country-level requirement, honestly
    assert rows[0].remote == 1
    assert rows[1].region == "Bavaria"
    assert rows[1].city == "Munich"


def test_lever_job_location_type_marks_remote():
    (row,) = normalize_locations(["Remote"], job_location_type="REMOTE")
    assert row.remote == 1
    assert row.city is None


def test_multi_zone_entry_records_the_offset_span():
    (row,) = normalize_locations(
        [
            {
                "city": "Dual hub",
                "alternate_timezones": ["America/New_York", "Europe/Berlin"],
            }
        ],
        reference_at="2026-06-15T09:00:00Z",
    )
    assert row.timezone_min < row.timezone_max
    assert row.timezone_min == -4 * 60  # EDT at the recorded instant
    assert row.timezone_max == 2 * 60  # CEST at the recorded instant


def test_no_timezone_evidence_means_no_timezone_columns():
    (row,) = normalize_locations(["Berlin, Germany"], reference_at="2026-06-15T09:00:00Z")
    assert row.timezone_min is None
    assert row.timezone_max is None


def test_duplicates_and_ordering_are_deterministic():
    first = normalize_locations(["Berlin, Germany", "berlin, germany", "Paris, France"])
    second = normalize_locations(["Berlin, Germany", "Berlin, Germany", "Paris, France"])
    assert [r.raw_text for r in first] == ["Berlin, Germany", "Paris, France"]
    assert [r.raw_text for r in second] == ["Berlin, Germany", "Paris, France"]
    assert first[0] == second[0]


def test_string_input_is_accepted_directly():
    rows = normalize_locations("Amsterdam, Netherlands")
    assert [r.city for r in rows] == ["Amsterdam"]
    assert rows[0].country == "NL"


def test_summary_is_presentation_only_and_derived_from_the_set():
    rows = normalize_locations(["Berlin, Germany / Remote (EMEA)"])
    summary = location_summary(rows)
    assert "Berlin" in summary and "Remote" in summary
    # presentation never round-trips into a stored canonical column
    assert isinstance(rows[0], LocationRecord)


def test_remote_token_without_a_city_is_flagged_not_invented_elsewhere():
    rows = normalize_locations(["Remote — EMEA"])
    assert len(rows) == 1
    assert rows[0].remote == 1
    assert rows[0].country is None
    assert rows[0].confidence < 0.8
