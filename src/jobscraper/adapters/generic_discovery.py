"""Minimal built-in generic discovery planner (S2.8 corrective).

v0.3.1.3 acquisition §12.1 requires a durable SOURCE_DISCOVERY identity
before the first careers-page probe.  This adapter is that immutable planning
identity; it is deliberately *not* the ROAD-04 generic crawler.  It plans one
bounded GET to the operator-recorded Source entry URL and emits no jobs, child
work, frontier expansion or crawl authority.

The host owns destination policy, execution, persistence, fingerprinting and
routing.  This adapter is pure: no I/O and no database access.
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
    ParseOutcome,
    ParseOutcomeKind,
    ValidatedResult,
    validate_manifest,
)
from jobscraper.net.urlnorm import (
    UrlNormalizationError,
    has_embedded_credentials,
    normalize_url,
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
MAX_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 2_000_000
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_BODY_BYTES = 1_000_000


@dataclass(frozen=True)
class GenericDiscoveryConfig:
    """Pinned config for the one source-entry discovery probe."""

    entry_url: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_bytes: int = DEFAULT_MAX_BODY_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_url", _validated_entry_url(self.entry_url))
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)):
            raise ValueError("timeout_s must be a number")
        if not 0 < float(self.timeout_s) <= MAX_TIMEOUT_S:
            raise ValueError(f"timeout_s must be within (0, {MAX_TIMEOUT_S}]")
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int):
            raise ValueError("max_bytes must be an integer")
        if not 0 < self.max_bytes <= MAX_BODY_BYTES:
            raise ValueError(f"max_bytes must be within (0, {MAX_BODY_BYTES}]")


_CONFIG_FIELD_NAMES = frozenset(
    field.name for field in dataclasses.fields(GenericDiscoveryConfig)
)


def _validated_entry_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("entry_url must be a string")
    candidate = value.strip()
    if not candidate or has_embedded_credentials(candidate):
        raise ValueError("entry_url must be a credential-free http(s) URL")
    try:
        normalized = normalize_url(candidate, drop_fragment=False)
    except (UrlNormalizationError, ValueError) as exc:
        raise ValueError(f"entry_url is not usable: {exc}") from exc
    if normalized.scheme not in ("http", "https") or not normalized.host:
        raise ValueError("entry_url must be an http(s) URL with a host")
    return candidate


def _evidence_refs(result: ValidatedResult) -> tuple[str, ...]:
    refs: list[str] = []
    for ref in (result.result_envelope_ref, result.validation_evidence_ref):
        if ref and ref not in refs:
            refs.append(ref)
    return tuple(refs)


class GenericDiscoveryAdapter:
    """One bounded source-entry probe; generic crawl breadth remains deferred."""

    manifest = MANIFEST

    def __init__(self, config: GenericDiscoveryConfig):
        self.config = config

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "GenericDiscoveryAdapter":
        if not isinstance(config, Mapping):
            raise ValueError("generic_discovery binding config must be a mapping")
        unknown = sorted(str(key) for key in config if key not in _CONFIG_FIELD_NAMES)
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
            raise ValueError(
                f"generic_discovery supports DISCOVER only, not {task.kind.value}"
            )
        if cursor is not None:
            raise ValueError("generic_discovery does not accept a crawl cursor")
        return RequestPlan(
            method="GET",
            url=self.config.entry_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.8,*/*;q=0.1"
            },
            expected_content_types=("text/html", "application/xhtml+xml", "application/json"),
            timeout_s=float(self.config.timeout_s),
            max_bytes=self.config.max_bytes,
            purpose="SOURCE_DISCOVERY",
        )

    def parse(
        self,
        task: AdapterTask,
        result: ValidatedResult,
        ctx: Any = None,
    ) -> ParseOutcome:
        """Contract-complete no-observation parse surface.

        The dedicated host discovery seam owns fingerprinting/routing, so this
        method is intentionally network- and data-inert.  It exists so the
        built-in still satisfies the shared adapter protocol if exercised by a
        contract probe; it must never be interpreted as enumeration coverage.
        """
        if task.kind is not AdapterTaskKind.DISCOVER:
            raise ValueError(
                f"generic_discovery supports DISCOVER only, not {task.kind.value}"
            )
        return ParseOutcome(
            kind=ParseOutcomeKind.SUCCESS_EMPTY,
            review_evidence=(
                {
                    "reason": "DISCOVERY_PROBE_RECOGNIZED",
                    "absence_authority": False,
                },
            ),
            evidence_refs=_evidence_refs(result),
        )

    def next_cursor(
        self,
        task: AdapterTask,
        outcome: ParseOutcome,
        current_cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> CrawlCursor | None:
        return None


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TIMEOUT_S",
    "GenericDiscoveryAdapter",
    "GenericDiscoveryConfig",
    "MANIFEST",
    "MAX_BODY_BYTES",
    "MAX_TIMEOUT_S",
]
