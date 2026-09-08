"""Unit tests for canonical provenance selection (01 §39, ROAD-03).

§39 fixes the ordering: employer structured ATS/API → employer careers page →
aggregator with a resolved employer origin → aggregator without one.  Slice 1
proxied this through strategy quality; Slice 2 classifies each presence from
its *recorded* evidence and stores the class, so selection is durable,
inspectable and replayable.  Selection controls presentation only — these tests
also pin that nothing about the losers is destroyed.
"""

from __future__ import annotations

import pytest

from jobscraper.pipeline.provenance import (
    AGGREGATOR_WITHOUT_RESOLVED_ORIGIN,
    AGGREGATOR_WITH_RESOLVED_ORIGIN,
    EMPLOYER_CAREERS_PAGE,
    EMPLOYER_STRUCTURED_API,
    EMPLOYER_STRUCTURED_ATS,
    PROVENANCE_SELECTOR_VERSION,
    SOURCE_QUALITY_CLASSES,
    classify_source_quality,
    select_canonical_provenance,
)


def test_classes_are_ordered_exactly_as_section_39_lists_them():
    assert SOURCE_QUALITY_CLASSES == (
        EMPLOYER_STRUCTURED_ATS,
        EMPLOYER_STRUCTURED_API,
        EMPLOYER_CAREERS_PAGE,
        AGGREGATOR_WITH_RESOLVED_ORIGIN,
        AGGREGATOR_WITHOUT_RESOLVED_ORIGIN,
    )


def test_ats_provider_adapter_with_structured_content_is_the_top_class():
    assert (
        classify_source_quality(
            strategy="PROVIDER_NATIVE",
            execution_class="HTTP",
            content_kind="STRUCTURED",
            same_host_as_source=True,
            origin_status="RESOLVED",
            origin_provider="GREENHOUSE",
        )
        == EMPLOYER_STRUCTURED_ATS
    )


def test_aggregator_host_structured_content_with_resolved_origin_is_an_aggregator():
    # the fetch host is not the ATS host, so the posting is reachable through
    # an aggregator even though its employer origin is now known
    assert (
        classify_source_quality(
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            content_kind="STRUCTURED",
            same_host_as_source=False,
            origin_status="RESOLVED",
            origin_provider="GREENHOUSE",
        )
        == AGGREGATOR_WITH_RESOLVED_ORIGIN
    )


def test_employer_own_structured_endpoint_is_classified_by_its_host():
    assert (
        classify_source_quality(
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            content_kind="STRUCTURED",
            same_host_as_source=True,
            origin_status=None,
            origin_provider=None,
        )
        == EMPLOYER_STRUCTURED_API
    )


def test_employer_html_page_is_a_careers_page():
    assert (
        classify_source_quality(
            strategy="HTTP_HTML",
            execution_class="HTTP",
            content_kind="HTML",
            same_host_as_source=True,
            origin_status=None,
            origin_provider=None,
        )
        == EMPLOYER_CAREERS_PAGE
    )


def test_unresolved_origin_on_a_foreign_host_never_claims_employer_quality():
    assert (
        classify_source_quality(
            strategy="HTTP_HTML",
            execution_class="HTTP",
            content_kind="HTML",
            same_host_as_source=False,
            origin_status="UNRESOLVED",
            origin_provider=None,
        )
        == AGGREGATOR_WITHOUT_RESOLVED_ORIGIN
    )


def test_employer_family_declaration_survives_an_apply_link_elsewhere():
    assert (
        classify_source_quality(
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            content_kind="STRUCTURED",
            same_host_as_source=True,
            origin_status="RESOLVED",
            origin_provider="GREENHOUSE",
            source_family="EMPLOYER_CAREERS",
            origin_on_source_host=False,
        )
        == EMPLOYER_STRUCTURED_ATS
    )


def test_self_hosted_aggregator_copy_is_demoted_by_its_resolved_origin():
    # the copy sits on the board's own host, but §32 located the posting's
    # home at an ATS elsewhere -> aggregator with resolved origin, never
    # employer quality
    assert (
        classify_source_quality(
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            content_kind="STRUCTURED",
            same_host_as_source=True,
            origin_status="RESOLVED",
            origin_provider="GREENHOUSE",
            source_family="PUBLIC_BOARD",
            origin_on_source_host=False,
        )
        == AGGREGATOR_WITH_RESOLVED_ORIGIN
    )


def test_ats_board_family_is_employer_side_structured_quality():
    assert (
        classify_source_quality(
            strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            execution_class="HTTP",
            content_kind="STRUCTURED",
            same_host_as_source=False,
            origin_status="RESOLVED",
            origin_provider="GREENHOUSE",
            source_family="ATS_BOARD",
            origin_on_source_host=True,
        )
        == EMPLOYER_STRUCTURED_ATS
    )


def test_selection_prefers_the_highest_class_over_recency_and_strategy():
    presences = [
        _presence("p-old-good", EMPLOYER_STRUCTURED_ATS, "2026-01-01T00:00:00Z", "PROVIDER_NATIVE"),
        _presence(
            "p-new-bad",
            AGGREGATOR_WITHOUT_RESOLVED_ORIGIN,
            "2026-09-01T00:00:00Z",
            "PROVIDER_NATIVE",
        ),
    ]
    assert select_canonical_provenance(presences)["id"] == "p-old-good"


def test_selection_ties_break_on_strategy_then_recency_then_stable_id():
    presences = [
        _presence("p-b", EMPLOYER_CAREERS_PAGE, "2026-05-05T00:00:00Z", "HTTP_HTML"),
        _presence("p-a", EMPLOYER_CAREERS_PAGE, "2026-05-05T00:00:00Z", "HTTP_HTML"),
        _presence("p-c", EMPLOYER_CAREERS_PAGE, "2026-06-06T00:00:00Z", "STRUCTURED_PAGE"),
    ]
    assert select_canonical_provenance(presences)["id"] == "p-c"
    without_c = presences[:2]
    assert select_canonical_provenance(without_c)["id"] == "p-a"  # smallest id wins


def test_missing_class_falls_back_to_the_strategy_proxy_without_inventing_quality():
    # a presence persisted before v12 has no stored class
    row = {
        "id": "p-legacy",
        "source_quality_class": None,
        "strategy_source": "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
        "last_seen_at": "2026-08-08T00:00:00Z",
    }
    winner = select_canonical_provenance([row])
    assert winner["id"] == "p-legacy"
    assert row["source_quality_class"] is None  # never back-filled by reading


def test_selection_from_no_presences_is_an_error_not_a_silent_none():
    with pytest.raises(ValueError):
        select_canonical_provenance([])


def test_selector_version_is_recorded():
    assert PROVENANCE_SELECTOR_VERSION == "canonical-provenance-selector-v2"


def test_selection_is_a_pure_order_over_the_supplied_rows():
    presences = [
        _presence("p-1", EMPLOYER_CAREERS_PAGE, "2026-05-05T00:00:00Z", "HTTP_HTML"),
        _presence("p-2", AGGREGATOR_WITH_RESOLVED_ORIGIN, "2026-06-06T00:00:00Z", "HTTP_HTML"),
    ]
    snapshot = [dict(p) for p in presences]
    select_canonical_provenance(presences)
    assert presences == snapshot  # presentation selection mutates nothing


def _presence(presence_id, quality_class, last_seen_at, strategy):
    return {
        "id": presence_id,
        "source_quality_class": quality_class,
        "last_seen_at": last_seen_at,
        "strategy_source": strategy,
    }
