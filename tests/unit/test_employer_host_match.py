"""Unit tests for the employer-host match that feeds the provenance class (§39).

``same_host_as_source`` decides between employer-side and aggregator classes, so
it is compared against the *source's* identity — the canonical host declared at
registration when present, otherwise the configured entry URL — and against the
posting links only.  It is derived from already-recorded URLs; nothing here may
trigger a request.
"""

from __future__ import annotations

from jobscraper.pipeline.driver import posting_host_matches_source


class _Observation:
    def __init__(self, canonical=None, application=None, raw=None):
        self.canonical_url_candidate = canonical
        self.application_url_candidate = application
        self.raw_url = raw


SOURCE = {
    "id": "src-1",
    "entry_url": "https://careers.acme.example/jobs",
    "canonical_host": "boards.greenhouse.io",
}


def test_canonical_host_is_authoritative_over_the_entry_url():
    assert (
        posting_host_matches_source(
            SOURCE, _Observation(canonical="https://boards.greenhouse.io/acme/j/42")
        )
        is True
    )
    assert (
        posting_host_matches_source(SOURCE, _Observation(canonical="https://careers.acme.example/j/42"))
        is False
    )


def test_entry_url_host_is_the_fallback():
    source = dict(SOURCE, canonical_host=None)
    assert (
        posting_host_matches_source(source, _Observation(application="https://careers.acme.example/j/42"))
        is True
    )
    assert (
        posting_host_matches_source(source, _Observation(application="https://boards.greenhouse.io/acme/j/42"))
        is False
    )


def test_posting_links_only_never_the_page_the_item_was_found_on():
    # the raw URL of an aggregator page may share the employer's host while the
    # posting itself lives elsewhere: only the posting links count
    assert (
        posting_host_matches_source(
            {"entry_url": "https://careers.acme.example/jobs"},
            _Observation(
                canonical="https://indeed.example/viewjob?k=1",
                application="https://other.example/apply",
                raw="https://careers.acme.example/jobs#postings",
            ),
        )
        is False
    )


def test_missing_evidence_is_not_a_match():
    assert posting_host_matches_source(SOURCE, _Observation()) is False
    assert posting_host_matches_source({"entry_url": None}, _Observation(canonical="https://x.example")) is False
    assert posting_host_matches_source(SOURCE, _Observation(canonical="not a url")) is False
    # a relative link cannot say which host the posting lives on
    assert (
        posting_host_matches_source(SOURCE, _Observation(canonical="/acme/j/42")) is False
    )
