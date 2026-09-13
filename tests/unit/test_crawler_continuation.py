"""A4 continuation identity unit tests (Post-S3.13 Corrective A4)."""

from __future__ import annotations

import json

import pytest

from jobscraper.acquisition.crawler.continuation import (
    ContinuationIdentityError,
    assert_claimed_continuation_target,
    build_continuation_payload,
)
from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.adapters.contract import AdapterTask, AdapterTaskKind, CrawlCursor
from jobscraper.adapters.feed_api import FeedApiAdapter
from jobscraper.runtime.requests import request_unique_key


def _cursor(page: int) -> CrawlCursor:
    return CrawlCursor(
        source_id="src",
        binding_id="bnd",
        adapter_id="json_api_feed",
        adapter_version="1.0.0",
        cursor_schema_version=1,
        state_json=json.dumps({"page": page}, sort_keys=True),
        checkpoint_at="2026-09-13T00:00:00.000000Z",
    )


def _plan(url: str) -> RequestPlan:
    return RequestPlan(method="GET", url=url)


def _feed_adapter() -> FeedApiAdapter:
    return FeedApiAdapter.from_config(
        {
            "url_template": "http://127.0.0.1:8765/jobs?page={page}",
            "items_path": "jobs",
            "fields": {"source_job_id": {"path": "id", "required": True}},
        }
    )


def test_continuation_request_target_identity_matches_next_planned_target():
    adapter = _feed_adapter()
    task = AdapterTask(kind=AdapterTaskKind.ENUMERATE, payload={})
    planning_ctx = None

    page1_plan = adapter.plan(task, None, ctx=planning_ctx)
    assert page1_plan.url == "http://127.0.0.1:8765/jobs?page=1"

    next_cursor = _cursor(2)
    next_plan = adapter.plan(task, next_cursor, ctx=planning_ctx)
    assert next_plan.url == "http://127.0.0.1:8765/jobs?page=2"

    payload = build_continuation_payload(
        request_type="LIST_FETCH",
        next_cursor=next_cursor,
        next_target_identity=next_plan.url,
    )
    # Durable target is the next planned target, not the just-completed page.
    assert payload["target_reference"] == "http://127.0.0.1:8765/jobs?page=2"
    assert payload["target_reference"] != page1_plan.url
    assert payload["cursor_state"] == next_cursor.state_json

    # request_unique_key binds the next target + next cursor state.
    key_next = request_unique_key(
        run_source_plan_id="plan",
        request_type="LIST_FETCH",
        target_identity=next_plan.url,
        strategy="HTTP_JSON",
        logical_key=next_cursor.state_json,
    )
    key_old_target = request_unique_key(
        run_source_plan_id="plan",
        request_type="LIST_FETCH",
        target_identity=page1_plan.url,
        strategy="HTTP_JSON",
        logical_key=next_cursor.state_json,
    )
    key_old_cursor = request_unique_key(
        run_source_plan_id="plan",
        request_type="LIST_FETCH",
        target_identity=next_plan.url,
        strategy="HTTP_JSON",
        logical_key=_cursor(1).state_json,
    )
    assert key_next != key_old_target
    assert key_next != key_old_cursor


def test_claim_fails_closed_when_durable_target_disagrees_with_planned_target():
    # Durable payload says page 2, but planning under the same cursor
    # produces page 3 (or another URL).
    next_cursor = _cursor(3)
    payload = {
        "cursor_state": next_cursor.state_json,
        "target_reference": "http://127.0.0.1:8765/jobs?page=2",
    }
    planned = _plan("http://127.0.0.1:8765/jobs?page=3")
    with pytest.raises(ContinuationIdentityError, match="disagrees"):
        assert_claimed_continuation_target(payload=payload, request_plan=planned)


def test_cursor_continuation_without_target_reference_is_refused():
    payload = {"cursor_state": _cursor(2).state_json}
    with pytest.raises(ContinuationIdentityError, match="target_reference"):
        assert_claimed_continuation_target(
            payload=payload, request_plan=_plan("http://127.0.0.1:8765/jobs?page=2")
        )


def test_seed_request_without_cursor_state_is_not_reinterpreted():
    # Seed requests carry no cursor_state and must remain valid.
    assert_claimed_continuation_target(payload={}, request_plan=_plan("http://127.0.0.1:8765/jobs?page=1"))
    assert_claimed_continuation_target(
        payload={"target_reference": "http://127.0.0.1:8765/jobs?page=1"},
        request_plan=_plan("http://127.0.0.1:8765/jobs?page=1"),
    )
    assert_claimed_continuation_target(
        payload={"role": "PAGE", "target_reference": "http://127.0.0.1:8765/p1"},
        request_plan=_plan("http://127.0.0.1:8765/p1"),
    )


def test_list_fetch_continuation_carries_cursor_state_and_target():
    cursor = _cursor(2)
    payload = build_continuation_payload(
        request_type="LIST_FETCH",
        next_cursor=cursor,
        next_target_identity="http://127.0.0.1:8765/jobs?page=2",
    )
    assert payload["cursor_state"] == cursor.state_json
    assert payload["target_reference"] == "http://127.0.0.1:8765/jobs?page=2"
    assert "role" not in payload
    # Matching planned target passes claim validation.
    assert_claimed_continuation_target(
        payload=payload, request_plan=_plan("http://127.0.0.1:8765/jobs?page=2")
    )


def test_source_crawl_continuation_carries_page_role_cursor_and_target():
    cursor = CrawlCursor(
        source_id="src",
        binding_id="bnd",
        adapter_id="fixture_crawler",
        adapter_version="1.0.0",
        cursor_schema_version=1,
        state_json=json.dumps({"url": "http://127.0.0.1:8765/p2"}, sort_keys=True),
        checkpoint_at="2026-09-13T00:00:00.000000Z",
    )
    payload = build_continuation_payload(
        request_type="SOURCE_CRAWL",
        next_cursor=cursor,
        next_target_identity="http://127.0.0.1:8765/p2",
    )
    assert payload["role"] == "PAGE"
    assert payload["cursor_state"] == cursor.state_json
    assert payload["target_reference"] == "http://127.0.0.1:8765/p2"
    assert_claimed_continuation_target(
        payload=payload, request_plan=_plan("http://127.0.0.1:8765/p2")
    )
