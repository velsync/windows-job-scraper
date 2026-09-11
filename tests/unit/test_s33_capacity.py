"""S3.3 capacity coordinator contract tests.  GENERATED ONLY; not executed."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from jobscraper.acquisition.envelope import ExecutionPlanEnvelope, RequestPlan
from jobscraper.runtime.dispatch import UnsupportedExecutionClass, dispatch_http

from jobscraper.runtime.capacity import (
    CapacityCoordinator,
    CapacityKey,
    CapacityLimits,
    CapacityUnavailable,
)


def test_execution_classes_have_independent_caps():
    coordinator = CapacityCoordinator(
        CapacityLimits(
            global_max=4,
            class_max={"HTTP": 2, "BROWSER": 1, "BROWSER_INTERACTIVE": 1},
            per_source_max=4,
            per_host_max=4,
        )
    )
    http = coordinator.reserve(CapacityKey("HTTP", "src-a", "a.example"))
    browser = coordinator.reserve(CapacityKey("BROWSER", "src-b", "b.example"))
    with pytest.raises(CapacityUnavailable):
        coordinator.reserve(CapacityKey("BROWSER", "src-c", "c.example"))
    browser.release()
    http.release()
    assert coordinator.snapshot()["global"] == 0


def test_per_host_and_per_source_caps_are_atomic_under_concurrency():
    coordinator = CapacityCoordinator(
        CapacityLimits(
            global_max=8,
            class_max={"HTTP": 8, "BROWSER": 1, "BROWSER_INTERACTIVE": 1},
            per_source_max=2,
            per_host_max=2,
        )
    )
    gate = threading.Barrier(3)
    release = threading.Event()

    def worker(i: int) -> bool:
        reservation = coordinator.try_reserve(CapacityKey("HTTP", "src", "h.example"))
        if reservation is None:
            return False
        try:
            gate.wait(timeout=2)
            release.wait(timeout=2)
            return True
        finally:
            reservation.release()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(worker, i) for i in range(3)]
        # Exactly two can reach the barrier.  The third returns False instead
        # of overcommitting.  Main thread is the third barrier participant.
        gate.wait(timeout=2)
        assert coordinator.snapshot()["global"] == 2
        release.set()
        results = [f.result(timeout=2) for f in futures]
    assert results.count(True) == 2
    assert results.count(False) == 1
    assert coordinator.snapshot()["global"] == 0


def test_release_is_exception_safe_and_idempotent():
    coordinator = CapacityCoordinator()
    reservation = coordinator.reserve(CapacityKey("HTTP", "src", "h.example"))
    reservation.release()
    reservation.release()
    assert coordinator.snapshot()["global"] == 0


def test_browser_class_never_enters_http_dispatcher():
    envelope = ExecutionPlanEnvelope(
        plan_id="p", request_id="r", attempt_id="a", run_id="run",
        run_source_plan_id="rsp", source_id="src", binding_id="bnd",
        binding_revision_id="br", adapter_id="adapter", adapter_version="1",
        strategy="PLAYWRIGHT_PUBLIC", execution_class="BROWSER",
        policy_snapshot_ref="policy", permission_profile_id="perm",
        permission_profile_revision=1, payload_kind="REQUEST",
        payload=RequestPlan("GET", "https://example.test/jobs", purpose="LIST_FETCH"),
    )
    with pytest.raises(UnsupportedExecutionClass):
        dispatch_http(
            None, envelope, None, now="2026-09-11T08:00:00.000000Z",
            attempt_count=1, max_attempts=3, expect="LIST",
        )


def test_same_reservation_concurrent_release_is_atomic():
    coordinator = CapacityCoordinator()
    reservation = coordinator.reserve(CapacityKey("HTTP", "src", "h.example"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _i: reservation.release(), range(16)))
    assert coordinator.snapshot()["global"] == 0
