"""Unit tests for deterministic salary normalization (01 §37).

Authority: docs/spec/v0.3.1.3/01_content_cleaning_and_search.md §34/§37;
the Slice-1 normalization authority ``jobscraper.pipeline.normalize`` owns
``parse_salary`` (single-writer: adapters never coerce salary themselves).

Deferred finding **DF-1** (S2.7 review): a provider-verbatim range whose
*first* number carries a K-suffix (``"$150K - $210K"``) collapsed its max to
the min, because the range pattern only expanded the K-suffix on the second
number — the K after the first number kept the range regex from matching at
all, so the string degraded to the single-number parse on ``150``.  The raw
string is preserved verbatim as evidence + ``salary_original_text`` either
way; this file pins the host parser behaviour itself.
"""

from __future__ import annotations

import pytest

from jobscraper.pipeline.normalize import parse_salary


class TestParseSalaryRangeKSuffix:
    @pytest.mark.parametrize(
        "text,expected_min,expected_max",
        [
            # provider-verbatim Ashby/Lever ranges: K on both endpoints
            ("$150K - $210K", 150000, 210000),
            ("$130K - $224K", 130000, 224000),
            # K only on the first endpoint
            ("$150K - $210000", 150000, 210000),
            # K only on the second endpoint
            ("$150000 - $210K", 150000, 210000),
            # plain integer range with no K anywhere (unchanged behaviour)
            ("$80,000 - $120,000", 80000, 120000),
        ],
    )
    def test_a_k_suffix_range_expands_both_endpoints(self, text, expected_min, expected_max):
        low, high, currency, period = parse_salary(text)
        assert (low, high) == (float(expected_min), float(expected_max)), text
        assert currency == "USD"

    def test_a_k_suffix_single_number_is_unchanged(self):
        # a lone K value is not a range and must keep parsing exactly as before
        low, high, currency, period = parse_salary("$100K")
        assert (low, high) == (100000.0, 100000.0)
        assert currency == "USD"

    def test_the_original_text_is_preserved_by_the_caller_not_the_parser(self):
        # parse_salary never fabricates or rewrites; the raw string is the
        # caller's verbatim-evidence responsibility.  Parser only returns numbers.
        low, high, currency, period = parse_salary("$150K - $210K")
        assert low == 150000 and high == 210000
