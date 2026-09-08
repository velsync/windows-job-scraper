"""Declarative JSON API/feed adapter (S1.5).

One simple, reliable adapter configured entirely by binding-revision
``config_json`` (URL template, item JSON path, field mappings). Suitable
for public JSON jobs feeds and the acceptance fixture server.

Discipline (02 §22): required-field failure never invents values — the
item is rejected with structured review evidence. A page whose items all
fail is ``PARSE_MARKER_MISSING``, never ``SUCCESS_EMPTY``.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from jobscraper.adapters.contract import (
    AdapterTask,
    CrawlCursor,
    FieldEvidenceRecord,
    ObservationRecord,
    ParseOutcome,
    ParseOutcomeKind,
    ValidatedResult,
    validate_manifest,
    value_hash,
)
from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.acquisition.failures import FailureKind, FailureRecord

MANIFEST = validate_manifest(
    {
        "id": "json_api_feed",
        "version": "1.0.0",
        "adapter_api_version": "1",
        "capabilities": ["listing_parse", "incremental", "health", "smoke"],
        "supported_execution_classes": ["HTTP"],
        "supported_auth_modes": ["NONE"],
        "cost_class": "LIGHT",
        "cursor_schema_version": 1,
    }
)

ADAPTER_ID = MANIFEST.id
ADAPTER_VERSION = MANIFEST.version


@dataclass(frozen=True)
class FeedApiConfig:
    url_template: str
    items_path: str = "jobs"
    fields: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    start_page: int = 1
    terminal_when: str = "empty_items"
    page_size_hint: int | None = None
    timeout_s: float = 30.0
    max_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if "{page}" not in self.url_template:
            raise ValueError("url_template must contain {page}")
        if self.terminal_when not in ("empty_items",):
            raise ValueError(f"unsupported terminal condition: {self.terminal_when!r}")
        if not self.fields:
            raise ValueError("fields mapping is required")


#: Accepted ``config_json`` members (an unknown key is refused, never ignored).
_CONFIG_FIELD_NAMES = frozenset(
    config_field.name for config_field in dataclasses.fields(FeedApiConfig)
)


def _dig(payload: Any, dotted_path: str) -> tuple[bool, Any]:
    node = payload
    for part in dotted_path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return False, None
    return True, node


class FeedApiAdapter:
    """plan / parse / next_cursor over a declarative JSON feed config."""

    manifest = MANIFEST

    #: 03 §40 coverage barrier: this binding contract proves listing presence
    #: from the enumeration alone (it emits no detail child work), so detail
    #: completion is not part of the absence-authority barrier.
    listing_identity_sufficient = True

    def __init__(self, config: FeedApiConfig):
        self.config = config

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "FeedApiAdapter":
        """Registry construction from a binding-revision ``config_json``.

        Unknown keys are refused rather than ignored, so a config this adapter
        does not understand cannot run with silently different semantics.
        """
        if not isinstance(config, Mapping):
            raise ValueError("json_api_feed binding config must be a mapping")
        unknown = sorted(
            str(key)
            for key in config
            if key not in _CONFIG_FIELD_NAMES
        )
        if unknown:
            raise ValueError(f"unknown json_api_feed binding config keys: {unknown}")
        return cls(FeedApiConfig(**dict(config)))

    # ------------------------------------------------------------------ plan

    def plan(
        self,
        task: AdapterTask,
        cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> RequestPlan:
        page = self._current_page(cursor)
        url = self.config.url_template.format(page=page)
        return RequestPlan(
            method="GET",
            url=url,
            headers={"Accept": "application/json"},
            expected_content_types=("application/json",),
            timeout_s=self.config.timeout_s,
            max_bytes=self.config.max_bytes,
            purpose=task.kind.value,
        )

    def _current_page(self, cursor: CrawlCursor | None) -> int:
        if cursor is None:
            return self.config.start_page
        state = cursor.state_json
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except ValueError:
                return self.config.start_page
        if not isinstance(state, Mapping):
            return self.config.start_page
        try:
            return int(state.get("page", self.config.start_page))
        except (TypeError, ValueError):
            return self.config.start_page

    # ----------------------------------------------------------------- parse

    def parse(
        self,
        task: AdapterTask,
        result: ValidatedResult,
        ctx: Any = None,
    ) -> ParseOutcome:
        envelope = result.envelope
        try:
            payload = json.loads(envelope.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._failure(envelope, FailureKind.PARSE_MARKER_MISSING,
                                 f"body is not valid JSON: {exc}")
        found, items = _dig(payload, self.config.items_path)
        if not found or not isinstance(items, list):
            return self._failure(
                envelope, FailureKind.PARSE_MARKER_MISSING,
                f"items path {self.config.items_path!r} not found or not a list",
            )
        if not items:
            return ParseOutcome(kind=ParseOutcomeKind.SUCCESS_EMPTY)

        observations: list[ObservationRecord] = []
        review: list[dict] = []
        for order, item in enumerate(items):
            record, item_review = self._observation(item, order, envelope)
            if record is not None:
                observations.append(record)
            if item_review is not None:
                review.append(item_review)

        if not observations:
            return self._failure(
                envelope, FailureKind.PARSE_MARKER_MISSING,
                f"all {len(items)} items failed required-field validation",
                review=review,
            )
        return ParseOutcome(
            kind=ParseOutcomeKind.SUCCESS_WITH_JOBS,
            observations=tuple(observations),
            review_evidence=tuple(review),
        )

    def _observation(
        self, item: Any, order: int, envelope
    ) -> tuple[ObservationRecord | None, dict | None]:
        if not isinstance(item, Mapping):
            return None, {"reason": "item is not an object", "order": order}
        fields: dict = {}
        evidence: list[FieldEvidenceRecord] = []
        rejected_for: str | None = None
        for field_name, spec in self.config.fields.items():
            path = spec.get("path", field_name)
            found, value = _dig(item, path)
            if not found or value is None:
                if spec.get("required"):
                    if rejected_for is None:
                        rejected_for = f"missing required field {field_name!r} at {path!r}"
                    continue
                continue
            if spec.get("many") and isinstance(value, list):
                fields[field_name] = [str(v) for v in value]
            else:
                fields[field_name] = str(value)
            evidence.append(
                FieldEvidenceRecord(
                    field_name=field_name,
                    locator_kind="json_path",
                    locator_value=path,
                    value_hash=value_hash(fields[field_name]),
                    excerpt=str(fields[field_name])[:200],
                )
            )
        if rejected_for is not None:
            return None, {
                "reason": rejected_for,
                "order": order,
                "item_keys": sorted(str(k) for k in item.keys())[:20],
            }
        return (
            ObservationRecord(
                source_job_id=fields.get("source_job_id") or None,
                raw_url=envelope.final_url,
                canonical_url_candidate=fields.get("job_url") or None,
                application_url_candidate=fields.get("apply_url") or None,
                fields=fields,
                field_evidence=tuple(evidence),
                source_rank_or_order=order,
            ),
            None,
        )

    def _failure(
        self, envelope, kind: FailureKind, detail: str, *, review=()
    ) -> ParseOutcome:
        return ParseOutcome(
            kind=ParseOutcomeKind.FAILURE,
            review_evidence=tuple(review),
            failure=FailureRecord(
                kind=kind,
                retryable=kind in (FailureKind.PARSE_EMPTY, FailureKind.TIMEOUT),
                source_health_impact="DEGRADED",
                source_id=envelope.source_id,
                binding_id=envelope.binding_id,
                adapter_id=envelope.adapter_id,
                adapter_version=envelope.adapter_version,
                run_id=None,
                request_id=envelope.request_id,
                attempt_id=envelope.attempt_id,
                details_redacted={"detail": detail[:500]},
            ),
        )

    # ------------------------------------------------------------ next cursor

    def next_cursor(
        self,
        task: AdapterTask,
        outcome: ParseOutcome,
        current_cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> CrawlCursor | None:
        if outcome.kind == ParseOutcomeKind.SUCCESS_EMPTY:
            return None  # recognized terminal: the feed ended
        if outcome.kind == ParseOutcomeKind.FAILURE:
            return None  # failures never advance the cursor
        page = self._current_page(current_cursor) + 1
        return CrawlCursor(
            source_id=current_cursor.source_id if current_cursor else "",
            binding_id=current_cursor.binding_id if current_cursor else "",
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            cursor_schema_version=MANIFEST.cursor_schema_version,
            state_json=json.dumps({"page": page}, sort_keys=True),
            checkpoint_at="",
        )


__all__ = ["ADAPTER_ID", "ADAPTER_VERSION", "FeedApiAdapter", "FeedApiConfig", "MANIFEST"]
