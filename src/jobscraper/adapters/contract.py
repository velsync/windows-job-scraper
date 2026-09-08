"""Adapter protocol and versioned data contracts (02 ACQ-02/03/04/09, §19/§22).

The parser receives ``ValidatedResult`` — never an unchecked raw response
(ACQ-02 parser validity gate). Adapters are pure: no database access, no
I/O; they propose requests and observations, the host validates and
executes.
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from jobscraper.acquisition.failures import FailureKind, FailureRecord
from jobscraper.acquisition.pagevalidity import PageClass
from jobscraper.acquisition.result import ResultEnvelope

#: Versioned cross-component data contracts (02 ACQ-09).  Slice 2 bumped this
#: additively: PlanningContext/ParseContext became concrete, and
#: ValidatedResult gained the §11.3/ACQ-09 reference fields plus a hard gate
#: that refuses an unvalidated page class.  Readers must accept every version
#: they claim.
CONTRACT_VERSION = 2
PARSE_CONTRACT_VERSION = CONTRACT_VERSION

_KNOWN_CAPABILITIES = frozenset(
    {
        "discover",
        "listing_parse",
        "detail_parse",
        "incremental",
        "health",
        "smoke",
    }
)
_EXECUTION_CLASSES = frozenset({"HTTP", "BROWSER", "BROWSER_INTERACTIVE"})
_AUTH_MODES = frozenset({"NONE", "SESSION", "API_KEY"})
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class AdapterManifest:
    id: str
    version: str
    adapter_api_version: str
    capabilities: tuple[str, ...]
    supported_execution_classes: tuple[str, ...]
    supported_auth_modes: tuple[str, ...]
    cost_class: str
    cursor_schema_version: int


def validate_manifest(data: Mapping[str, Any]) -> AdapterManifest:
    """Schema-validate an adapter manifest (typed rejections, no free-form)."""
    if not isinstance(data, Mapping):
        raise ValueError("manifest must be a mapping")
    missing = [
        k
        for k in (
            "id",
            "version",
            "adapter_api_version",
            "capabilities",
            "supported_execution_classes",
            "supported_auth_modes",
            "cost_class",
            "cursor_schema_version",
        )
        if k not in data
    ]
    if missing:
        raise ValueError(f"manifest missing required keys: {sorted(missing)}")
    ident = data["id"]
    if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9_]{2,64}", ident):
        raise ValueError(f"invalid adapter id: {ident!r}")
    version = data["version"]
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        raise ValueError(f"adapter version must be semver: {version!r}")
    capabilities = tuple(data["capabilities"])
    if not capabilities or not all(c in _KNOWN_CAPABILITIES for c in capabilities):
        raise ValueError(f"unknown capability in {capabilities}")
    classes = tuple(data["supported_execution_classes"])
    if not classes or not all(c in _EXECUTION_CLASSES for c in classes):
        raise ValueError(f"unknown execution class in {classes}")
    auth_modes = tuple(data["supported_auth_modes"])
    if not all(m in _AUTH_MODES for m in auth_modes):
        raise ValueError(f"unknown auth mode in {auth_modes}")
    cursor_version = data["cursor_schema_version"]
    if not isinstance(cursor_version, int) or cursor_version < 1:
        raise ValueError("cursor_schema_version must be a positive integer")
    return AdapterManifest(
        id=ident,
        version=version,
        adapter_api_version=str(data["adapter_api_version"]),
        capabilities=capabilities,
        supported_execution_classes=classes,
        supported_auth_modes=auth_modes,
        cost_class=str(data["cost_class"]),
        cursor_schema_version=cursor_version,
    )


class AdapterTaskKind(enum.Enum):
    HEALTH = "HEALTH"
    DISCOVER = "DISCOVER"
    ENUMERATE = "ENUMERATE"
    CRAWL = "CRAWL"
    DETAIL = "DETAIL"
    SMOKE = "SMOKE"


@dataclass(frozen=True)
class AdapterTask:
    kind: AdapterTaskKind
    payload: Mapping[str, Any] = field(default_factory=dict)
    query_id: str | None = None


#: Page classes a *normal* parser may be handed at all (02 ACQ-02 gate).
#: EMPTY is included because a recognized empty enumeration is a successful
#: outcome, not an invalid page; everything else is host-policy territory.
NORMAL_PARSE_CLASSES = frozenset(
    {PageClass.VALID_LIST, PageClass.VALID_JOB, PageClass.EMPTY}
)


@dataclass(frozen=True)
class ValidatedResultEnvelope:
    """The only parse input (02 ACQ-02 + ACQ-09 ``ValidatedResultEnvelope``).

    Constructing one is itself the validity gate: an invalid class raises, so
    a parser can never be handed login/challenge/rate-limited content and
    emit normal observations from it.
    """

    envelope: ResultEnvelope
    page_class: PageClass
    validation_evidence: Mapping[str, Any] = field(default_factory=dict)
    contract_version: int = PARSE_CONTRACT_VERSION
    security_policy_result: str = "ALLOWED"
    cache_representation_ref: str | None = None

    def __post_init__(self) -> None:
        if self.page_class not in NORMAL_PARSE_CLASSES:
            raise ValueError(
                f"{self.page_class.value} is not a valid class for normal "
                "observation extraction (02 ACQ-02); host policy/health/retry "
                "must handle it instead"
            )

    @property
    def validated_page_class(self) -> PageClass:
        """ACQ-09 field name for the class the validity gate produced."""
        return self.page_class

    @property
    def result_envelope_ref(self) -> str | None:
        """Reference to the durable result evidence (never the body itself)."""
        return self.envelope.body_ref

    @property
    def validation_evidence_ref(self) -> str | None:
        digest = self.envelope.normalized_content_hash or self.envelope.body_hash
        if digest is None:
            return None
        return f"validity://{self.page_class.value}/{digest[:16]}"


#: Slice-1 name for the same contract (kept so the accepted call sites and
#: fixtures remain valid).
ValidatedResult = ValidatedResultEnvelope


@dataclass(frozen=True)
class PlanningContext:
    """Host-owned planning input (02 ACQ-09): pins only, never mutable state.

    An adapter may read these values to shape a plan; every one of them is an
    immutable reference resolved by the host.
    """

    contract_version: int = PARSE_CONTRACT_VERSION
    run_id: str | None = None
    run_source_plan_id: str | None = None
    source_snapshot_ref: str | None = None
    binding_revision_id: str | None = None
    permission_profile_revision: int | None = None
    policy_snapshot_ref: str | None = None
    query_revision_ref: str | None = None
    budget_snapshot_ref: str | None = None
    cursor_schema_version: int = 1


@dataclass(frozen=True)
class ParseContext:
    """Host-owned parse input (02 ACQ-09) with the request-scoped
    idempotency namespace the parser must key deterministic child work on."""

    contract_version: int = PARSE_CONTRACT_VERSION
    request_id: str | None = None
    attempt_id: str | None = None
    run_source_plan_id: str | None = None
    parser_version: str | None = None
    normalization_version: str | None = None
    idempotency_namespace: str | None = None


@dataclass(frozen=True)
class CrawlCursor:
    source_id: str
    binding_id: str
    adapter_id: str
    adapter_version: str
    cursor_schema_version: int
    state_json: str
    checkpoint_at: str


@dataclass(frozen=True)
class StopPolicy:
    """Declared stop policy (02 §19). Bounded by construction."""

    max_pages: int = 50
    max_consecutive_empty_pages: int = 2
    max_consecutive_no_new_jobs_pages: int = 5
    max_duplicate_pages: int = 3
    max_runtime_s: float = 600.0
    max_requests: int = 200


class ParseOutcomeKind(enum.Enum):
    SUCCESS_WITH_JOBS = "SUCCESS_WITH_JOBS"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    PARTIAL = "PARTIAL"
    FAILURE = "FAILURE"


@dataclass(frozen=True)
class FieldEvidenceRecord:
    """One extracted field with its locator and span (03 §30 field evidence)."""

    field_name: str
    locator_kind: str
    locator_value: str
    value_hash: str
    excerpt: str = ""
    evidence_start: int | None = None
    evidence_end: int | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class ObservationRecord:
    """An adapter's proposal for one immutable JobObservation (§29).

    The host pipeline — not the adapter — persists this under the request
    fence with a deterministic observation_unique_key.
    """

    source_job_id: str | None
    raw_url: str | None
    canonical_url_candidate: str | None
    application_url_candidate: str | None
    fields: dict
    field_evidence: tuple[FieldEvidenceRecord, ...] = ()
    source_rank_or_order: int | None = None
    page_cursor_json: str | None = None


@dataclass(frozen=True)
class DiscoveredTask:
    """Typed child task (ACQ-04): a discovered reference is not I/O permission."""

    kind: str  # DETAIL | ENUMERATE | CRAWL
    logical_key: str
    target_reference: str
    priority: int = 0
    depth: int = 0


@dataclass(frozen=True)
class ParseOutcome:
    contract_version: int = CONTRACT_VERSION
    kind: ParseOutcomeKind = ParseOutcomeKind.SUCCESS_EMPTY
    observations: tuple[ObservationRecord, ...] = ()
    discovered_tasks: tuple[DiscoveredTask, ...] = ()
    review_evidence: tuple[dict, ...] = ()
    cursor_proposal: CrawlCursor | None = None
    coverage_proposal: dict | None = None
    continuation_required: bool = False
    failure: FailureRecord | None = None
    #: typed closure/missing evidence (02 ACQ-02/ACQ-09): a parser reports
    #: "closed"/"not found" through this channel, never as a fabricated
    #: normal observation.
    closure_or_missing_evidence: tuple[dict, ...] = ()
    #: references to the durable evidence rows supporting this outcome
    evidence_refs: tuple[str, ...] = ()


# ------------------------------------------------------- loop/trap protection


@dataclass(frozen=True)
class PageSignature:
    cursor_state: str
    url: str
    page_body_hash: str
    job_ids: frozenset


class PaginationTracker:
    """Loop/trap protection (02 §19): repeated next cursor, repeated next URL,
    repeated normalized page hash, repeated same job set."""

    def __init__(self, *, max_duplicate_pages: int = 1):
        self._seen_cursors: set[str] = set()
        self._seen_urls: set[str] = set()
        self._seen_hashes: set[str] = set()
        self._seen_job_sets: set[frozenset] = set()
        self._max_duplicate_pages = max_duplicate_pages

    def record(self, signature: PageSignature) -> None:
        self._seen_cursors.add(signature.cursor_state)
        self._seen_urls.add(signature.url)
        self._seen_hashes.add(signature.page_body_hash)
        self._seen_job_sets.add(signature.job_ids)

    def is_trap(self, signature: PageSignature) -> bool:
        return (
            signature.cursor_state in self._seen_cursors
            or signature.url in self._seen_urls
            or signature.page_body_hash in self._seen_hashes
            or signature.job_ids in self._seen_job_sets
        )


def value_hash(value: Any) -> str:
    encoded = json_dumps(value)
    return hashlib.sha256(encoded.encode()).hexdigest()


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "CONTRACT_VERSION",
    "NORMAL_PARSE_CLASSES",
    "PARSE_CONTRACT_VERSION",
    "AdapterManifest",
    "AdapterTask",
    "AdapterTaskKind",
    "CONTRACT_VERSION",
    "CrawlCursor",
    "DiscoveredTask",
    "FieldEvidenceRecord",
    "ObservationRecord",
    "PageSignature",
    "PaginationTracker",
    "ParseContext",
    "ParseOutcome",
    "ParseOutcomeKind",
    "PlanningContext",
    "StopPolicy",
    "ValidatedResult",
    "ValidatedResultEnvelope",
    "validate_manifest",
    "value_hash",
]
