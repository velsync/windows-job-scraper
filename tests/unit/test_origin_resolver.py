"""S2.1 — origin resolver (02 §32).

Proves the resolver chain

    observation → bounded redirect unwrap → tracking cleanup → ATS
    fingerprint(endpoint) → origin provider/board/job id → direct
    application-URL candidate → evidence-backed origin relation

without ever performing I/O of its own: it consumes the recorded redirect
chain / final URL plus the observation's own URL candidates.  Low-confidence
resolutions must come back UNRESOLVED rather than guessed, and the original
source link is never overwritten.
"""

from __future__ import annotations

from jobscraper.acquisition.origin import (
    ORIGIN_CONFIDENCE_THRESHOLD,
    OriginStatus,
    resolve_origin,
)

NOW = "2026-09-08T09:00:00.000000Z"


def _resolve(**kwargs):
    kwargs.setdefault("observed_at", NOW)
    return resolve_origin(**kwargs)


def test_aggregator_job_url_resolves_to_greenhouse_origin_from_its_own_link():
    resolution = _resolve(
        discovery_url="http://127.0.0.1:9/aggregator?page=1",
        raw_source_url="http://127.0.0.1:9/aggregator?page=1",
        canonical_job_url="https://careers.example.test/jobs/12345",
        application_url="https://boards.greenhouse.io/acme/jobs/12345?gh_jid=12345&utm_source=agg",
        redirect_chain=(),
        final_url="http://127.0.0.1:9/aggregator?page=1",
    )
    assert resolution.status is OriginStatus.RESOLVED
    assert (resolution.origin_provider, resolution.origin_board, resolution.origin_job_id) == (
        "GREENHOUSE",
        "acme",
        "12345",
    )
    # the proposed direct application URL is the canonical ATS job page ...
    assert resolution.direct_application_url == "https://boards.greenhouse.io/acme/jobs/12345"
    # ... while the recorded origin relation is the tracking-cleaned URL the
    # source actually reported (gh_jid is semantic for Greenhouse, so it stays)
    assert resolution.origin_url == "https://boards.greenhouse.io/acme/jobs/12345?gh_jid=12345"
    # the original aggregator link survives untouched (§32)
    assert resolution.preserved_source_url == "http://127.0.0.1:9/aggregator?page=1"
    assert resolution.confidence >= ORIGIN_CONFIDENCE_THRESHOLD
    kinds = {item.kind for item in resolution.evidence}
    assert {"ATS_ENDPOINT", "TRACKING_CLEANED"} <= kinds
    assert {item.field for item in resolution.evidence if item.kind == "ATS_ENDPOINT"} == {
        "application_url"
    }


def test_bounded_redirect_unwrap_uses_only_the_recorded_chain():
    resolution = _resolve(
        discovery_url="https://aggregator.example.test/job/77",
        raw_source_url="https://aggregator.example.test/job/77",
        canonical_job_url=None,
        application_url=None,
        redirect_chain=[
            "https://aggregator.example.test/job/77",
            "https://jobs.lever.co/acme/2f2b9d5c-1111-2222-3333-444455556666?utm_medium=agg",
        ],
        final_url="https://jobs.lever.co/acme/2f2b9d5c-1111-2222-3333-444455556666?utm_medium=agg",
    )
    assert resolution.status is OriginStatus.RESOLVED
    assert (resolution.origin_provider, resolution.origin_board, resolution.origin_job_id) == (
        "LEVER",
        "acme",
        "2f2b9d5c-1111-2222-3333-444455556666",
    )
    assert any(i.kind == "REDIRECT_UNWRAP" for i in resolution.evidence)
    # the *discovery* link is still the aggregator, never the unwrapped target
    assert resolution.preserved_source_url == "https://aggregator.example.test/job/77"


def test_redirect_unwrap_is_bounded():
    chain = [f"https://hop{i}.example.test/{i}" for i in range(25)]
    chain.append("https://jobs.lever.co/acme/aaaa1111-2222-3333-4444-555566667777")
    resolution = _resolve(
        discovery_url=chain[0],
        raw_source_url=chain[0],
        canonical_job_url=None,
        application_url=None,
        redirect_chain=chain,
        final_url=chain[-1],
    )
    # the final hop is still examined (bounded, not abandoned): only the
    # number of *intermediate* hops considered is capped
    assert resolution.max_unwrap_hops >= 1
    assert len(resolution.evidence) <= resolution.max_unwrap_hops + 4


def test_employer_own_board_url_is_origin_but_stays_employer_source():
    """An employer board URL is its own origin: resolution succeeds and the
    source class stays employer-side (no fake 'aggregator' downgrade)."""
    resolution = _resolve(
        discovery_url="https://boards.greenhouse.io/acme",
        raw_source_url="https://boards.greenhouse.io/acme/jobs/42",
        canonical_job_url="https://boards.greenhouse.io/acme/jobs/42",
        application_url="https://boards.greenhouse.io/acme/jobs/42",
        redirect_chain=(),
        final_url="https://boards.greenhouse.io/acme/jobs/42",
    )
    assert resolution.status is OriginStatus.RESOLVED
    assert resolution.origin_provider == "GREENHOUSE"
    assert resolution.same_host_as_source is True


def test_unknown_source_without_ats_shape_is_unresolved_and_unchanged():
    resolution = _resolve(
        discovery_url="https://careers.example.test/jobs/1",
        raw_source_url="https://careers.example.test/jobs/1",
        canonical_job_url="https://careers.example.test/jobs/1",
        application_url="https://careers.example.test/jobs/1/apply",
        redirect_chain=(),
        final_url="https://careers.example.test/jobs/1",
    )
    assert resolution.status is OriginStatus.UNRESOLVED
    assert resolution.origin_provider is None
    assert resolution.origin_job_id is None
    assert resolution.confidence < ORIGIN_CONFIDENCE_THRESHOLD
    assert resolution.direct_application_url is None
    assert any(i.kind == "NO_ATS_PATTERN" for i in resolution.evidence)


def test_ambiguous_low_confidence_shape_is_not_forced():
    """A generic-looking path on a *known* host without a job id must not
    fabricate an origin: unresolved beats a confident-looking guess."""
    resolution = _resolve(
        discovery_url="https://jobs.ashbyhq.com/acme",
        raw_source_url="https://jobs.ashbyhq.com/acme",
        canonical_job_url="https://jobs.ashbyhq.com/acme",
        application_url=None,
        redirect_chain=(),
        final_url="https://jobs.ashbyhq.com/acme",
    )
    assert resolution.status is OriginStatus.UNRESOLVED
    assert resolution.origin_job_id is None


def test_cross_provider_conflict_is_unresolved_and_recorded():
    resolution = _resolve(
        discovery_url="https://agg.example.test/x",
        raw_source_url="https://agg.example.test/x",
        canonical_job_url="https://jobs.lever.co/acme/aaaaaaaa-1111-2222-3333-444455556666",
        application_url="https://boards.greenhouse.io/acme/jobs/777",
        redirect_chain=(),
        final_url="https://agg.example.test/x",
    )
    assert resolution.status is OriginStatus.UNRESOLVED
    assert resolution.origin_provider is None
    assert any(i.kind == "PROVIDER_CONFLICT" for i in resolution.evidence)
    assert resolution.conflict == {"canonical_job_url": "LEVER", "application_url": "GREENHOUSE"}


def test_resolution_is_deterministic_and_idempotent():
    kwargs = dict(
        discovery_url="https://agg.example.test/x",
        raw_source_url="https://agg.example.test/x",
        canonical_job_url=None,
        application_url="https://job-boards.greenhouse.io/acme/jobs/5?fbclid=abc",
        redirect_chain=(),
        final_url="https://agg.example.test/x",
    )
    first = _resolve(**kwargs)
    second = _resolve(**kwargs)
    assert first == second
    assert first.evidence == second.evidence
    assert first.origin_url == "https://job-boards.greenhouse.io/acme/jobs/5"


def test_javascript_and_credential_bearing_candidates_are_ignored():
    resolution = _resolve(
        discovery_url="https://agg.example.test/x",
        raw_source_url="https://agg.example.test/x",
        canonical_job_url="javascript://boards.greenhouse.io/acme/jobs/1",
        application_url="https://u:p@boards.greenhouse.io/acme/jobs/1",
        redirect_chain=("https://boards.greenhouse.io/acme/jobs/2",),
        final_url="https://agg.example.test/x",
    )
    # only the clean redirect target is usable evidence; hostile candidates are
    # recorded as rejected rather than resolved or emitted as a link
    assert {i["reason"] for i in resolution.rejected_candidates} == {
        "EMBEDDED_CREDENTIALS", "UNSUPPORTED_SCHEME"
    }
    assert resolution.rejected_candidates
    assert all("javascript" not in str(v) for v in (resolution.origin_url, resolution.direct_application_url))


def test_evidence_entries_carry_the_matching_field_and_locator():
    resolution = _resolve(
        discovery_url="https://agg.example.test/x",
        raw_source_url="https://agg.example.test/x",
        canonical_job_url="https://boards.greenhouse.io/acme/jobs/8",
        application_url=None,
        redirect_chain=(),
        final_url="https://agg.example.test/x",
    )
    item = next(i for i in resolution.evidence if i.kind == "ATS_ENDPOINT")
    assert item.field == "canonical_job_url"
    assert item.pattern_id
    assert item.observed_at == NOW


def test_serialisation_round_trip_is_redacted_and_json_safe():
    resolution = _resolve(
        discovery_url="https://agg.example.test/x",
        raw_source_url="https://agg.example.test/x",
        canonical_job_url="https://jobs.ashbyhq.com/acme/bbbbbbbb-1111-2222-3333-444455556666",
        application_url=None,
        redirect_chain=(),
        final_url="https://agg.example.test/x",
    )
    payload = resolution.as_dict()
    assert payload["status"] == "RESOLVED"
    assert payload["resolver_version"] >= 1
    # Authorization/Cookie-shaped keys must never appear in persisted evidence
    assert "authorization" not in str(payload).lower()
    assert "cookie" not in str(payload).lower()
