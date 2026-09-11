"""Service-owned in-memory capacity coordinator (RUN-09, ACQ §14, S3.3).

Durable ownership remains in SQLite.  This module only accounts scarce live
execution slots inside the authoritative service process.  Executors receive
an already-authorized envelope; they never claim durable work and never touch
this coordinator directly.
"""

from __future__ import annotations

import enum
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from jobscraper.config import AppConfig

from jobscraper.acquisition.envelope import ExecutionPlanEnvelope


class ExecutionClass(enum.Enum):
    HTTP = "HTTP"
    BROWSER = "BROWSER"
    BROWSER_INTERACTIVE = "BROWSER_INTERACTIVE"


@dataclass(frozen=True)
class CapacityLimits:
    global_max: int = 16
    class_max: dict[str, int] = field(
        default_factory=lambda: {"HTTP": 16, "BROWSER": 1, "BROWSER_INTERACTIVE": 1}
    )
    per_source_max: int = 2
    per_host_max: int = 4
    per_egress_max: int | None = None

    @classmethod
    def from_config(cls, config: "AppConfig") -> "CapacityLimits":
        return cls(
            global_max=max(1, int(config.http_max_concurrency)
                           + int(config.browser_max_concurrency)
                           + int(config.browser_interactive_max_concurrency)),
            class_max={
                "HTTP": int(config.http_max_concurrency),
                "BROWSER": int(config.browser_max_concurrency),
                "BROWSER_INTERACTIVE": int(config.browser_interactive_max_concurrency),
            },
            per_source_max=int(config.source_max_concurrency),
            per_host_max=int(config.host_max_concurrency),
            per_egress_max=config.egress_max_concurrency,
        )

    def __post_init__(self) -> None:
        if self.global_max < 1:
            raise ValueError("global_max must be >= 1")
        if self.per_source_max < 1 or self.per_host_max < 1:
            raise ValueError("source/host capacity must be >= 1")
        if self.per_egress_max is not None and self.per_egress_max < 1:
            raise ValueError("per_egress_max must be >= 1 when configured")
        for name in (member.value for member in ExecutionClass):
            if int(self.class_max.get(name, 0)) < 1:
                raise ValueError(f"missing positive capacity for execution class {name}")


@dataclass(frozen=True)
class CapacityKey:
    execution_class: str
    source_id: str
    host: str
    egress_identity: str | None = None


class CapacityUnavailable(RuntimeError):
    def __init__(self, key: CapacityKey):
        self.key = key
        super().__init__(f"capacity unavailable for {key}")


class CapacityReservation:
    """Idempotently releasable service-owned slot reservation."""

    def __init__(self, coordinator: "CapacityCoordinator", key: CapacityKey):
        self._coordinator = coordinator
        self.key = key
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        # The same reservation can be observed by exception/cancellation
        # cleanup paths on different threads.  Make idempotence atomic rather
        # than relying on the GIL between the check and coordinator decrement.
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._coordinator._release(self.key)

    def __enter__(self) -> "CapacityReservation":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class CapacityCoordinator:
    """Atomic accounting across global/class/source/host/egress dimensions."""

    def __init__(self, limits: CapacityLimits | None = None):
        self.limits = limits or CapacityLimits()
        self._lock = threading.RLock()
        self._global = 0
        self._classes: dict[str, int] = {}
        self._sources: dict[str, int] = {}
        self._hosts: dict[str, int] = {}
        self._egress: dict[str, int] = {}

    def _would_fit(self, key: CapacityKey) -> bool:
        if key.execution_class not in {item.value for item in ExecutionClass}:
            return False
        if self._global >= self.limits.global_max:
            return False
        if self._classes.get(key.execution_class, 0) >= int(
            self.limits.class_max[key.execution_class]
        ):
            return False
        if self._sources.get(key.source_id, 0) >= self.limits.per_source_max:
            return False
        if self._hosts.get(key.host, 0) >= self.limits.per_host_max:
            return False
        if (
            key.egress_identity
            and self.limits.per_egress_max is not None
            and self._egress.get(key.egress_identity, 0) >= self.limits.per_egress_max
        ):
            return False
        return True

    def try_reserve(self, key: CapacityKey) -> CapacityReservation | None:
        with self._lock:
            if not self._would_fit(key):
                return None
            self._global += 1
            self._classes[key.execution_class] = self._classes.get(key.execution_class, 0) + 1
            self._sources[key.source_id] = self._sources.get(key.source_id, 0) + 1
            self._hosts[key.host] = self._hosts.get(key.host, 0) + 1
            if key.egress_identity:
                self._egress[key.egress_identity] = self._egress.get(key.egress_identity, 0) + 1
            return CapacityReservation(self, key)

    def reserve(self, key: CapacityKey) -> CapacityReservation:
        reservation = self.try_reserve(key)
        if reservation is None:
            raise CapacityUnavailable(key)
        return reservation

    @contextmanager
    def slot(self, key: CapacityKey):
        reservation = self.reserve(key)
        try:
            yield reservation
        finally:
            reservation.release()

    def _release(self, key: CapacityKey) -> None:
        with self._lock:
            if self._global <= 0:
                raise RuntimeError("capacity accounting underflow")
            self._global -= 1
            self._decrement(self._classes, key.execution_class)
            self._decrement(self._sources, key.source_id)
            self._decrement(self._hosts, key.host)
            if key.egress_identity:
                self._decrement(self._egress, key.egress_identity)

    @staticmethod
    def _decrement(bucket: dict[str, int], key: str) -> None:
        current = bucket.get(key, 0)
        if current <= 0:
            raise RuntimeError(f"capacity accounting underflow for {key!r}")
        if current == 1:
            bucket.pop(key, None)
        else:
            bucket[key] = current - 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "global": self._global,
                "classes": dict(self._classes),
                "sources": dict(self._sources),
                "hosts": dict(self._hosts),
                "egress": dict(self._egress),
            }


def capacity_key_for_envelope(envelope: ExecutionPlanEnvelope) -> CapacityKey:
    host = (urlsplit(envelope.payload.url).hostname or "").lower()
    if not host:
        raise ValueError("capacity accounting requires a concrete request host")
    return CapacityKey(
        execution_class=envelope.execution_class,
        source_id=envelope.source_id,
        host=host,
        # The envelope field is host-owned.  Imported/source-controlled data
        # cannot manufacture this identity (R2-F6).
        egress_identity=envelope.payload.egress_requirement,
    )


_SERVICE_COORDINATOR = CapacityCoordinator()
_SERVICE_LOCK = threading.RLock()


def configure_service_capacity(config: "AppConfig") -> CapacityCoordinator:
    """Install service-lifetime limits before dispatch begins."""
    global _SERVICE_COORDINATOR
    with _SERVICE_LOCK:
        if _SERVICE_COORDINATOR.snapshot()["global"] != 0:
            raise RuntimeError("cannot reconfigure capacity while slots are reserved")
        _SERVICE_COORDINATOR = CapacityCoordinator(CapacityLimits.from_config(config))
        return _SERVICE_COORDINATOR


def service_capacity_coordinator() -> CapacityCoordinator:
    """Process-lifetime coordinator owned by the service process."""
    with _SERVICE_LOCK:
        return _SERVICE_COORDINATOR


__all__ = [
    "CapacityCoordinator",
    "CapacityKey",
    "CapacityLimits",
    "CapacityReservation",
    "CapacityUnavailable",
    "ExecutionClass",
    "capacity_key_for_envelope",
    "configure_service_capacity",
    "service_capacity_coordinator",
]
