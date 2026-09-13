"""Durable continuation identity (Post-S3.13 Corrective A4).

Ordinary pagination state belongs to the exact ``RunSourcePlan``. A
continuation request must durably name the next logical target it will
actually execute, so a later claim can prove the durable target still
agrees with adapter planning before any network I/O.

This module is pure and network-inert: it builds continuation payloads
and validates claimed continuations. It performs no I/O.
"""

from __future__ import annotations

from typing import Mapping

from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.adapters.contract import CrawlCursor


class ContinuationIdentityError(RuntimeError):
    pass


def build_continuation_payload(
    *,
    request_type: str,
    next_cursor: CrawlCursor,
    next_target_identity: str,
) -> dict[str, object]:
    if request_type not in {"LIST_FETCH", "SOURCE_CRAWL"}:
        raise ContinuationIdentityError("unsupported continuation request type")
    if not isinstance(next_target_identity, str) or not next_target_identity:
        raise ContinuationIdentityError("continuation target identity is required")
    if not isinstance(next_cursor.state_json, str) or not next_cursor.state_json:
        raise ContinuationIdentityError("continuation cursor state is required")

    payload: dict[str, object] = {
        "cursor_state": next_cursor.state_json,
        "target_reference": next_target_identity,
    }
    if request_type == "SOURCE_CRAWL":
        payload["role"] = "PAGE"
    return payload


def assert_claimed_continuation_target(
    *,
    payload: Mapping[str, object],
    request_plan: RequestPlan,
) -> None:
    cursor_state = payload.get("cursor_state")
    if cursor_state is None:
        return  # seed/non-cursor request

    durable_target = payload.get("target_reference")
    if not isinstance(durable_target, str) or not durable_target:
        raise ContinuationIdentityError(
            "cursor continuation is missing durable target_reference"
        )
    if request_plan.url != durable_target:
        raise ContinuationIdentityError(
            "planned continuation URL disagrees with durable target_reference"
        )


__all__ = [
    "ContinuationIdentityError",
    "assert_claimed_continuation_target",
    "build_continuation_payload",
]
