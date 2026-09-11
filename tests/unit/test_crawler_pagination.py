from jobscraper.acquisition.crawler.pagination import (
    PaginationGuardState,
    PaginationSignature,
    PaginationStopKind,
    advance_guard,
)
from jobscraper.adapters.contract import StopPolicy


POLICY = StopPolicy(max_pages=10, max_consecutive_empty_pages=2, max_consecutive_no_new_jobs_pages=3, max_duplicate_pages=2, max_runtime_s=60, max_requests=20)


def test_tracking_only_next_url_variant_is_a_pagination_loop():
    state, first = advance_guard(PaginationGuardState(), PaginationSignature.from_values(next_url="https://jobs.example.test/list?page=2&utm_source=x", observations_added=1), POLICY)
    assert first.kind is PaginationStopKind.CONTINUE
    _, second = advance_guard(state, PaginationSignature.from_values(next_url="https://jobs.example.test/list?page=2&utm_source=y", observations_added=1), POLICY)
    assert second.kind is PaginationStopKind.PAGINATION_LOOP


def test_repeated_cursor_is_restart_safe_loop_detection():
    state, _ = advance_guard(PaginationGuardState(), PaginationSignature.from_values(next_cursor='{"page":2}', observations_added=1), POLICY)
    restored = PaginationGuardState.from_json(state.to_json())
    _, decision = advance_guard(restored, PaginationSignature.from_values(next_cursor='{"page":2}', observations_added=1), POLICY)
    assert decision.kind is PaginationStopKind.PAGINATION_LOOP
    assert decision.failure_kind == "PAGINATION_LOOP"


def test_empty_page_does_not_stop_until_declared_threshold():
    state, one = advance_guard(PaginationGuardState(), PaginationSignature.from_values(next_cursor="1", recognized_empty=True), POLICY)
    assert one.kind is PaginationStopKind.CONTINUE
    _, two = advance_guard(state, PaginationSignature.from_values(next_cursor="2", recognized_empty=True), POLICY)
    assert two.kind is PaginationStopKind.EMPTY_LIMIT
