"""Built-in Ashby Job Posting API adapter (S2.7).

A **code adapter** (02 §9.2) for the public Ashby Job Posting API, the third
of the initial production providers (02 §12.3).  Like the Greenhouse and
Lever adapters it serves every Ashby board through one reviewed identity
(02 §8.2): the board token is pinned in the immutable binding revision, so a
single adapter definition covers thousands of employers.

Provider contract researched 2026-09-09 (recorded in
``tests/fixtures/ashby/README.md`` and the S2.7 review record):

* ``GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true``
  returns a **single full JSON document**
  ``{"jobs": [...], "apiVersion": "1"}`` — the whole published board
  membership in one response.  There is **no pagination** and no declared
  total, so one recognized response is a complete enumeration and there is
  never a cursor to advance;
* there is **no public per-job detail endpoint**
  (``/posting-api/job-board/{org}/job/{id}`` answers 401): every posting's
  content, locations, links and compensation arrive inline in the board
  document, so this adapter declares no ``detail_parse`` capability, plans
  no ``DETAIL`` task and proposes no child work — detail completion cannot
  be part of its coverage story because listing identity is sufficient
  (03 §40);
* a posting no longer on the board simply stops appearing: its closure
  evidence is **absence** proven by a later COMPLETE generation (03 §40,
  RUN-13), never a per-job 404;
* ``isListed: false`` marks a posting that "should only be available via
  direct link" — it is not part of the public board's membership.  It is
  excluded with typed review evidence, never observed;
* ``apiVersion`` is a provider contract stamp (currently ``"1"``).  A
  response stamped anything else still parses — the member-level
  required-field discipline is unchanged — but it does not yield
  authoritative coverage: the outcome is ``PARTIAL`` with review evidence,
  so absence inference stays forbidden for that generation (ACQ-03).

Contract surface (02 ACQ-02):

* ``ENUMERATE`` → ``GET {api}/posting-api/job-board/{board}?includeCompensation=true``;
* ``HEALTH``/``SMOKE`` → the same document, parsed for recognition only;
  they never emit observations;
* ``DETAIL``/``CRAWL``/``DISCOVER`` are explicitly unsupported — requesting
  one raises, because a planned fetch is the only honest answer this API
  can give for those task kinds (crawl breadth is ROAD-04; discovery
  probes belong to the generic discovery binding).

Discipline preserved from the accepted S2.5/S2.6 adapters:

* required-field failures reject the item with structured review evidence and
  never invent values; a document whose listed members all fail is
  ``PARSE_MARKER_MISSING``, never ``SUCCESS_EMPTY`` (02 §22, ACQ-03);
* a rejected listed member makes the document ``PARTIAL`` — good
  observations persist, but the membership proof is broken for this
  generation and absence inference is forbidden (ACQ-03, RUN-13; the host
  durably degrades the generation, so this can never be laundered into
  ``COMPLETE``);
* ``publishedAt`` is the provider-stated *publication* time and maps to
  ``posted_at`` (02 §22; contrast Lever's ``createdAt``, which is creation
  time and stays evidence-only);
* ``jobUrl``/``applyUrl`` are kept as candidates/evidence only when the
  versioned endpoint table reads them as **this board's, this posting's**
  hosted URL — ``applyUrl`` is that URL plus ``/application`` — so a
  well-formed link about another board or posting can never re-attribute
  the job (02 §32; S2.6 F2);
* the direct application link the product shows is derived from the
  versioned endpoint table plus the pinned board token and validated id
  (PROD-06), never from scraped content;
* no I/O, no database, no policy authority — the adapter plans and parses
  only; the host validates, executes, fences and persists (ARC-04.4, ACQ-02);
* URL shapes come from :mod:`jobscraper.acquisition.atsendpoints`, the single
  versioned owner of "known ATS endpoint patterns" (02 §12.1, §32);
* durable timestamps are UTC RFC 3339 produced by :mod:`jobscraper.timeutil`
  (03 §50); an unusable provider timestamp is dropped, never guessed.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from jobscraper.acquisition.atsendpoints import identify_url, spec_for_provider
from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.acquisition.failures import FailureKind, FailureRecord
from jobscraper.adapters.contract import (
    AdapterTask,
    AdapterTaskKind,
    CrawlCursor,
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
        "id": "ashby",
        "version": "1.0.0",
        "adapter_api_version": "1",
        # no "discover": discovery probes are the generic discovery binding's
        # task (02 §12.1).  no "incremental": the posting API is a full
        # snapshot with no since parameter.  no "detail_parse": the public
        # posting API has no per-job detail endpoint.
        "capabilities": ["listing_parse", "health", "smoke"],
        "supported_execution_classes": ["HTTP"],
        "supported_auth_modes": ["NONE"],
        "cost_class": "LIGHT",
        "cursor_schema_version": 1,
    }
)

ADAPTER_ID = MANIFEST.id
ADAPTER_VERSION = MANIFEST.version

#: The provider this adapter speaks for, and the endpoint authority it uses.
PROVIDER = "ASHBY"
_SPEC = spec_for_provider(PROVIDER)


def _origin_and_path(template: str) -> tuple[str, str]:
    """Split a versioned endpoint template into ``(origin, path)``."""
    normalized = normalize_url(template, drop_fragment=False)
    port = f":{normalized.port}" if normalized.port else ""
    return f"{normalized.scheme}://{normalized.host}{port}", normalized.path


#: Public API origin and the single board path, taken from the endpoint
#: table so "what an Ashby board API URL looks like" has exactly one owner.
DEFAULT_API_BASE_URL, BOARD_PATH_TEMPLATE = _origin_and_path(_SPEC.list_url_template)
#: Public, job-specific hosted page a user is sent to (PROD-06).  Derived
#: from the endpoint table + pinned board token, never from scraped content.
JOB_PAGE_TEMPLATE = _SPEC.job_url_template

#: The provider contract stamp this parser builds against.  Anything else is
#: reviewable ``PARTIAL`` evidence, never silent SUCCESS and never FAILURE.
RECOGNIZED_API_VERSION = "1"

#: The one query the provider defines for the public board: ask for the
#: compensation evidence the posting API withholds unless explicitly
#: requested (02 §12.3 fixture contract; acceptance §72.11 salary honesty).
_COMPENSATION_QUERY = "includeCompensation=true"

#: Declared stop policy (02 §19).  The board document is one page and plans
#: exactly one request: the declared budget is honest by construction.
STOP_POLICY = StopPolicy(
    max_pages=1,
    max_consecutive_empty_pages=1,
    max_consecutive_no_new_jobs_pages=1,
    max_duplicate_pages=1,
    max_runtime_s=300.0,
    max_requests=1,
)

#: Host policy caps the plan may not exceed (02 §11.1 / 04 §5.1); a config
#: above them is refused at construction instead of at execution time.
MAX_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 2_000_000

_BOARD_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Source-native posting id: the same shape the endpoint table matches for
#: ASHBY job URLs (lowercase UUID-like tokens, 8..64 chars).  Strictness is
#: the security property: the value is interpolated into a derived URL.
_POSTING_ID_RE = re.compile(r"^[a-z0-9_-]{8,64}$")

#: Route words are never a board token or a posting id: accepting one would
#: let a config build ``/posting-api/job-board/job-board`` and read a
#: reserved segment as an employer.  Mirrors the guard in
#: :mod:`jobscraper.acquisition.atsendpoints`.
_RESERVED_TOKENS = frozenset(
    {
        "jobs", "job", "postings", "posting", "api", "v0", "v1", "boards",
        "board", "company", "companies", "search", "all", "list", "apply",
        "application", "applications", "careers", "index", "job-board",
        "posting-api", "departments", "offices", "teams", "content",
    }
)

#: Ashby's documented workplace-type vocabulary (02 §22: out-of-vocabulary
#: values are evidence, never silently coerced).
_WORKPLACE_TYPES = frozenset({"OnSite", "Remote", "Hybrid"})

#: The applyUrl suffix the provider appends to the hosted posting URL
#: (verified 2026-09-09: ``{jobUrl}/application``).
_APPLICATION_SUFFIX = "/application"

_REVIEW_EXCERPT_CHARS = 120


@dataclass(frozen=True)
class AshbyConfig:
    """Immutable binding-revision config snapshot for one Ashby board.

    Every value is operator/host-provisioned and pinned with the binding
    revision (02 §8.3).  Nothing here may be widened by scraped content: the
    board token and the API origin are the only inputs used to build request
    URLs, and both are validated at construction.
    """

    #: Ashby jobs page name (``jobs.ashbyhq.com/{board}``,
    #: ``/posting-api/job-board/{board}``).  The provider answers the same
    #: document regardless of the token's case and echoes the request's
    #: spelling, so the pinned token is canonicalized to lowercase exactly
    #: like the other providers.
    board: str
    #: API origin.  Defaults to the provider's public API; an operator may pin
    #: another origin (for example an offline fixture host), which the host's
    #: destination policy still validates per connection (04 §5.1).
    api_base_url: str = DEFAULT_API_BASE_URL
    #: Employer display name for this board.  The posting API does not carry
    #: the employer's name, so it is either declared here or left absent — it
    #: is never guessed from the board token.
    company_name: str | None = None
    #: Employer careers URL, when the operator knows it (01 §33.1 signal).
    careers_url: str | None = None
    timeout_s: float = MAX_TIMEOUT_S
    max_bytes: int = MAX_BODY_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "board", _validated_board(self.board))
        object.__setattr__(self, "api_base_url", _validated_origin(self.api_base_url))
        object.__setattr__(self, "company_name", _validated_name(self.company_name))
        object.__setattr__(self, "careers_url", _validated_careers_url(self.careers_url))
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
    def from_mapping(cls, config: Mapping[str, Any]) -> "AshbyConfig":
        """Typed construction from a binding-revision ``config_json`` object.

        Unknown keys are refused rather than ignored: a config the adapter
        does not understand must not silently run with different semantics
        (02 §9: no host policy inside adapter manifests/config).  In
        particular there is no ``detail_fetch``/``page_size`` knob — the
        posting API has neither a detail endpoint nor pagination, and a knob
        that plans nothing would lie about the contract.
        """
        if not isinstance(config, Mapping):
            raise ValueError("ashby binding config must be a mapping")
        unknown = sorted(str(key) for key in config if key not in _CONFIG_FIELD_NAMES)
        if unknown:
            raise ValueError(f"unknown ashby binding config keys: {unknown}")
        return cls(**dict(config))


#: Accepted ``config_json`` members (a binding config the adapter does not
#: understand is refused, never partially applied).
_CONFIG_FIELD_NAMES = frozenset(
    config_field.name for config_field in dataclasses.fields(AshbyConfig)
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
            f"invalid Ashby board token {value!r}: expected a slug matching "
            "[a-z0-9][a-z0-9_-]{0,63}"
        )
    if board in _RESERVED_TOKENS:
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


def _reviewed_posting_url(value: Any, *, board: str, posting_id: str) -> str | None:
    """A provider link, accepted only when it is *this* posting's on *this* board.

    Gates, all host-owned: the URL must be safe http(s); the endpoint table —
    not the payload — must recognize it as an Ashby hosted job URL; and the
    board token and posting id it names must equal the pinned board and the
    id being parsed.  A safe-looking URL on another host, another board or
    another posting is content, not a link candidate: origin resolution
    consumes this value as the canonical job URL (02 §32), so a foreign
    spelling here would re-attribute the job to a board or posting the host
    never fetched.

    Ashby's ``applyUrl`` is the hosted posting URL plus ``/application``
    (verified 2026-09-09), which the endpoint table deliberately does not
    treat as job-specific.  It is accepted as an *evidence field* under the
    same board/id gate by matching its parent path, query-free.
    """
    safe = _safe_http_url(value)
    if safe is None:
        return None
    if _matches_this_posting(safe, board=board, posting_id=posting_id):
        return safe
    normalized = normalize_url(safe, drop_fragment=False)
    path = normalized.path.rstrip("/")
    if path.endswith(_APPLICATION_SUFFIX) and not normalized.query:
        parent = (
            f"{normalized.scheme}://{normalized.host}"
            f"{path[: -len(_APPLICATION_SUFFIX)]}"
        )
        if _matches_this_posting(parent, board=board, posting_id=posting_id):
            return safe
    return None


def _matches_this_posting(url: str, *, board: str, posting_id: str) -> bool:
    """Endpoint-table recognition of ``url`` as *this* board's *this* posting."""
    match = identify_url(url, spec=_SPEC)
    if not match.job_specific or match.kind != "HOSTED":
        return False
    return match.board == board and match.job_id == posting_id


def _posting_id(value: Any) -> str | None:
    """An Ashby source-native posting id, or ``None``.

    Only lowercase ``[a-z0-9_-]{8,64}`` is accepted — exactly what the
    endpoint table recognizes in an Ashby job URL.  Anything else
    (uppercase, traversal, query, an embedded URL, a route word) is refused,
    never cleaned, because the value is interpolated into a derived URL.
    """
    if not isinstance(value, str):
        return None
    if not _POSTING_ID_RE.fullmatch(value) or value in _RESERVED_TOKENS:
        return None
    return value


def _text(value: Any) -> str | None:
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
    """A JSON-safe, scalar-only projection of one provider address object."""
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


def _api_version(value: Any) -> str | None:
    """The provider's contract stamp when it sent a non-empty string."""
    return _text(value)


def _is_recognized_version(value: Any) -> bool:
    return _api_version(value) == RECOGNIZED_API_VERSION


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AshbyAdapter:
    """plan / parse / next_cursor for one pinned Ashby board."""

    manifest = MANIFEST

    #: Declared stop policy (02 §19), exposed so the host can bound a run
    #: against what this adapter itself claims.
    stop_policy = STOP_POLICY

    #: 03 §40 coverage barrier: this binding contract proves listing presence
    #: from the enumeration alone — the posting API returns the whole board
    #: membership with source-native ids in one document.  There is no
    #: per-job endpoint to defer identity to, so no detail barrier exists.
    listing_identity_sufficient = True

    def __init__(self, config: AshbyConfig):
        self.config = config

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "AshbyAdapter":
        """Registry construction from a binding-revision config snapshot."""
        return cls(AshbyConfig.from_mapping(config or {}))

    # ------------------------------------------------------------------ plan

    def plan(
        self,
        task: AdapterTask,
        cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> RequestPlan:
        kind = task.kind
        if kind in (
            AdapterTaskKind.ENUMERATE,
            AdapterTaskKind.HEALTH,
            AdapterTaskKind.SMOKE,
        ):
            url = self._board_url()
        else:
            raise ValueError(
                f"ashby adapter does not support {kind.value} tasks "
                "(02 ACQ-02: every supported task is explicit — the public "
                "posting API has no detail endpoint, crawl breadth is "
                "ROAD-04, and discovery belongs to the generic binding)"
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

    def _board_url(self) -> str:
        """The one endpoint this API defines, built from pinned config.

        Only the pinned board token and the pinned origin are inputs; a
        board, host or URL found in page content (or in a task payload) is
        never consulted, so scraped content cannot re-point a fetch (ACQ-04).
        Any cursor is likewise ignored: one document is the whole board and
        a stale offset could only narrow it.
        """
        path = BOARD_PATH_TEMPLATE.format(board=self.config.board)
        return f"{self.config.api_base_url}{path}?{_COMPENSATION_QUERY}"

    # ---------------------------------------------------------------- cursor

    def next_cursor(
        self,
        task: AdapterTask,
        outcome: ParseOutcome,
        current_cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> CrawlCursor | None:
        """``None`` always: the posting API is a single full document.

        There is no next page to propose, and a bounded ``PARTIAL`` (a
        rejected member, an unrecognized contract version) must never be
        mistaken for a terminal cursor (02 §19; ACQ-03).
        """
        return None

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
        if kind is not AdapterTaskKind.ENUMERATE:
            raise ValueError(
                f"ashby adapter cannot parse {kind.value} tasks "
                "(02 ACQ-02: every supported task is explicit)"
            )
        payload, error = _json_object(result.envelope.body)
        if payload is None:
            return self._failure(
                result, FailureKind.PARSE_MARKER_MISSING, error or "unusable body"
            )
        return self._parse_board(payload, result)

    def _parse_probe(self, result: ValidatedResult) -> ParseOutcome:
        """HEALTH/SMOKE: recognize the board shape, never emit observations."""
        payload, error = _json_object(result.envelope.body)
        jobs = payload.get("jobs") if payload is not None else None
        if payload is None or not isinstance(jobs, list):
            return self._failure(
                result,
                FailureKind.PARSE_MARKER_MISSING,
                error or "health probe did not find a 'jobs' array",
            )
        return ParseOutcome(
            kind=ParseOutcomeKind.SUCCESS_EMPTY,
            # The probe is recognition-only, but every number it records must
            # be exact: postings_total is the document's array size, while
            # listed_members applies the same ``isListed is False`` gate as
            # enumeration — an unlisted posting is not part of the board this
            # probe just health-checked for.
            review_evidence=(
                {
                    "reason": "HEALTH_PROBE_RECOGNIZED",
                    "postings_total": len(jobs),
                    "listed_members": sum(
                        1
                        for job in jobs
                        if isinstance(job, Mapping) and job.get("isListed") is not False
                    ),
                },
            ),
            evidence_refs=_evidence_refs(result),
        )

    # ------------------------------------------------------------ board parse

    def _parse_board(self, payload: dict, result: ValidatedResult) -> ParseOutcome:
        envelope = result.envelope
        items = payload.get("jobs")
        if not isinstance(items, list):
            review: dict = {"reason": "ITEMS_MARKER_MISSING"}
            if "jobs" in payload:
                review["jobs_type"] = type(items).__name__
            review["top_level_keys"] = sorted(str(key) for key in payload)[:20]
            return self._failure(
                result,
                FailureKind.PARSE_MARKER_MISSING,
                "response carries no 'jobs' array — the board template changed",
                review=(review,),
            )

        refs = _evidence_refs(result)
        observations: list[ObservationRecord] = []
        review_items: list[dict] = []
        rejected_members = 0
        unlisted_excluded = 0
        seen_ids: set[str] = set()
        for order, item in enumerate(items):
            decision = self._board_member(item, order, envelope, seen_ids)
            if decision.observation is not None:
                observations.append(decision.observation)
                seen_ids.add(decision.observation.source_job_id)
            elif decision.outcome == "unlisted":
                unlisted_excluded += 1
            elif decision.outcome == "rejected":
                # The provider listed a member that could not be admitted to
                # the stable membership set.  Preserve good observations, but
                # never turn the resulting subset into absence authority.
                rejected_members += 1
            review_items.extend(decision.review)

        recognized_version = _is_recognized_version(payload.get("apiVersion"))
        if not recognized_version:
            review_items.append(
                {
                    "reason": "UNRECOGNIZED_API_VERSION",
                    "api_version": _api_version(payload.get("apiVersion")),
                }
            )

        if not observations and rejected_members:
            return self._failure(
                result,
                FailureKind.PARSE_MARKER_MISSING,
                f"all {rejected_members} listed jobs failed required-field validation",
                review=review_items,
            )

        coverage = {
            # the provider declares no total: the document *is* the membership
            "declared_total": None,
            "api_version": _api_version(payload.get("apiVersion")),
            "observed": len(observations),
            "rejected_members": rejected_members,
            "unlisted_excluded": unlisted_excluded,
        }
        if not observations and not rejected_members:
            if recognized_version:
                # ACQ-03: a recognized empty board is success, not a failure
                # (the listed set is genuinely empty; unlisted postings are
                # not part of it).
                return ParseOutcome(
                    kind=ParseOutcomeKind.SUCCESS_EMPTY,
                    review_evidence=tuple(review_items),
                    coverage_proposal=coverage,
                    evidence_refs=refs,
                )
            # An unrecognized contract stamp withholds absence authority even
            # over an empty listed set (ACQ-03, RUN-13).
            return ParseOutcome(
                kind=ParseOutcomeKind.PARTIAL,
                review_evidence=tuple(review_items),
                coverage_proposal=coverage,
                evidence_refs=refs,
            )

        # §19/ACQ-03/RUN-13: the membership proof is incomplete when a listed
        # member was rejected, or when the provider stamped the document with
        # a contract version this parser has not been reviewed against.  Good
        # observations still persist; absence inference stays forbidden.
        incomplete = rejected_members > 0 or not recognized_version
        return ParseOutcome(
            kind=(
                ParseOutcomeKind.PARTIAL
                if incomplete
                else ParseOutcomeKind.SUCCESS_WITH_JOBS
            ),
            observations=tuple(observations),
            review_evidence=tuple(review_items),
            # one document is terminal: there is never a page two
            continuation_required=False,
            coverage_proposal=coverage,
            evidence_refs=refs,
        )

    @dataclasses.dataclass(frozen=True)
    class _MemberDecision:
        observation: ObservationRecord | None
        outcome: str | None  # None when admitted; "unlisted" | "rejected" otherwise
        review: tuple[dict, ...]

    def _board_member(
        self, item: Any, order: int, envelope, seen_ids: set[str]
    ) -> "AshbyAdapter._MemberDecision":
        decision = self._MemberDecision
        if not isinstance(item, Mapping):
            return decision(
                None, "rejected", ({"reason": "ITEM_NOT_AN_OBJECT", "order": order},)
            )

        # ---- listing gate: ``isListed: false`` is the provider saying "not
        # part of the public board" (direct link only).  It is excluded from
        # membership with evidence; only the literal boolean False excludes.
        is_listed = item.get("isListed")
        review: list[dict] = []
        if is_listed is False:
            exclusion: dict = {"reason": "UNLISTED_POSTING_EXCLUDED", "order": order}
            raw_id = item.get("id")
            if _text(raw_id) is not None:
                exclusion["posting_id"] = _text(raw_id)
            return decision(None, "unlisted", (exclusion,))
        refused: list[dict] = []
        if is_listed is not None and not isinstance(is_listed, bool):
            refused.append({"field": "isListed", "excerpt": _excerpt(is_listed)})

        # ---- required identity
        posting_id = _posting_id(item.get("id"))
        title = _text(item.get("title"))
        if posting_id is None or title is None:
            return decision(
                None,
                "rejected",
                (*review, *_refusal_review(refused), self._rejection(item, order, posting_id, title)),
            )
        if posting_id in seen_ids:
            # The document contradicts itself about one identity.  The member
            # *set* stays well-defined (the id is a member, first spelling
            # wins); the duplication is review evidence, not a second
            # observation.
            return decision(
                None,
                None,
                (*review, {"reason": "DUPLICATE_POSTING_ID", "order": order, "posting_id": posting_id}),
            )

        fields, evidence, field_refused = self._fields(item, posting_id=posting_id, title=title)
        refused.extend(field_refused)
        review.extend(_refusal_review(refused))
        record = ObservationRecord(
            source_job_id=posting_id,
            raw_url=envelope.final_url,
            canonical_url_candidate=self._reviewed_link(item.get("jobUrl"), posting_id),
            application_url_candidate=self._job_page_url(posting_id),
            fields=fields,
            field_evidence=tuple(evidence),
            source_rank_or_order=order,
        )
        return decision(record, None, tuple(review))

    def _rejection(
        self, item: Mapping, order: int, posting_id: str | None, title: str | None
    ) -> dict:
        """Structured review evidence for one rejected item (02 §22)."""
        missing = [name for name, value in (("id", posting_id), ("title", title)) if value is None]
        review: dict = {
            "reason": "REQUIRED_FIELD_MISSING",
            "order": order,
            "missing": missing,
            "item_keys": sorted(str(key) for key in item)[:20],
        }
        # A refused identifier is evidence, not a fetch target: it is recorded
        # bounded so a reviewer can see what the source actually sent.
        raw_id = item.get("id")
        if posting_id is None and raw_id is not None:
            review["rejected_id_excerpt"] = _excerpt(raw_id)
        for name in ("jobUrl", "applyUrl"):
            raw_url = item.get(name)
            if raw_url is not None and _safe_http_url(raw_url) is None:
                review[f"rejected_{name}_excerpt"] = _excerpt(raw_url)
        return review

    def _job_page_url(self, posting_id: str) -> str:
        """The direct application URL: endpoint table + pinned board + id."""
        return JOB_PAGE_TEMPLATE.format(board=self.config.board, job_id=posting_id)

    def _reviewed_link(self, value: Any, posting_id: str) -> str | None:
        """A provider link candidate for *this* posting on *this* board."""
        return _reviewed_posting_url(value, board=self.config.board, posting_id=posting_id)

    # ---------------------------------------------------------------- fields

    def _fields(
        self, item: Mapping, *, posting_id: str, title: str
    ) -> tuple[dict, list[FieldEvidenceRecord], list[dict]]:
        """Extract the observation fields plus their locator evidence.

        Returns ``(fields, evidence, refused)``.  Values the provider did
        not send are absent — never defaulted (02 §22).
        """
        fields: dict = {}
        evidence: list[FieldEvidenceRecord] = []
        refused: list[dict] = []

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

        put("source_job_id", posting_id, "id")
        put("title", title, "title")
        put("company", self.config.company_name, "company_name", kind="binding_config")
        put("careers_url", self.config.careers_url, "careers_url", kind="binding_config")

        description, description_locator = _description(item)
        put("description", description, description_locator)

        locations = _locations(item)
        put("locations", locations, "location+secondaryLocations")

        put("department", _text(item.get("department")), "department")
        put("team", _text(item.get("team")), "team")
        # the provider's own wording, unmapped: normalization owns the
        # vocabulary (01 §33)
        put("employment_type", _text(item.get("employmentType")), "employmentType")

        workplace = _text(item.get("workplaceType"))
        if workplace is not None:
            if workplace in _WORKPLACE_TYPES:
                put("workplace_type", workplace, "workplaceType")
            else:
                refused.append({"field": "workplaceType", "excerpt": _excerpt(workplace)})

        is_remote = item.get("isRemote")
        remote = False
        if isinstance(is_remote, bool):
            put("is_remote", is_remote, "isRemote")
            remote = is_remote
        elif is_remote is not None:
            refused.append({"field": "isRemote", "excerpt": _excerpt(is_remote)})
        if workplace == "Remote":
            remote = True
        if remote:
            put("job_location_type", "REMOTE", "isRemote+workplaceType")

        # publishedAt is the provider-stated *last publication* time; an
        # unusable value is dropped, never guessed (02 §22, 03 §50).
        put("posted_at", _utc_timestamp(item.get("publishedAt")), "publishedAt")

        address = item.get("address")
        postal = address.get("postalAddress") if isinstance(address, Mapping) else None
        if isinstance(postal, Mapping):
            put("address", _flat(postal), "address.postalAddress")

        compensation = item.get("compensation")
        if isinstance(compensation, Mapping):
            put(
                "salary",
                _text(compensation.get("scrapeableCompensationSalarySummary")),
                "compensation.scrapeableCompensationSalarySummary",
            )
            put(
                "compensation_summary",
                _text(compensation.get("compensationTierSummary")),
                "compensation.compensationTierSummary",
            )

        for name, field_name in (("jobUrl", "hosted_url"), ("applyUrl", "apply_url")):
            raw_url = item.get(name)
            if raw_url is None:
                continue
            reviewed = self._reviewed_link(raw_url, posting_id)
            if reviewed is None:
                refused.append({"field": name, "excerpt": _excerpt(raw_url)})
            else:
                put(field_name, reviewed, name)

        return fields, evidence, refused

    # --------------------------------------------------------------- failures

    def _failure(
        self,
        result: ValidatedResult,
        kind: FailureKind,
        detail: str,
        *,
        review: tuple | list = (),
    ) -> ParseOutcome:
        return ParseOutcome(
            kind=ParseOutcomeKind.FAILURE,
            review_evidence=tuple(review),
            failure=self._failure_record(result.envelope, kind, detail),
            # ACQ-09: a failure is as traceable as a success — the envelope
            # and validity evidence that produced it stay linked.
            evidence_refs=_evidence_refs(result),
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


def _refusal_review(refused: list[dict]) -> tuple[dict, ...]:
    """One typed review-evidence entry for a member's refused values."""
    if not refused:
        return ()
    reason = (
        "UNSAFE_URL_REFUSED"
        if any(entry["field"] in ("jobUrl", "applyUrl") for entry in refused)
        else "UNRECOGNIZED_VALUE_REFUSED"
    )
    return ({"reason": reason, "fields": list(refused)},)


def _description(item: Mapping) -> tuple[str | None, str]:
    """The one content document the posting carries, HTML first.

    Nothing is sanitized here: the host's single cleaning path owns that
    (01 §34).  A posting whose HTML member is empty falls back to the
    provider's plain-text variant; a posting with neither simply has no
    description (truthful — there is no detail endpoint to backfill from).
    """
    html_doc = _text(item.get("descriptionHtml"))
    if html_doc:
        return html_doc, "descriptionHtml"
    plain = _text(item.get("descriptionPlain"))
    if plain:
        return plain, "descriptionPlain"
    return None, ""


def _locations(item: Mapping) -> list[str]:
    """The posting's stated location set: primary text, then secondaries.

    ``location`` is the provider's composed primary text; each
    ``secondaryLocations[].location`` is another place the posting is open
    in.  The set stays ordered and de-duplicated exactly as stated (01
    §33.2); the structured ``address.postalAddress`` constituents are kept
    separately as field evidence, never merged into the text set.
    """
    locations: list[str] = []

    def add(value: Any) -> None:
        text = _text(value)
        if text and text not in locations:
            locations.append(text)

    add(item.get("location"))
    secondaries = item.get("secondaryLocations")
    if isinstance(secondaries, list):
        for entry in secondaries:
            if isinstance(entry, Mapping):
                add(entry.get("location"))
    return locations


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "AshbyAdapter",
    "AshbyConfig",
    "DEFAULT_API_BASE_URL",
    "JOB_PAGE_TEMPLATE",
    "MANIFEST",
    "MAX_BODY_BYTES",
    "MAX_TIMEOUT_S",
    "PROVIDER",
    "RECOGNIZED_API_VERSION",
    "STOP_POLICY",
]
