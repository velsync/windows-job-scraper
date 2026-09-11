"""S3.4 deterministic retry policy tests."""

from __future__ import annotations

from datetime import datetime, timezone
import math

from jobscraper.acquisition.failures import FailureKind, FailureRecord
from jobscraper.runtime.retry import RetryAction, RetryPolicy, decide_retry, parse_retry_after


def _failure(kind: FailureKind, retryable: bool = True) -> FailureRecord:
    return FailureRecord(kind=kind, retryable=retryable)


def test_retry_after_delta_seconds_is_honored_over_backoff():
    decision = decide_retry(
        _failure(FailureKind.RATE_LIMIT),
        page_class="RATE_LIMITED",
        attempt_count=1,
        max_attempts=3,
        headers={"Retry-After": "120"},
        policy=RetryPolicy(base_delay_s=5, max_delay_s=300, jitter_fraction=0),
        random_unit=lambda: 0.5,
    )
    assert decision.action is RetryAction.RETRY
    assert decision.delay_s == 120
    assert decision.retry_after_s == 120


def test_attempt_budget_exhaustion_is_terminal():
    decision = decide_retry(
        _failure(FailureKind.HTTP_5XX),
        page_class="UNKNOWN",
        attempt_count=3,
        max_attempts=3,
        headers={},
    )
    assert decision.action is RetryAction.FAIL
    assert decision.budget_exhausted
    assert decision.delay_s is not None
    assert decision.delay_s > 0


def test_auth_and_policy_fail_without_adaptive_retry():
    auth = decide_retry(
        _failure(FailureKind.AUTH_REQUIRED, retryable=False),
        page_class="LOGIN_REQUIRED",
        attempt_count=1,
        max_attempts=3,
        headers={},
    )
    policy = decide_retry(
        _failure(FailureKind.POLICY_REJECTED, retryable=False),
        page_class="UNKNOWN",
        attempt_count=1,
        max_attempts=3,
        headers={},
    )
    assert auth.action is RetryAction.FAIL
    assert policy.action is RetryAction.FAIL


def test_http_date_retry_after_is_deterministic_with_injected_clock():
    now = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Fri, 11 Sep 2026 08:02:00 GMT", now=now) == 120


def test_retry_after_negative_is_clamped_and_nonfinite_is_rejected():
    now = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    assert parse_retry_after("-5", now=now) == 0
    assert parse_retry_after("inf", now=now) is None
    huge = parse_retry_after("1e30", now=now)
    assert huge is not None and math.isfinite(huge)


def test_budget_exhausted_retry_after_is_preserved_for_source_cooldown():
    now = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    decision = decide_retry(
        _failure(FailureKind.RATE_LIMIT),
        page_class="RATE_LIMITED",
        attempt_count=3, max_attempts=3,
        headers={"Retry-After": "120"}, now=now,
        policy=RetryPolicy(base_delay_s=5, max_delay_s=300, jitter_fraction=0),
    )
    assert decision.action is RetryAction.FAIL
    assert decision.budget_exhausted
    assert decision.delay_s == 120
    assert decision.retry_after_s == 120
