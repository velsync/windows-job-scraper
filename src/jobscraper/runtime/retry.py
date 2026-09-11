"""Deterministic retry/backoff policy (RUN-10, §15, S3.4)."""

from __future__ import annotations

import enum
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping

from jobscraper.acquisition.failures import FailureKind, FailureRecord


class RetryAction(enum.Enum):
    SUCCEED = "SUCCEED"
    RETRY = "RETRY"
    FAIL = "FAIL"


@dataclass(frozen=True)
class RetryPolicy:
    base_delay_s: float = 5.0
    max_delay_s: float = 300.0
    jitter_fraction: float = 0.20

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.base_delay_s)
            or not math.isfinite(self.max_delay_s)
            or self.base_delay_s < 0
            or self.max_delay_s < self.base_delay_s
        ):
            raise ValueError("invalid retry delay bounds")
        if (
            not math.isfinite(self.jitter_fraction)
            or not 0.0 <= self.jitter_fraction <= 1.0
        ):
            raise ValueError("jitter_fraction must be finite and between 0 and 1")


@dataclass(frozen=True)
class RetryDecision:
    action: RetryAction
    failure_kind: str | None = None
    delay_s: float | None = None
    retry_after_raw: str | None = None
    retry_after_s: float | None = None
    budget_exhausted: bool = False
    reason: str = ""


_TRANSIENT = frozenset(
    {
        FailureKind.DNS_ERROR,
        FailureKind.CONNECT_ERROR,
        FailureKind.TLS_ERROR,
        FailureKind.TIMEOUT,
        FailureKind.HTTP_5XX,
        FailureKind.RATE_LIMIT,
        FailureKind.CHALLENGE,
        FailureKind.BROWSER_CRASH,
        FailureKind.WORKER_CRASH,
    }
)

_PAGE_RETRY_KIND = {
    "RATE_LIMITED": FailureKind.RATE_LIMIT,
    "CHALLENGE_PAGE": FailureKind.CHALLENGE,
}
_PAGE_FAIL_KIND = {
    "LOGIN_REQUIRED": FailureKind.AUTH_REQUIRED,
    "AUTH_EXPIRED": FailureKind.AUTH_REQUIRED,
}


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def parse_retry_after(
    raw: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse delta-seconds or HTTP-date Retry-After; malformed values are ignored."""

    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    # Clamp only to the representable datetime range, not to an invented
    # product-policy ceiling: a provider's valid Retry-After remains honored.
    max_representable = max(
        0.0,
        (datetime.max.replace(tzinfo=timezone.utc) - current).total_seconds() - 1.0,
    )
    if seconds is not None:
        if not math.isfinite(seconds):
            return None
        return min(max_representable, max(0.0, seconds))
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    seconds_until = (target.astimezone(timezone.utc) - current).total_seconds()
    return min(max_representable, max(0.0, seconds_until))


def _backoff(
    attempt_count: int,
    policy: RetryPolicy,
    random_unit: Callable[[], float],
) -> float:
    exponent = max(0, int(attempt_count) - 1)
    if policy.base_delay_s == 0 or policy.max_delay_s == 0:
        base = 0.0
    else:
        ratio = max(1.0, policy.max_delay_s / policy.base_delay_s)
        cap_exponent = max(0, int(math.ceil(math.log2(ratio))))
        bounded_exponent = min(exponent, cap_exponent)
        base = min(
            policy.max_delay_s,
            policy.base_delay_s * (2**bounded_exponent),
        )
    unit = float(random_unit())
    if not math.isfinite(unit):
        unit = 0.5
    unit = min(1.0, max(0.0, unit))
    # Symmetric bounded jitter.  Injecting random_unit makes tests deterministic.
    multiplier = 1.0 + ((unit * 2.0) - 1.0) * policy.jitter_fraction
    return min(policy.max_delay_s, max(0.0, base * multiplier))


def decide_retry(
    failure: FailureRecord | None,
    *,
    page_class: str | None,
    attempt_count: int,
    max_attempts: int,
    headers: Mapping[str, str] | None = None,
    closure_is_success: bool = False,
    policy: RetryPolicy | None = None,
    random_unit: Callable[[], float] | None = None,
    now: datetime | None = None,
) -> RetryDecision:
    """Return a deterministic request transition for one execution result."""

    if closure_is_success:
        return RetryDecision(RetryAction.SUCCEED, reason="typed detail closure")

    effective_kind = failure.kind if failure is not None else None
    if page_class in _PAGE_RETRY_KIND:
        effective_kind = _PAGE_RETRY_KIND[page_class]
    elif page_class in _PAGE_FAIL_KIND:
        effective_kind = _PAGE_FAIL_KIND[page_class]

    if effective_kind is None:
        return RetryDecision(RetryAction.SUCCEED)
    if effective_kind is FailureKind.CANCELLED:
        return RetryDecision(RetryAction.FAIL, effective_kind.value, reason="cancelled")
    if effective_kind in {
        FailureKind.AUTH_REQUIRED,
        FailureKind.POLICY_REJECTED,
        FailureKind.BLOCKED,
        FailureKind.HTTP_4XX,
        FailureKind.PAGINATION_LOOP,
    }:
        return RetryDecision(
            RetryAction.FAIL,
            effective_kind.value,
            reason="non-retryable source/policy failure",
        )

    retryable = effective_kind in _TRANSIENT or bool(failure and failure.retryable)
    if not retryable:
        return RetryDecision(RetryAction.FAIL, effective_kind.value, reason="not retryable")

    chosen = policy or RetryPolicy()
    backoff = _backoff(
        attempt_count,
        chosen,
        random.random if random_unit is None else random_unit,
    )
    retry_after_raw = _header(headers or {}, "Retry-After")
    retry_after_s = parse_retry_after(retry_after_raw, now=now)
    delay = max(backoff, retry_after_s or 0.0)
    if int(attempt_count) >= int(max_attempts):
        # The request itself is terminal, but source-protection state is not:
        # preserve the computed finite cooldown for the durable rate circuit.
        return RetryDecision(
            RetryAction.FAIL,
            effective_kind.value,
            delay_s=delay,
            retry_after_raw=retry_after_raw,
            retry_after_s=retry_after_s,
            budget_exhausted=True,
            reason="attempt budget exhausted",
        )

    return RetryDecision(
        RetryAction.RETRY,
        effective_kind.value,
        delay_s=delay,
        retry_after_raw=retry_after_raw,
        retry_after_s=retry_after_s,
        reason="retryable failure within attempt budget",
    )


__all__ = [
    "RetryAction",
    "RetryDecision",
    "RetryPolicy",
    "decide_retry",
    "parse_retry_after",
]
