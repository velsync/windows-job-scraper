"""Typed acquisition contracts.

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md
(ACQ-02 adapter protocol, ACQ-03 parse outcome model, ACQ-04 detail-child
planning, ACQ-09 versioned cross-component data contracts, section 11
envelopes/plans, section 21 page validity, section 27 typed failure model).

These dataclasses are the versioned cross-component contract set; contract
fixtures live in tests/contract/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ContractVersioned:
    contract_version: str = "1"


# --------------------------------------------------------------------- enums
class Strategy(str, Enum):
    PROVIDER_NATIVE = "PROVIDER_NATIVE"
    FEED_OR_PUBLIC_STRUCTURED_ENDPOINT = "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT"
    STRUCTURED_PAGE = "STRUCTURED_PAGE"
    HTTP_HTML = "HTTP_HTML"
    PLAYWRIGHT_PUBLIC = "PLAYWRIGHT_PUBLIC"
    PLAYWRIGHT_AUTHENTICATED = "PLAYWRIGHT_AUTHENTICATED"
    MANUAL_UNSUPPORTED = "MANUAL_UNSUPPORTED"


STRATEGY_ORDER = [
    Strategy.PROVIDER_NATIVE,
    Strategy.FEED_OR_PUBLIC_STRUCTURED_ENDPOINT,
    Strategy.STRUCTURED_PAGE,
    Strategy.HTTP_HTML,
    Strategy.PLAYWRIGHT_PUBLIC,
    Strategy.PLAYWRIGHT_AUTHENTICATED,
    Strategy.MANUAL_UNSUPPORTED,
]


class ExecutionClass(str, Enum):
    HTTP = "HTTP"
    BROWSER = "BROWSER"
    BROWSER_INTERACTIVE = "BROWSER_INTERACTIVE"
    HOST_NATIVE = "HOST_NATIVE"  # non-network pipeline tasks


class PageClass(str, Enum):
    VALID_LIST = "VALID_LIST"
    VALID_JOB = "VALID_JOB"
    JS_SHELL = "JS_SHELL"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    CHALLENGE_PAGE = "CHALLENGE_PAGE"
    EMPTY = "EMPTY"
    NOT_FOUND = "NOT_FOUND"
    JOB_CLOSED = "JOB_CLOSED"
    UNEXPECTED_REDIRECT = "UNEXPECTED_REDIRECT"
    UNEXPECTED_CONTENT = "UNEXPECTED_CONTENT"
    UNKNOWN = "UNKNOWN"


INVALID_PAGE_CLASSES = {
    PageClass.JS_SHELL,
    PageClass.LOGIN_REQUIRED,
    PageClass.AUTH_EXPIRED,
    PageClass.RATE_LIMITED,
    PageClass.CHALLENGE_PAGE,
    PageClass.UNEXPECTED_REDIRECT,
    PageClass.UNEXPECTED_CONTENT,
    PageClass.UNKNOWN,
}


class FailureKind(str, Enum):
    DNS_ERROR = "DNS_ERROR"
    CONNECT_ERROR = "CONNECT_ERROR"
    TLS_ERROR = "TLS_ERROR"
    TIMEOUT = "TIMEOUT"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    RATE_LIMIT = "RATE_LIMIT"
    BLOCKED = "BLOCKED"
    CHALLENGE = "CHALLENGE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CONSENT_INTERSTITIAL = "CONSENT_INTERSTITIAL"
    PARSE_MARKER_MISSING = "PARSE_MARKER_MISSING"
    PARSE_EMPTY = "PARSE_EMPTY"
    PAGINATION_LOOP = "PAGINATION_LOOP"
    DUPLICATE_PAGE = "DUPLICATE_PAGE"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    INVALID_JOB_RECORD = "INVALID_JOB_RECORD"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"
    BROWSER_CRASH = "BROWSER_CRASH"
    WORKER_CRASH = "WORKER_CRASH"
    LEASE_LOST = "LEASE_LOST"
    CANCELLED = "CANCELLED"
    POLICY_REJECTED = "POLICY_REJECTED"
    NO_BROWSER_RUNTIME = "NO_BROWSER_RUNTIME"
    UNSUPPORTED = "UNSUPPORTED"


class RequestType(str, Enum):
    SOURCE_HEALTH_CHECK = "SOURCE_HEALTH_CHECK"
    SOURCE_DISCOVERY = "SOURCE_DISCOVERY"
    LIST_FETCH = "LIST_FETCH"
    DETAIL_FETCH = "DETAIL_FETCH"
    SOURCE_CRAWL = "SOURCE_CRAWL"
    NORMALIZE = "NORMALIZE"
    RECONCILE = "RECONCILE"
    ENRICH = "ENRICH"
    ELIGIBILITY = "ELIGIBILITY"
    SCORE = "SCORE"
    ADAPTER_SMOKE = "ADAPTER_SMOKE"
    EXPORT = "EXPORT"


class AdapterTaskKind(str, Enum):
    HEALTH = "HEALTH"
    DISCOVER = "DISCOVER"
    ENUMERATE = "ENUMERATE"
    CRAWL = "CRAWL"
    DETAIL = "DETAIL"
    SMOKE = "SMOKE"


REQUEST_TYPE_TO_TASK = {
    RequestType.SOURCE_HEALTH_CHECK: AdapterTaskKind.HEALTH,
    RequestType.SOURCE_DISCOVERY: AdapterTaskKind.DISCOVER,
    RequestType.LIST_FETCH: AdapterTaskKind.ENUMERATE,
    RequestType.SOURCE_CRAWL: AdapterTaskKind.CRAWL,
    RequestType.DETAIL_FETCH: AdapterTaskKind.DETAIL,
    RequestType.ADAPTER_SMOKE: AdapterTaskKind.SMOKE,
}


class ParseOutcomeKind(str, Enum):
    SUCCESS_WITH_JOBS = "SUCCESS_WITH_JOBS"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    PARTIAL = "PARTIAL"
    FAILURE = "FAILURE"


class CoverageAuthority(str, Enum):
    AUTHORITATIVE_FULL_SOURCE = "AUTHORITATIVE_FULL_SOURCE"
    AUTHORITATIVE_DECLARED_SCOPE = "AUTHORITATIVE_DECLARED_SCOPE"
    NON_AUTHORITATIVE_QUERY = "NON_AUTHORITATIVE_QUERY"
    DETAIL_ONLY = "DETAIL_ONLY"
    NO_ABSENCE_INFERENCE = "NO_ABSENCE_INFERENCE"


class CompletionState(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class DiscoveredTaskKind(str, Enum):
    DETAIL = "DETAIL"
    ENUMERATE = "ENUMERATE"
    CRAWL = "CRAWL"


class OperationalState(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    CHALLENGED = "CHALLENGED"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    BROKEN = "BROKEN"


HEALTH_DIMENSIONS = (
    "CONNECTIVITY",
    "AUTH",
    "CONTENT_VALIDITY",
    "NAVIGATION",
    "EXTRACTION",
    "RATE_LIMIT",
    "DATA_QUALITY",
)


# ------------------------------------------------------------------ manifests
@dataclass(frozen=True)
class AdapterManifest:
    """Schema-validated adapter identity and capabilities (module 02 section 9)."""

    id: str
    version: str
    adapter_api_version: str
    capabilities: tuple[str, ...]
    supported_execution_classes: tuple[str, ...]
    supported_auth_modes: tuple[str, ...]
    cost_class: str = "LIGHT"
    cursor_schema_version: int = 1
    supports_absence_authority: bool = False
    listing_identity_sufficient: bool = True

    def supports(self, execution_class: str) -> bool:
        return execution_class in self.supported_execution_classes

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "adapter_api_version": self.adapter_api_version,
            "capabilities": list(self.capabilities),
            "supported_execution_classes": list(self.supported_execution_classes),
            "supported_auth_modes": list(self.supported_auth_modes),
            "cost_class": self.cost_class,
            "cursor_schema_version": self.cursor_schema_version,
            "supports_absence_authority": self.supports_absence_authority,
            "listing_identity_sufficient": self.listing_identity_sufficient,
        }


@dataclass(frozen=True)
class PermissionProfileSnapshot:
    """Immutable permission-profile revision content (module 02 section 9.1)."""

    permission_profile_id: str
    revision: str
    network_hosts: tuple[str, ...] = ()
    authenticated_session_access: bool = False
    browser_capability: tuple[str, ...] = ()
    snapshot_read: bool = False
    fixture_read: bool = False
    egress_modes: tuple[str, ...] = ("DIRECT",)
    filesystem_access: str = "DENIED"
    subprocess_access: str = "DENIED"

    def host_allowed(self, host: str) -> bool:
        from jobscraper.security.netpolicy import normalize_host_for_lookup

        host = normalize_host_for_lookup(host)
        if "*" in self.network_hosts:
            return True
        if host in self.network_hosts:
            return True
        # Suffix wildcards like *.example.com are not supported by design:
        # exact hosts only unless the profile explicitly says '*'.
        return False


# ------------------------------------------------------------------- planning
@dataclass(frozen=True)
class PlanningContext(ContractVersioned):
    run_id: str
    run_source_plan_id: str
    source_snapshot_ref: str
    binding_revision_id: str
    permission_profile_revision: str
    policy_snapshot_ref: str
    query_revision_ref: str | None = None
    budget_snapshot_ref: str | None = None
    contract_version: str = "1"


@dataclass(frozen=True)
class RequestPlan:
    """Host-validated HTTP request plan (module 02 section 11.1)."""

    method: str
    url: str
    headers_without_secrets: dict[str, str] = field(default_factory=dict)
    body_json: str | None = None
    expected_content_types: tuple[str, ...] = ("application/json",)
    allowed_redirects: bool = False
    timeout_s: float = 30.0
    max_bytes: int = 10 * 1024 * 1024
    cache_policy: str = "NO_CACHE"
    revalidation_headers_allowed: bool = False
    auth_scope_ref: str | None = None
    egress_requirement: str = "DIRECT"
    purpose: str = "read"
    operation_capability_ref: str | None = None
    expected_operation_class: str = "READ_ONLY"
    validators: dict[str, str] = field(default_factory=dict)

    def validate_read_only(self) -> None:
        if self.method.upper() not in {"GET", "HEAD"}:
            # Only an explicitly supported read-only search/filter POST may be
            # allowed by capability; default deny.
            if self.expected_operation_class != "READ_ONLY_SEARCH_POST":
                raise ValueError(
                    f"method {self.method} not permitted for read acquisition"
                )


@dataclass(frozen=True)
class BrowserActionPlan:
    """Bounded declarative browser plan (module 02 section 11.2)."""

    entry_url: str
    auth_scope_ref: str | None
    allowed_hosts: tuple[str, ...]
    max_actions: int = 15
    max_runtime_s: float = 120.0
    actions: tuple[Mapping[str, Any], ...] = ()
    completion_guards: tuple[Mapping[str, Any], ...] = ()
    capture_requirements: tuple[str, ...] = ("html",)
    approved_action_capabilities: tuple[str, ...] = ()
    expected_request_guards: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class ExecutionPlanEnvelope:
    """Common ownership/policy envelope for executors (module 02 section 11)."""

    plan_id: str
    request_id: str
    attempt_id: str
    run_id: str
    run_source_plan_id: str
    source_id: str
    binding_id: str
    binding_revision_id: str
    adapter_id: str
    adapter_version: str
    strategy: str
    execution_class: str
    policy_snapshot_ref: str
    permission_profile_id: str
    permission_profile_revision: str
    payload_kind: str  # "RequestPlan" | "BrowserActionPlan"
    payload: RequestPlan | BrowserActionPlan

    def validate(self) -> None:
        if self.payload_kind == "RequestPlan":
            if not isinstance(self.payload, RequestPlan):
                raise ValueError("payload_kind RequestPlan requires RequestPlan payload")
            if isinstance(self.payload, RequestPlan):
                self.payload.validate_read_only()
        elif self.payload_kind == "BrowserActionPlan":
            if not isinstance(self.payload, BrowserActionPlan):
                raise ValueError("payload_kind BrowserActionPlan requires BrowserActionPlan payload")
        else:
            raise ValueError(f"unknown payload_kind {self.payload_kind}")


# -------------------------------------------------------------------- results
@dataclass(frozen=True)
class ResultEnvelope:
    """Normalized execution result (module 02 section 11.3)."""

    execution_plan_id: str
    request_id: str
    attempt_id: str
    run_source_plan_id: str
    source_id: str
    binding_id: str
    binding_revision_id: str
    adapter_id: str
    adapter_version: str
    strategy: str
    execution_class: str
    requested_url: str
    final_url: str | None
    status_code: int | None
    headers_redacted: dict[str, str]
    content_type: str | None
    body: bytes | None
    body_hash: str | None
    normalized_content_hash: str | None
    fetched_at: str
    duration_ms: int
    bytes_downloaded: int
    redirect_chain: list[str]
    transport: str  # "HTTP" | "BROWSER"
    browser_used: bool
    robots_decision: str | None
    validators_sent: dict[str, str]
    was_304: bool
    resource_blocking_applied: str | None
    failure_kind: str | None
    failure_detail: str | None


@dataclass(frozen=True)
class ClassifierEvidence:
    status_code: int | None
    final_url: str | None
    content_type: str | None
    title: str | None
    body_size: int
    markers: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidatedResultEnvelope(ContractVersioned):
    """The only input a normal adapter parser may consume (ACQ-02)."""

    result_envelope_ref: str
    result: ResultEnvelope
    validated_page_class: str
    validation_evidence_ref: str
    security_policy_result: str
    cache_representation_ref: str | None = None
    contract_version: str = "1"

    @property
    def is_valid_for_extraction(self) -> bool:
        return self.validated_page_class in {PageClass.VALID_LIST.value, PageClass.VALID_JOB.value}


@dataclass(frozen=True)
class ParseContext(ContractVersioned):
    request_id: str
    attempt_id: str
    run_source_plan_id: str
    parser_version: str
    recipe_version: str | None
    normalization_contract_version: str = "1"
    idempotency_namespace: str = ""
    contract_version: str = "1"


@dataclass(frozen=True)
class ObservationDraft:
    """One observation proposed by a parser before durable persistence."""

    source_job_id: str | None
    raw_url: str | None
    canonical_url_candidate: str | None
    application_url_candidate: str | None
    content: dict[str, Any]
    page_cursor: str | None = None
    enumeration_scope_key: str | None = None
    source_rank_or_order: int | None = None
    field_evidence: tuple[Mapping[str, Any], ...] = ()
    observation_unique_key: str | None = None
    is_closure_evidence: bool = False
    closure_kind: str | None = None


@dataclass(frozen=True)
class DiscoveredTask:
    """Typed child task emitted by a parser (ACQ-04)."""

    kind: DiscoveredTaskKind
    logical_key: str
    target_reference: str
    priority: int = 100
    depth: int = 1
    parent_observation_id: str | None = None


@dataclass(frozen=True)
class CoverageProposal:
    scope_key: str
    enumeration_scope_key: str | None
    page_cursor: str | None
    is_terminal: bool
    stop_reason: str | None


@dataclass(frozen=True)
class ParseOutcome(ContractVersioned):
    """Parser outcome model (ACQ-03). SUCCESS_EMPTY is not a failure."""

    kind: ParseOutcomeKind
    observations: tuple[ObservationDraft, ...] = ()
    closure_or_missing_evidence: tuple[Mapping[str, Any], ...] = ()
    discovered_tasks: tuple[DiscoveredTask, ...] = ()
    cursor_proposal: Mapping[str, Any] | None = None
    coverage_proposal: CoverageProposal | None = None
    continuation_required: bool = False
    failure_kind: str | None = None
    failure_detail: str | None = None
    contract_version: str = "1"

    @property
    def succeeded(self) -> bool:
        return self.kind in {
            ParseOutcomeKind.SUCCESS_WITH_JOBS,
            ParseOutcomeKind.SUCCESS_EMPTY,
        }


@dataclass(frozen=True)
class AdapterTask:
    kind: AdapterTaskKind
    payload: Mapping[str, Any]
    query_id: str | None = None


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
class TypedFailure:
    """Structured failure record (module 02 section 27)."""

    kind: FailureKind
    retryable: bool
    source_health_impact: str  # NONE | MINOR | MAJOR
    http_status: int | None = None
    source_id: str | None = None
    binding_id: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    attempt_id: str | None = None
    details_redacted: str | None = None
    observed_at: str = ""
