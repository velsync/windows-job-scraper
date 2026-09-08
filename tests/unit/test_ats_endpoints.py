"""S2.1 — versioned ATS endpoint pattern table (02 §12.1/§32).

The table is the single owner of "known ATS endpoint patterns" for both the
origin resolver and (S2.4) the fingerprint classifier.  It is data + pure
matching: no I/O, no DB, and it may never invent an identifier that the URL
does not actually contain.
"""

from __future__ import annotations

import pytest

from jobscraper.acquisition.atsendpoints import (
    ATS_ENDPOINT_SPECS,
    ENDPOINT_RULES_VERSION,
    identify_url,
    spec_for_provider,
)


def test_table_is_versioned_and_covers_the_slice2_providers():
    assert ENDPOINT_RULES_VERSION >= 1
    providers = {spec.provider for spec in ATS_ENDPOINT_SPECS}
    assert {"GREENHOUSE", "LEVER", "ASHBY"} <= providers
    for spec in ATS_ENDPOINT_SPECS:
        assert spec.host_suffixes and spec.api_hosts and spec.job_url_template
        # the application-link template must be board+job specific (§31:
        # a generic careers/apply URL alone may not identify a job)
        assert "{board}" in spec.job_url_template and "{job_id}" in spec.job_url_template


def test_greenhouse_api_url_identifies_board_and_job():
    match = identify_url(
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1234567?department%5B%5D=eng"
    )
    assert match.provider == "GREENHOUSE"
    assert match.board == "acme"
    assert match.job_id == "1234567"
    assert match.application_url == "https://boards.greenhouse.io/acme/jobs/1234567"
    assert match.evidence


def test_greenhouse_hosted_board_url_identifies_job():
    match = identify_url("https://job-boards.greenhouse.io/acme/jobs/4294967?gh_jid=4294967")
    assert match.provider == "GREENHOUSE"
    assert match.board == "acme"
    assert match.job_id == "4294967"


def test_lever_and_ashby_shapes():
    lever = identify_url("https://jobs.lever.co/acme/2f2b9d5c-1111-2222-3333-444455556666")
    assert (lever.provider, lever.board, lever.job_id) == (
        "LEVER",
        "acme",
        "2f2b9d5c-1111-2222-3333-444455556666",
    )
    lever_api = identify_url("https://api.lever.co/v0/postings/acme/5f0d1a2b3c4d5e6f7a8b9c0d")
    assert (lever_api.provider, lever_api.board, lever_api.job_id) == (
        "LEVER", "acme", "5f0d1a2b3c4d5e6f7a8b9c0d")
    ashby = identify_url("https://jobs.ashbyhq.com/acme/5f0d1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b")
    assert (ashby.provider, ashby.board) == ("ASHBY", "acme")
    ashby_api = identify_url("https://api.ashbyhq.com/posting-api/job-board/acme")
    assert (ashby_api.provider, ashby_api.board, ashby_api.job_id) == ("ASHBY", "acme", None)


def test_non_ats_url_matches_nothing_and_invents_nothing():
    match = identify_url("https://careers.example.test/jobs/12345")
    assert match.provider is None
    assert match.board is None
    assert match.job_id is None
    assert match.application_url is None
    assert match.evidence == ()


def test_board_root_is_never_a_job_identity():
    """§31: a generic careers/home/apply URL alone must not identify a job."""
    for url in (
        "https://boards.greenhouse.io/acme",
        "https://jobs.lever.co/acme",
        "https://jobs.ashbyhq.com/acme",
        "https://api.lever.co/v0/postings/acme",
    ):
        match = identify_url(url)
        assert match.job_id is None, url


def test_credentials_and_weird_schemes_are_rejected_not_resolved():
    for url in (
        "https://user:pass@boards.greenhouse.io/acme/jobs/1",
        "javascript://boards.greenhouse.io/acme/jobs/1",
        "not a url",
        "",
    ):
        match = identify_url(url)
        assert match.provider is None, url


def test_lookalike_host_is_not_trusted():
    # suffix matching must be on label boundaries: "greenhouse.io.evil.test"
    # is not a greenhouse host
    assert identify_url("https://boards.greenhouse.io.evil.test/acme/jobs/1").provider is None


def test_spec_lookup_is_exact_and_unknown_provider_is_typed():
    assert spec_for_provider("GREENHOUSE").provider == "GREENHOUSE"
    with pytest.raises(KeyError):
        spec_for_provider("NOT_AN_ATS")
