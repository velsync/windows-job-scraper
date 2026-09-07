"""Service clock with wall-clock anomaly detection.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 50
(clock-anomaly lease invalidation).

Lease comparisons use a consistent UTC time source. If the service detects a
material backward/forward wall-clock anomaly beyond tolerance, it stops new
claims, invalidates current ownership under a fresh service epoch, records
the anomaly, and safely reclaims/retries rather than extending an already
elapsed lease.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from jobscraper.timeutil import to_rfc3339, utc_now

FORWARD_TOLERANCE_S = 3600.0  # 1h forward jump tolerance
BACKWARD_TOLERANCE_S = 120.0  # 2 min backward jump tolerance


class ClockAnomaly(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"clock anomaly ({kind}): {detail}")
        self.kind = kind
        self.detail = detail


class ServiceClock:
    """Tracks wall-clock continuity; detects material anomalies."""

    def __init__(self, *, forward_tolerance_s: float = FORWARD_TOLERANCE_S,
                 backward_tolerance_s: float = BACKWARD_TOLERANCE_S) -> None:
        self._lock = threading.Lock()
        self._max_seen = utc_now()
        self._epoch = 0
        self._anomalies: list[dict] = []
        self._forward_tolerance = forward_tolerance_s
        self._backward_tolerance = backward_tolerance_s

    def now(self):
        """Return current UTC time; raises ClockAnomaly on material jumps."""
        now = utc_now()
        with self._lock:
            delta = (now - self._max_seen).total_seconds()
            if delta < -self._backward_tolerance:
                self._record("BACKWARD", delta)
                raise ClockAnomaly("BACKWARD", f"wall clock moved back {delta:.1f}s")
            if delta > self._forward_tolerance:
                self._record("FORWARD", delta)
                raise ClockAnomaly("FORWARD", f"wall clock jumped forward {delta:.1f}s")
            if delta > 0:
                self._max_seen = now
            return now

    def now_s(self) -> str:
        return to_rfc3339(self.now())

    def _record(self, kind: str, delta: float) -> None:
        self._epoch += 1
        self._anomalies.append(
            {"kind": kind, "delta_seconds": delta, "epoch": self._epoch, "at": to_rfc3339(utc_now())}
        )
        del self._anomalies[:-50]

    def invalidate_epoch(self) -> int:
        """Explicitly advance the service epoch (invalidates in-memory ownership)."""
        with self._lock:
            self._epoch += 1
            return self._epoch

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def anomalies(self) -> list[dict]:
        with self._lock:
            return list(self._anomalies)

    def rebase(self) -> None:
        """After an anomaly is handled, rebase the tracked maximum."""
        with self._lock:
            self._max_seen = utc_now()
