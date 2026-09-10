"""Bounded host-driven source discovery planner (02 §12.1).

This adapter exists only to give the *first* careers-page probe an immutable,
manifest-validated binding identity.  It plans one read-only GET from the
durable SOURCE_DISCOVERY request payload.  It does not crawl careers pages or
extract jobs; generic HTML job discovery remains ROAD-04 work.

I/O, destination policy, fingerprinting, routing and evidence persistence are
host-owned.  The adapter receives no database handle and performs no network
access itself (ACQ-02/ACQ-08/ACQ-09).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Mapping

from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    validate_manifest,
)

MANIFEST = validate_manifest(
    {
        "id": "generic_discovery",
        "version": "1.0.0",
        "adapter_api_version": "1",
        "capabilities": ["discover"],
        "supported_execution_classes": ["HTTP"],
        "supported_auth_modes": ["NONE"],
        "cost_class": "LIGHT",
        "cursor_schema_version": 1,
    }
)

ADAPTER_ID = MANIFEST.id
ADAPTER_VERSION = MANIFEST.version


@dataclass(frozen=True)
class GenericDiscoveryConfig:
    timeout_s: float = 10.0
    max_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.timeout_s, (int, float)) or isinstance(self.timeout_s, bool):
            raise ValueError("generic_discovery timeout_s must be numeric")
        if not 0 < float(self.timeout_s) <= 30.0:
            raise ValueError("generic_discovery timeout_s must be > 0 and <= 30")
        if not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool):
            raise ValueError("generic_discovery max_bytes must be an integer")
        if not 1 <= self.max_bytes <= 2_000_000:
            raise ValueError("generic_discovery max_bytes must be 1..2000000")


_CONFIG_FIELDS = frozenset(field.name for field in dataclasses.fields(GenericDiscoveryConfig))


class GenericDiscoveryAdapter:
    """Pure one-probe planner for a durable SOURCE_DISCOVERY request."""

    manifest = MANIFEST
    listing_identity_sufficient = True

    def __init__(self, config: GenericDiscoveryConfig):
        self.config = config

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "GenericDiscoveryAdapter":
        if config is None:
            config = {}
        if not isinstance(config, Mapping):
            raise ValueError("generic_discovery binding config must be a mapping")
        unknown = sorted(str(key) for key in config if key not in _CONFIG_FIELDS)
        if unknown:
            raise ValueError(f"unknown generic_discovery binding config keys: {unknown}")
        return cls(GenericDiscoveryConfig(**dict(config)))

    def plan(
        self,
        task: AdapterTask,
        cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> RequestPlan:
        if task.kind is not AdapterTaskKind.DISCOVER:
            raise ValueError("generic_discovery only accepts DISCOVER tasks")
        if cursor is not None:
            raise ValueError("generic_discovery does not accept a crawl cursor")
        target = task.payload.get("target_reference") or task.payload.get("url")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("SOURCE_DISCOVERY requires a target_reference URL")
        return RequestPlan(
            method="GET",
            url=target.strip(),
            headers={"Accept": "text/html,application/xhtml+xml,application/json;q=0.8"},
            expected_content_types=("text/html", "application/xhtml+xml", "application/json"),
            timeout_s=float(self.config.timeout_s),
            max_bytes=self.config.max_bytes,
            purpose="SOURCE_DISCOVERY",
        )


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "GenericDiscoveryAdapter",
    "GenericDiscoveryConfig",
    "MANIFEST",
]
