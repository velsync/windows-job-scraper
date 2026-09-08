"""Built-in Greenhouse Job Board API adapter (S2.5).

A **code adapter** (02 §9.2) for the public Greenhouse board API, one of the
initial production providers (02 §12.3).  It serves every Greenhouse board
through one reviewed identity (02 §8.2): the board token is pinned in the
immutable binding revision, so a single adapter definition covers thousands
of employers.

Contract surface (02 ACQ-02):

* ``ENUMERATE`` → ``GET {api}/v1/boards/{board}/jobs[?content=true]``.  The
  endpoint returns the board's whole membership in one response, so there is
  no cursor to advance; completeness is checked against the provider's own
  declared ``meta.total`` and a short response is a bounded ``PARTIAL``,
  never an authoritative empty (ACQ-03, §19);
* ``DETAIL`` → ``GET {api}/v1/boards/{board}/jobs/{job_id}``, planned only
  from the **pinned board token plus a validated source-native id**.  A URL,
  host or board found in page content is never a fetch target (ACQ-04:
  "discovered URL/reference ≠ permission to perform I/O");
* ``HEALTH``/``SMOKE`` → the board endpoint, parsed for recognition only;
  they never emit observations;
* ``CRAWL``/``DISCOVER`` are explicitly unsupported: generic crawl breadth is
  ROAD-04 and discovery probes belong to the generic discovery binding.

Discipline preserved from the accepted Slice-1/Slice-2 surfaces:

* required-field failures reject the item with structured review evidence and
  never invent values; a page whose items all fail is ``PARSE_MARKER_MISSING``,
  never ``SUCCESS_EMPTY`` (02 §22, ACQ-03);
* no I/O, no database, no policy authority — the adapter plans and parses
  only; the host validates, executes, fences and persists (ARC-04.4, ACQ-02);
* URL shapes come from :mod:`jobscraper.acquisition.atsendpoints`, the single
  versioned owner of "known ATS endpoint patterns" (02 §12.1, §32);
* durable timestamps are UTC RFC 3339 produced by :mod:`jobscraper.timeutil`,
  the single owner of that format (03 §50); an unparseable provider timestamp
  is dropped, never guessed.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from jobscraper.acquisition.atsendpoints import spec_for_provider
from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.acquisition.failures import FailureKind, FailureRecord
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
    DiscoveredTask,
    FieldEvidenceRecord,
    ObservationRecord,
    ParseOutcome,
    ParseOutcomeKind,
    StopPolicy,
    ValidatedResult,
    validate_manifest,
    value_hash,
)
from jobscraper.net.urlnorm import (
    UrlNormalizationError,
    has_embedded_credentials,
    normalize_url,
)
from jobscraper.timeutil import parse_rfc3339, to_rfc3339

MANIFEST = validate_manifest(
    {
        "id": "greenhouse",
        "version": "1.0.0",
        "adapter_api_version": "1",
        # no "discover": discovery probes are the generic discovery binding's
        # task (02 §12.1).  no "incremental": the board API exposes no
        # since/cursor parameter, and claiming one would be a lie.
        "capabilities": ["listing_parse", "detail_parse", "health", "smoke"],
        "supported_execution_classes": ["HTTP"],
        "supported_auth_modes": ["NONE"],
        "cost_class": "LIGHT",
        "cursor_schema_version": 1,
    }
)

ADAPTER_ID = MANIFEST.id
ADAPTER_VERSION = MANIFEST.version

#: The provider this adapter speaks for, and the endpoint authority it uses.
PROVIDER = "GREENHOUSE"
_SPEC = spec_for_provider(PROVIDER)


def _origin_and_path(template: str) -> tuple[str, str]:
    """Split a versioned endpoint template into ``(origin, path)``."""
    normalized = normalize_url(template, drop_fragment=False)
    port = f":{normalized.port}" if normalized.port else ""
    return f"{normalized.scheme}://{normalized.host}{port}", normalized.path


#: Public API origin and the two path shapes, both taken from the endpoint
#: table so "what a Greenhouse board URL looks like" has exactly one owner.
DEFAULT_API_BASE_URL, LIST_PATH_TEMPLATE = _origin_and_path(_SPEC.list_url_template)
_, DETAIL_PATH_TEMPLATE = _origin_and_path(_SPEC.detail_url_template)
#: Public, job-specific board page a user is sent to (PROD-06).  Derived from
#: the endpoint table + pinned board token, never from scraped content.
JOB_PAGE_TEMPLATE = _SPEC.job_url_template

#: Declared stop policy (02 §19).  The board endpoint is one page; the request
#: budget is one enumeration plus the bounded detail enrichment it may spawn.
STOP_POLICY = StopPolicy(
    max_pages=1,
    max_consecutive_empty_pages=1,
    max_consecutive_no_new_jobs_pages=1,
    max_duplicate_pages=1,
    max_runtime_s=300.0,
    max_requests=51,
)

#: Host policy caps the plan may not exceed (02 §11.1 / 04 §5.1); a config
#: above them is refused at construction instead of at execution time.
MAX_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 2_000_000

#: Hard bound on typed detail child work per enumeration (ACQ-04).
MAX_DETAIL_REQUESTS = STOP_POLICY.max_requests - STOP_POLICY.max_pages
DEFAULT_MAX_DETAIL_REQUESTS = 25

#: Detail child work is claimed *after* the enumeration pages that produced
#: it, so listing coverage is proven before enrichment spends the budget.
DETAIL_TASK_PRIORITY = -5
DETAIL_TASK_DEPTH = 1

_BOARD_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_JOB_ID_RE = re.compile(r"^\d{1,20}$")

#: Route words are never a board token: accepting one would let a config build
#: ``/v1/boards/v1/jobs`` and read a reserved segment as an employer.  Mirrors
#: the guard in :mod:`jobscraper.acquisition.atsendpoints`.
_RESERVED_BOARD_TOKENS = frozenset(
    {"boards", "board", "jobs", "job", "v0", "v1", "api", "search", "all", "list"}
)

_REVIEW_EXCERPT_CHARS = 120


@dataclass(frozen=True)
class GreenhouseConfig:
    """Immutable binding-revision config snapshot for one Greenhouse board.

    Every value is operator/host-provisioned and pinned with the binding
    revision (02 §8.3).  Nothing here may be widened by scraped content: the
    board token and the API origin are the only inputs used to build request
    URLs, and both are validated at construction.
    """

    #: Greenhouse board token (``boards.greenhouse.io/{board}``).
    board: str
    #: API origin.  Defaults to the provider's public API; an operator may pin
    #: another origin (for example an offline fixture host), which the host's
    #: destination policy still validates per connection (04 §5.1).
    api_base_url: str = DEFAULT_API_BASE_URL
    #: Employer display name for this board.  The public board API does not
    #: carry the employer's name, so it is either declared here or left
    #: absent — it is never guessed from the board token.
    company_name: str | None = None
    #: Employer careers URL, when the operator knows it (01 §33.1 signal).
    careers_url: str | None = None
    #: ``True`` requests ``?content=true`` so the enumeration carries content
    #: and no detail child work is needed.
    include_content: bool = False
    #: ``True`` emits typed ``DETAIL`` child tasks for jobs whose content the
    #: enumeration did not carry.
    detail_fetch: bool = True
    #: Bound on detail child tasks per enumeration (ACQ-04).
    max_detail_requests: int = DEFAULT_MAX_DETAIL_REQUESTS
    timeout_s: float = MAX_TIMEOUT_S
    max_bytes: int = MAX_BODY_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "board", _validated_board(self.board))
        object.__setattr__(self, "api_base_url", _validated_origin(self.api_base_url))
        object.__setattr__(self, "company_name", _validated_name(self.company_name))
        object.__setattr__(self, "careers_url", _validated_careers_url(self.careers_url))
        if not isinstance(self.include_content, bool):
            raise ValueError("include_content must be a boolean")
        if not isinstance(self.detail_fetch, bool):
            raise ValueError("detail_fetch must be a boolean")
        if isinstance(self.max_detail_requests, bool) or not isinstance(
            self.max_detail_requests, int
        ):
            raise ValueError("max_detail_requests must be an integer")
        if not 0 <= self.max_detail_requests <= MAX_DETAIL_REQUESTS:
            raise ValueError(
                f"max_detail_requests must be within 0..{MAX_DETAIL_REQUESTS}, "
                f"got {self.max_detail_requests!r}"
            )
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)):
            raise ValueError("timeout_s must be a number")
        if not 0 < float(self.timeout_s) <= MAX_TIMEOUT_S:
            raise ValueError(
                f"timeout_s must be within (0, {MAX_TIMEOUT_S}] — the host policy "
                f"cap is not negotiable by binding config"
            )
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int):
            raise ValueError("max_bytes must be an integer")
        if not 0 < self.max_bytes <= MAX_BODY_BYTES:
            raise ValueError(
                f"max_bytes must be within (0, {MAX_BODY_BYTES}] — the host policy "
                f"cap is not negotiable by binding config"
            )

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "GreenhouseConfig":
        """Typed construction from a binding-revision ``config_json`` object.

        Unknown keys are refused rather than ignored: a config the adapter
        does not understand must not silently run with different semantics
        (02 §9: no host policy inside adapter manifests/config).
        """
        if not isinstance(config, Mapping):
            raise ValueError("greenhouse binding config must be a mapping")
        unknown = sorted(str(key) for key in config if key not in _CONFIG_FIELD_NAMES)
        if unknown:
            raise ValueError(f"unknown greenhouse binding config keys: {unknown}")
        return cls(**dict(config))


#: Accepted ``config_json`` members (a binding config the adapter does not
#: understand is refused, never partially applied).
_CONFIG_FIELD_NAMES = frozenset(
    config_field.name for config_field in dataclasses.fields(GreenhouseConfig)
)


# ---------------------------------------------------------------------------
# Validation helpers (fail closed, never coerce silently)
# ---------------------------------------------------------------------------


def _validated_board(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"board must be a string, got {type(value).__name__}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        # Control characters (including a newline) are refused rather than
        # cleaned away: a token that needed sanitizing is not a token this
        # host understands (fail closed, as in net.safelinks/net.urlnorm).
        raise ValueError(f"board token contains control characters: {value!r}")
    board = value.strip().lower()
    if not _BOARD_TOKEN_RE.fullmatch(board):
        raise ValueError(
            f"invalid Greenhouse board token {value!r}: expected a slug matching "
            "[a-z0-9][a-z0-9_-]{0,63}"
        )
    if board in _RESERVED_BOARD_TOKENS:
        raise ValueError(f"{board!r} is an endpoint route word, not a board token")
    return board


def _validated_origin(value: Any) -> str:
    """A bare http(s) origin: scheme + host [+ port], and nothing else."""
    if not isinstance(value, str):
        raise ValueError(f"api_base_url must be a string, got {type(value).__name__}")
    candidate = value.strip()
    if not candidate:
        raise ValueError("api_base_url must not be empty")
    if has_embedded_credentials(candidate):
        raise ValueError("api_base_url must not carry credentials")
    try:
        normalized = normalize_url(candidate, drop_fragment=False)
    except (UrlNormalizationError, ValueError) as exc:
        raise ValueError(f"api_base_url is not a usable origin: {exc}") from exc
    if normalized.scheme not in ("http", "https"):
        raise ValueError(f"api_base_url scheme {normalized.scheme!r} is not http(s)")
    if not normalized.host:
        raise ValueError("api_base_url has no host")
    if normalized.path not in ("", "/") or normalized.query or normalized.fragment:
        raise ValueError(
            "api_base_url must be a bare origin: path/query/fragment are not "
            "part of a pinned API base"
        )
    port = f":{normalized.port}" if normalized.port else ""
    return f"{normalized.scheme}://{normalized.host}{port}"


def _validated_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"company_name must be a string or null, got {type(value).__name__}")
    name = value.strip()
    if not name:
        raise ValueError("company_name must not be empty when provided")
    if len(name) > 200:
        raise ValueError("company_name exceeds 200 characters")
    return name


def _validated_careers_url(value: Any) -> str | None:
    if value is None:
        return None
    safe = _safe_http_url(value)
    if safe is None:
        raise ValueError(f"careers_url is not a safe http(s) URL: {value!r}")
    return safe


def _safe_http_url(value: Any) -> str | None:
    """The raw URL when — and only when — it is a usable http(s) URL.

    02 §31: the raw spelling is preserved (identity comparison normalizes
    later); PROD-05: an active scheme (``javascript:``, ``data:``) or embedded
    credentials never becomes a link candidate.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or has_embedded_credentials(candidate):
        return None
    try:
        normalized = normalize_url(candidate, drop_fragment=False)
    except (UrlNormalizationError, ValueError):
        return None
    if normalized.scheme not in ("http", "https") or not normalized.host:
        return None
    return candidate


def _job_id(value: Any) -> str | None:
    """A Greenhouse source-native job id, or ``None``.

    Ids are plain integers.  Strictness here is the security property: the
    returned string is interpolated into a detail URL, so anything that is not
    exactly digits is refused instead of being cleaned (no traversal, no
    query, no embedded URL).
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None
    return text if _JOB_ID_RE.fullmatch(text) else None


def _text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("name") or value.get("text")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _utc_timestamp(value: Any) -> str | None:
    """Provider timestamp → durable UTC RFC 3339 (03 §50), or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return to_rfc3339(parse_rfc3339(value))
    except (ValueError, TypeError, OverflowError):
        return None


def _flat(entry: Mapping) -> dict:
    """A JSON-safe, scalar-only projection of one provider location object."""
    flat: dict = {}
    for key, value in entry.items():
        if not isinstance(key, str) or isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                flat[key] = text
        elif isinstance(value, (int, float)):
            flat[key] = value
    return flat


def _excerpt(value: Any) -> str:
    if isinstance(value, str):
        return value[:_REVIEW_EXCERPT_CHARS]
    try:
        return json.dumps(value, sort_keys=True, default=str)[:_REVIEW_EXCERPT_CHARS]
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)[:_REVIEW_EXCERPT_CHARS]


def _json_object(body: bytes) -> tuple[dict | None, str | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"body is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, f"expected a JSON object, got {type(payload).__name__}"
    return payload, None


def _evidence_refs(result: ValidatedResult) -> tuple[str, ...]:
    """ACQ-09 references to the durable evidence behind this outcome."""
    refs: list[str] = []
    for ref in (result.result_envelope_ref, result.validation_evidence_ref):
        if ref and ref not in refs:
            refs.append(ref)
    return tuple(refs)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GreenhouseAdapter:
    """plan / parse / next_cursor for one pinned Greenhouse board."""

    manifest = MANIFEST

    #: Declared stop policy (02 §19), exposed so the host can bound a run
    #: against what this adapter itself claims.
    stop_policy = STOP_POLICY

    #: 03 §40 coverage barrier: this binding contract proves listing presence
    #: from the enumeration alone — the board endpoint returns the whole
    #: membership with source-native ids — so detail completion is *not* part
    #: of the absence-authority barrier.  Detail work still has to be drained
    #: before the run may terminalize (ACQ-04 child work).
    listing_identity_sufficient = True

    def __init__(self, config: GreenhouseConfig):
        self.config = config

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "GreenhouseAdapter":
        """Registry construction from a binding-revision config snapshot."""
        return cls(GreenhouseConfig.from_mapping(config or {}))

    # ------------------------------------------------------------------ plan

    def plan(
        self,
        task: AdapterTask,
        cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> RequestPlan:
        kind = task.kind
        if kind is AdapterTaskKind.DETAIL:
            url = self._detail_url(task)
        elif kind in (
            AdapterTaskKind.ENUMERATE,
            AdapterTaskKind.HEALTH,
            AdapterTaskKind.SMOKE,
        ):
            url = self._list_url()
        else:
            raise ValueError(
                f"greenhouse adapter does not support {kind.value} tasks "
                "(02 ACQ-02: every supported task is explicit)"
            )
        return RequestPlan(
            method="GET",
            url=url,
            headers={"Accept": "application/json"},
            expected_content_types=("application/json",),
            timeout_s=self.config.timeout_s,
            max_bytes=self.config.max_bytes,
            purpose=kind.value,
        )

    def _list_url(self) -> str:
        url = f"{self.config.api_base_url}{LIST_PATH_TEMPLATE.format(board=self.config.board)}"
        return f"{url}?content=true" if self.config.include_content else url

    def _detail_url(self, task: AdapterTask) -> str:
        """The detail target: pinned board token + validated source-native id.

        Only ``target_reference`` is read from the task payload.  A board, host
        or URL appearing in the payload (i.e. originating from content) is
        ignored by construction, so scraped content cannot re-point a fetch.
        """
        payload = task.payload or {}
        job_id = _job_id(payload.get("target_reference"))
        if job_id is None:
            raise ValueError(
                "DETAIL task carries no usable source-native Greenhouse job id;"
                " refusing to plan a fetch (02 ACQ-04: a discovered reference is"
                " not I/O permission)"
            )
        return (
            f"{self.config.api_base_url}"
            f"{DETAIL_PATH_TEMPLATE.format(board=self.config.board, job_id=job_id)}"
        )

    # ----------------------------------------------------------------- parse

    def parse(
        self,
        task: AdapterTask,
        result: ValidatedResult,
        ctx: Any = None,
    ) -> ParseOutcome:
        kind = task.kind
        if kind in (AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE):
            return self._parse_probe(result)
        if kind not in (AdapterTaskKind.ENUMERATE, AdapterTaskKind.DETAIL):
            raise ValueError(
                f"greenhouse adapter cannot parse {kind.value} tasks "
                "(02 ACQ-02: every supported task is explicit)"
            )
        payload, error = _json_object(result.envelope.body)
        if payload is None:
            return self._failure(
                result.envelope, FailureKind.PARSE_MARKER_MISSING, error or "unusable body"
            )
        if kind is AdapterTaskKind.ENUMERATE:
            return self._parse_list(payload, result)
        return self._parse_detail(payload, result, task)

    def _parse_probe(self, result: ValidatedResult) -> ParseOutcome:
        """HEALTH/SMOKE: recognize the board shape, never emit observations."""
        payload, error = _json_object(result.envelope.body)
        jobs = payload.get("jobs") if payload is not None else None
        if payload is None or not isinstance(jobs, list):
            return self._failure(
                result.envelope,
                FailureKind.PARSE_MARKER_MISSING,
                error or "health probe did not find a 'jobs' array",
            )
        return ParseOutcome(
            kind=ParseOutcomeKind.SUCCESS_EMPTY,
            review_evidence=(
                {"reason": "HEALTH_PROBE_RECOGNIZED", "listed_jobs": len(jobs)},
            ),
            evidence_refs=_evidence_refs(result),
        )

    # ------------------------------------------------------------ list parse

    def _parse_list(self, payload: dict, result: ValidatedResult) -> ParseOutcome:
        envelope = result.envelope
        items = payload.get("jobs")
        if not isinstance(items, list):
            return self._failure(
                envelope,
                FailureKind.PARSE_MARKER_MISSING,
                "response carries no 'jobs' array — the board template changed",
                review=(
                    {
                        "reason": "ITEMS_MARKER_MISSING",
                        "top_level_keys": sorted(str(key) for key in payload)[:20],
                    },
                ),
            )
        declared_total = _declared_total(payload)
        refs = _evidence_refs(result)
        if not items:
            # ACQ-03: a recognized empty board is success, not a failure.
            return ParseOutcome(
                kind=ParseOutcomeKind.SUCCESS_EMPTY,
                coverage_proposal={
                    "declared_total": declared_total,
                    "observed": 0,
                    "detail_tasks": 0,
                },
                evidence_refs=refs,
            )

        observations: list[ObservationRecord] = []
        review: list[dict] = []
        tasks: list[DiscoveredTask] = []
        for order, item in enumerate(items):
            observation, item_review, task = self._list_observation(item, order, envelope)
            if observation is not None:
                observations.append(observation)
            if item_review is not None:
                review.append(item_review)
            if task is not None:
                tasks.append(task)

        if not observations:
            return self._failure(
                envelope,
                FailureKind.PARSE_MARKER_MISSING,
                f"all {len(items)} listed jobs failed required-field validation",
                review=review,
            )

        budget = self.config.max_detail_requests if self.config.detail_fetch else 0
        if len(tasks) > budget:
            capped = tasks[budget:]
            tasks = tasks[:budget]
            review.append(
                {
                    "reason": "DETAIL_BUDGET_REACHED",
                    "bound": budget,
                    "deferred_job_ids": [task.target_reference for task in capped][:50],
                }
            )

        # §19/ACQ-03: the provider declared more members than it returned, so
        # this enumeration did not complete — bounded PARTIAL, no authority.
        truncated = declared_total is not None and declared_total > len(items)
        return ParseOutcome(
            kind=ParseOutcomeKind.PARTIAL if truncated else ParseOutcomeKind.SUCCESS_WITH_JOBS,
            observations=tuple(observations),
            discovered_tasks=tuple(tasks),
            review_evidence=tuple(review),
            continuation_required=truncated,
            coverage_proposal={
                "declared_total": declared_total,
                "observed": len(observations),
                "detail_tasks": len(tasks),
            },
            evidence_refs=refs,
        )

    def _list_observation(
        self, item: Any, order: int, envelope
    ) -> tuple[ObservationRecord | None, dict | None, DiscoveredTask | None]:
        if not isinstance(item, Mapping):
            return None, {"reason": "ITEM_NOT_AN_OBJECT", "order": order}, None
        job_id = _job_id(item.get("id"))
        title = _text(item.get("title"))
        if job_id is None or title is None:
            return None, self._rejection(item, order, job_id, title), None

        fields, evidence, field_review = self._fields(item, job_id=job_id, title=title)
        canonical_url = _safe_http_url(item.get("absolute_url"))
        record = ObservationRecord(
            source_job_id=job_id,
            raw_url=envelope.final_url,
            canonical_url_candidate=canonical_url,
            application_url_candidate=self._job_page_url(job_id),
            fields=fields,
            field_evidence=tuple(evidence),
            source_rank_or_order=order,
        )
        task = None
        if self.config.detail_fetch and not _text(item.get("content")):
            task = DiscoveredTask(
                kind="DETAIL",
                logical_key=f"greenhouse:{self.config.board}:job:{job_id}",
                target_reference=job_id,
                priority=DETAIL_TASK_PRIORITY,
                depth=DETAIL_TASK_DEPTH,
            )
        return record, field_review, task

    def _rejection(self, item: Mapping, order: int, job_id: str | None, title: str | None) -> dict:
        """Structured review evidence for one rejected item (02 §22)."""
        missing = [name for name, value in (("id", job_id), ("title", title)) if value is None]
        review: dict = {
            "reason": "REQUIRED_FIELD_MISSING",
            "order": order,
            "missing": missing,
            "item_keys": sorted(str(key) for key in item)[:20],
        }
        # A refused identifier is evidence, not a fetch target: it is recorded
        # bounded so a reviewer can see what the source actually sent.
        raw_id = item.get("id")
        if job_id is None and raw_id is not None:
            review["rejected_id_excerpt"] = _excerpt(raw_id)
        raw_url = item.get("absolute_url")
        if raw_url is not None and _safe_http_url(raw_url) is None:
            review["rejected_absolute_url_excerpt"] = _excerpt(raw_url)
        return review

    def _job_page_url(self, job_id: str) -> str:
        """The direct application URL: endpoint table + pinned board + id."""
        return JOB_PAGE_TEMPLATE.format(board=self.config.board, job_id=job_id)

    # ---------------------------------------------------------- detail parse

    def _parse_detail(
        self, payload: dict, result: ValidatedResult, task: AdapterTask
    ) -> ParseOutcome:
        envelope = result.envelope
        requested = _job_id((task.payload or {}).get("target_reference"))
        job_id = _job_id(payload.get("id"))
        refs = _evidence_refs(result)
        if requested is not None and job_id is not None and requested != job_id:
            # The response describes a different posting than the one asked
            # for.  Emitting an observation here would attribute content to a
            # job the provider never returned (02 §22, ACQ-02).
            return ParseOutcome(
                kind=ParseOutcomeKind.FAILURE,
                failure=self._failure_record(
                    envelope,
                    FailureKind.INVALID_JOB_RECORD,
                    f"detail response describes job {job_id}, request asked for {requested}",
                ),
                closure_or_missing_evidence=(
                    {
                        "reason": "DETAIL_ID_MISMATCH",
                        "target_reference": requested,
                        "observed_id": job_id,
                    },
                ),
                evidence_refs=refs,
            )

        title = _text(payload.get("title"))
        review: list[dict] = []
        if job_id is None or title is None:
            missing = [name for name, value in (("id", job_id), ("title", title)) if value is None]
            review.append(
                {
                    "reason": "REQUIRED_FIELD_MISSING",
                    "missing": missing,
                    "item_keys": sorted(str(key) for key in payload)[:20],
                }
            )
            return ParseOutcome(
                kind=ParseOutcomeKind.FAILURE,
                review_evidence=tuple(review),
                failure=self._failure_record(
                    envelope,
                    FailureKind.PARSE_MARKER_MISSING,
                    f"detail response is missing required field(s): {', '.join(missing)}",
                ),
                closure_or_missing_evidence=(
                    {
                        "reason": "REQUIRED_FIELD_MISSING",
                        "target_reference": requested,
                        "missing": missing,
                    },
                ),
                evidence_refs=refs,
            )

        fields, evidence, field_review = self._fields(payload, job_id=job_id, title=title)
        if field_review is not None:
            review.append(field_review)
        canonical_url = _safe_http_url(payload.get("absolute_url"))
        if canonical_url is None and payload.get("absolute_url") is not None:
            review.append(
                {
                    "reason": "UNSAFE_URL_REFUSED",
                    "field": "absolute_url",
                    "excerpt": _excerpt(payload.get("absolute_url")),
                }
            )
        observation = ObservationRecord(
            source_job_id=job_id,
            raw_url=envelope.final_url,
            canonical_url_candidate=canonical_url,
            application_url_candidate=self._job_page_url(job_id),
            fields=fields,
            field_evidence=tuple(evidence),
        )
        return ParseOutcome(
            kind=ParseOutcomeKind.SUCCESS_WITH_JOBS,
            observations=(observation,),
            review_evidence=tuple(review),
            evidence_refs=refs,
        )

    # ---------------------------------------------------------------- fields

    def _fields(
        self, item: Mapping, *, job_id: str, title: str
    ) -> tuple[dict, list[FieldEvidenceRecord], dict | None]:
        """Extract the observation fields plus their locator evidence.

        Returns ``(fields, evidence, review_or_None)``.  Values the provider
        did not send are absent — never defaulted (02 §22).
        """
        fields: dict = {}
        evidence: list[FieldEvidenceRecord] = []
        review: dict | None = None

        def put(name: str, value: Any, locator: str, *, kind: str = "json_path") -> None:
            if value is None or value == [] or value == "":
                return
            fields[name] = value
            evidence.append(
                FieldEvidenceRecord(
                    field_name=name,
                    locator_kind=kind,
                    locator_value=locator,
                    value_hash=value_hash(value),
                    excerpt=_excerpt(value),
                )
            )

        put("source_job_id", job_id, "id")
        put("title", title, "title")
        put("company", self.config.company_name, "company_name", kind="binding_config")
        put("careers_url", self.config.careers_url, "careers_url", kind="binding_config")

        content = item.get("content")
        if isinstance(content, str) and content.strip():
            put("description", content, "content")

        locations, locator = _location_signal(item)
        put("locations", locations, locator)
        requirements = [
            flat
            for flat in (
                _flat(entry)
                for entry in (item.get("applicant_location_requirements") or ())
                if isinstance(entry, Mapping)
            )
            if flat
        ]
        put("applicant_location_requirements", requirements, "applicant_location_requirements")
        put("departments", _names(item.get("departments")), "departments")
        put("offices", _names(item.get("offices")), "offices")
        put("requisition_id", _text(item.get("requisition_id")), "requisition_id")

        posted_at, posted_locator = _posted_at(item)
        put("posted_at", posted_at, posted_locator)

        employment_type, employment_locator = _employment_type(item)
        put("employment_type", employment_type, employment_locator)

        raw_url = item.get("absolute_url")
        if raw_url is not None and _safe_http_url(raw_url) is None:
            review = {
                "reason": "UNSAFE_URL_REFUSED",
                "field": "absolute_url",
                "excerpt": _excerpt(raw_url),
            }
        return fields, evidence, review

    # ------------------------------------------------------------ next cursor

    def next_cursor(
        self,
        task: AdapterTask,
        outcome: ParseOutcome,
        current_cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> CrawlCursor | None:
        """``None`` always: the board endpoint is not paginated.

        There is no next page to propose, and a bounded ``PARTIAL`` must never
        be mistaken for a terminal cursor (02 §19: one short page does not
        prove absence).
        """
        return None

    # --------------------------------------------------------------- failures

    def _failure(
        self,
        envelope,
        kind: FailureKind,
        detail: str,
        *,
        review: tuple | list = (),
    ) -> ParseOutcome:
        return ParseOutcome(
            kind=ParseOutcomeKind.FAILURE,
            review_evidence=tuple(review),
            failure=self._failure_record(envelope, kind, detail),
        )

    def _failure_record(self, envelope, kind: FailureKind, detail: str) -> FailureRecord:
        return FailureRecord(
            kind=kind,
            # A missing marker or an invalid record is a source-contract break:
            # retrying the same bytes cannot succeed (02 §27).
            retryable=False,
            source_health_impact="DEGRADED",
            http_status=envelope.status_code,
            source_id=envelope.source_id,
            binding_id=envelope.binding_id,
            adapter_id=envelope.adapter_id,
            adapter_version=envelope.adapter_version,
            run_id=None,
            request_id=envelope.request_id,
            attempt_id=envelope.attempt_id,
            details_redacted={"detail": detail[:500]},
        )


# ---------------------------------------------------------------------------
# Provider-shape readers (data-only, no policy)
# ---------------------------------------------------------------------------


def _declared_total(payload: Mapping) -> int | None:
    """The provider's own membership count, when it stated one."""
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        return None
    total = meta.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    return total


def _names(value: Any) -> list[str]:
    """Display names of one provider object array (departments/offices)."""
    if not isinstance(value, (list, tuple)):
        return []
    names = []
    for entry in value:
        name = _text(entry)
        if name:
            names.append(name)
    return names


def _location_signal(item: Mapping) -> tuple[list, str]:
    """The one location signal set for this posting, richest first.

    Precedence is deterministic and documented:

    1. ``job_listing_admin_locations`` — structured city/region/country plus
       the provider's own location type;
    2. ``location.name`` — the provider's composed text (``location-rules-v1``
       splits the documented multi-location separators);
    3. ``offices`` — office names/locations.

    Only one signal set is emitted so the canonical location *set* stays a set
    of distinct places (01 §33.2) instead of the same place repeated at three
    levels of precision.  ``applicant_location_requirements`` travels through
    its own documented side-channel, and offices/departments are always kept
    as field evidence.
    """
    admin = [
        flat
        for flat in (
            _flat(entry)
            for entry in (item.get("job_listing_admin_locations") or ())
            if isinstance(entry, Mapping)
        )
        if flat
    ]
    if admin:
        return admin, "job_listing_admin_locations"
    location = item.get("location")
    name = _text(location)
    if name:
        return [name], "location.name"
    offices: list[str] = []
    for office in item.get("offices") or ():
        if not isinstance(office, Mapping):
            continue
        text = _text(office.get("location")) or _text(office.get("name"))
        if text:
            offices.append(text)
    if offices:
        return offices, "offices"
    return [], ""


def _posted_at(item: Mapping) -> tuple[str | None, str]:
    """Posted time from the provider's own *publication* stamps only.

    ``updated_at`` is deliberately not a fallback: on the board API it is a
    last-modified time, so mapping it to ``posted_at`` would assert a
    publication date the provider never stated — and because a presence keeps
    the first value it was given, that guess would then shadow the real
    ``first_published_at`` arriving on the detail pass.  When the provider
    states no publication time the field is simply absent (02 §22: values the
    provider did not send are never defaulted), and ``discovered_at`` /
    ``first_seen_at`` still record when the host saw the posting.
    """
    for locator in (
        "first_published_at",
        "job_post_information.date_published",
    ):
        node: Any = item
        for part in locator.split("."):
            node = node.get(part) if isinstance(node, Mapping) else None
        stamp = _utc_timestamp(node)
        if stamp is not None:
            return stamp, locator
    return None, "updated_at"


def _employment_type(item: Mapping) -> tuple[str | None, str]:
    """The provider's own employment-type wording (01 §33).

    Greenhouse spells the member ``employement_type``; both spellings are read
    and the raw wording is passed through unmapped — normalization owns the
    vocabulary.
    """
    info = item.get("job_post_information")
    if isinstance(info, Mapping):
        for key in ("employement_type", "employment_type"):
            text = _text(info.get(key))
            if text:
                return text, f"job_post_information.{key}"
    return None, "job_post_information.employement_type"


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_MAX_DETAIL_REQUESTS",
    "DETAIL_TASK_DEPTH",
    "DETAIL_TASK_PRIORITY",
    "GreenhouseAdapter",
    "GreenhouseConfig",
    "JOB_PAGE_TEMPLATE",
    "MANIFEST",
    "MAX_BODY_BYTES",
    "MAX_DETAIL_REQUESTS",
    "MAX_TIMEOUT_S",
    "PROVIDER",
    "STOP_POLICY",
]
