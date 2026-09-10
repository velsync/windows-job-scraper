"""Built-in Lever Postings API adapter (S2.6).

A **code adapter** (02 §9.2) for the public Lever postings API, the second of
the initial production providers (02 §12.3).  Like the Greenhouse adapter it
serves every Lever site through one reviewed identity (02 §8.2): the site
token is pinned in the immutable binding revision, so a single adapter
definition covers thousands of employers.

Contract surface (02 ACQ-02):

* ``ENUMERATE`` → ``GET {api}/v0/postings/{board}?mode=json&skip=N&limit=M``.
  Lever returns a **bare JSON array** with *no declared total*, paginated by
  ``skip``/``limit``.  A single page therefore cannot prove membership on its
  own: a page that fills the requested ``limit`` is ``continuation_required``
  and the adapter proposes a bounded offset cursor (02 §19); only a short
  page, or a recognized empty page, is terminal.  Claiming COMPLETE on a full
  first page would grant absence authority over postings never fetched
  (ACQ-03, RUN-13) — the one direction this adapter must never take;
* ``DETAIL`` → ``GET {api}/v0/postings/{board}/{posting_id}``, planned only
  from the **pinned site token plus a validated source-native id**.  A URL,
  host or site found in page content is never a fetch target (ACQ-04);
* ``HEALTH``/``SMOKE`` → the postings endpoint, parsed for recognition only;
  they never emit observations;
* ``CRAWL``/``DISCOVER`` are explicitly unsupported: crawl breadth is ROAD-04
  and discovery probes belong to the generic discovery binding.

Provider semantics adapted (not copied) from S2.5:

* the offset cursor is scoped to the run source plan that produced it, so a
  later run always re-enumerates the whole site from ``skip=0`` — the
  coverage generation is ``AUTHORITATIVE_FULL_SOURCE``, never "since the
  last run";
* loop/trap protection (02 §19): a continuation page carrying exactly the
  same posting-id set as the page before it is a repeated page, and the
  adapter stops proposing cursors instead of walking a server that ignores
  ``skip``; the outcome stays non-terminal;
* a page with a rejected listed member is ``PARTIAL`` and proposes **no**
  continuation either: the membership proof for this generation is already
  broken, and walking on would let a later clean short page finalize the
  same generation ``COMPLETE`` (ACQ-03 forbids absence inference from a
  PARTIAL unit; RUN-13).  Good observations from the page are still
  persisted — PARTIAL is not FAILURE;
* Lever's ``createdAt`` is the posting's *creation* time, which the provider
  does not state to be its publication time.  It is kept as a durable UTC
  evidence field (``posting_created_at``) and deliberately **not** mapped to
  ``posted_at`` (02 §22: values the provider did not state are never
  asserted).  ``workplaceType`` ``remote`` is the documented remote signal
  and travels through the ``job_location_type`` side-channel; ``hybrid`` and
  ``on-site`` are preserved verbatim as evidence only;
* the direct application link is derived from the versioned endpoint table
  plus the pinned site token and validated id (PROD-06).  The provider's own
  ``hostedUrl``/``applyUrl`` are kept as candidates/evidence only when the
  endpoint table reads them as **this site's, this posting's** hosted job
  URL: ``hostedUrl`` becomes the canonical-URL candidate that origin
  resolution consumes (02 §32), so a well-formed link about another site or
  posting would re-attribute the job.  Anything else is refused with review
  evidence, never used;
* required-field failures reject the item with structured review evidence and
  never invent values; a page whose items all fail is ``PARSE_MARKER_MISSING``,
  never ``SUCCESS_EMPTY`` (02 §22, ACQ-03);
* no I/O, no database, no policy authority — the adapter plans and parses
  only; the host validates, executes, fences and persists (ARC-04.4, ACQ-02);
* URL shapes come from :mod:`jobscraper.acquisition.atsendpoints`, the single
  versioned owner of "known ATS endpoint patterns" (02 §12.1, §32);
* durable timestamps are UTC RFC 3339 produced by :mod:`jobscraper.timeutil`
  (03 §50); an unusable provider timestamp is dropped, never guessed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from jobscraper.acquisition.atsendpoints import identify_url, spec_for_provider
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
from jobscraper.timeutil import UTC, to_rfc3339

MANIFEST = validate_manifest(
    {
        "id": "lever",
        "version": "1.0.0",
        "adapter_api_version": "1",
        # no "discover": discovery probes are the generic discovery binding's
        # task (02 §12.1).  no "incremental": the postings API exposes no
        # since parameter; skip/limit is offset paging within one full
        # enumeration, not an incremental feed.
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
PROVIDER = "LEVER"
_SPEC = spec_for_provider(PROVIDER)


def _origin_and_path(template: str) -> tuple[str, str]:
    """Split a versioned endpoint template into ``(origin, path)``."""
    normalized = normalize_url(template, drop_fragment=False)
    port = f":{normalized.port}" if normalized.port else ""
    return f"{normalized.scheme}://{normalized.host}{port}", normalized.path


#: Public API origin and the two path shapes, both taken from the endpoint
#: table so "what a Lever postings URL looks like" has exactly one owner.
DEFAULT_API_BASE_URL, LIST_PATH_TEMPLATE = _origin_and_path(_SPEC.list_url_template)
_, DETAIL_PATH_TEMPLATE = _origin_and_path(_SPEC.detail_url_template)
#: Public, job-specific hosted page a user is sent to (PROD-06).  Derived from
#: the endpoint table + pinned site token, never from scraped content.
JOB_PAGE_TEMPLATE = _SPEC.job_url_template
#: Offset paging bounds.  Lever documents ``limit`` with a default of 100 and
#: no larger page; the adapter never asks for more than that.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = MAX_PAGE_SIZE

#: Declared stop policy (02 §19).  Offset paging is bounded by pages *and* by
#: the same-page trap check; the request budget is the pages plus the bounded
#: detail enrichment they may spawn, and ``max_requests`` is sized so the
#: adapter cannot exceed its own declaration (see ``MAX_DETAIL_REQUESTS``).
STOP_POLICY = StopPolicy(
    max_pages=10,
    max_consecutive_empty_pages=1,
    max_consecutive_no_new_jobs_pages=1,
    max_duplicate_pages=1,
    max_runtime_s=300.0,
    max_requests=200,
)

#: Host policy caps the plan may not exceed (02 §11.1 / 04 §5.1); a config
#: above them is refused at construction instead of at execution time.
MAX_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 2_000_000

#: Hard bound on typed detail child work per enumeration page (ACQ-04),
#: sized so that even a walk that reaches ``max_pages`` — every page spawning
#: its full detail budget — stays inside the declared ``max_requests``.  The
#: adapter's stop policy is a promise the host bounds a run against; it must
#: not be one the adapter itself can exceed.
MAX_DETAIL_REQUESTS = (STOP_POLICY.max_requests - STOP_POLICY.max_pages) // STOP_POLICY.max_pages
DEFAULT_MAX_DETAIL_REQUESTS = MAX_DETAIL_REQUESTS
assert STOP_POLICY.max_pages * (1 + MAX_DETAIL_REQUESTS) <= STOP_POLICY.max_requests

#: Detail child work is claimed *after* the enumeration pages that produced
#: it, so listing coverage is proven before enrichment spends the budget.
DETAIL_TASK_PRIORITY = -5
DETAIL_TASK_DEPTH = 1

_BOARD_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Source-native posting id: the same shape the endpoint table matches for
#: LEVER job URLs (lowercase UUID-like tokens, 8..40 chars).  Strictness is
#: the security property: the value is interpolated into a detail URL.
_POSTING_ID_RE = re.compile(r"^[a-z0-9_-]{8,40}$")

#: Route words are never a site token or a posting id: accepting one would
#: let a config build ``/v0/postings/postings`` and read a reserved segment as
#: an employer.  Mirrors the guard in :mod:`jobscraper.acquisition.atsendpoints`.
_RESERVED_TOKENS = frozenset(
    {
        "postings", "posting", "boards", "board", "jobs", "job", "v0", "v1",
        "api", "search", "all", "list", "apply", "application", "applications",
        "careers", "index",
    }
)

_WORKPLACE_TYPES = frozenset({"unspecified", "on-site", "remote", "hybrid"})

_REVIEW_EXCERPT_CHARS = 120


@dataclass(frozen=True)
class LeverConfig:
    """Immutable binding-revision config snapshot for one Lever site.

    Every value is operator/host-provisioned and pinned with the binding
    revision (02 §8.3).  Nothing here may be widened by scraped content: the
    site token and the API origin are the only inputs used to build request
    URLs, and both are validated at construction.
    """

    #: Lever site token (``jobs.lever.co/{board}``, ``/v0/postings/{board}``).
    board: str
    #: API origin.  Defaults to the provider's public API; an operator may pin
    #: another origin (for example an offline fixture host), which the host's
    #: destination policy still validates per connection (04 §5.1).
    api_base_url: str = DEFAULT_API_BASE_URL
    #: Employer display name for this site.  The postings API does not carry
    #: the employer's name, so it is either declared here or left absent — it
    #: is never guessed from the site token.
    company_name: str | None = None
    #: Employer careers URL, when the operator knows it (01 §33.1 signal).
    careers_url: str | None = None
    #: ``True`` emits typed ``DETAIL`` child tasks for postings whose content
    #: the enumeration did not carry (the live API usually carries it inline).
    detail_fetch: bool = True
    #: Bound on detail child tasks per enumeration page (ACQ-04).
    max_detail_requests: int = DEFAULT_MAX_DETAIL_REQUESTS
    #: Offset page size (``limit``); 1..100.
    page_size: int = DEFAULT_PAGE_SIZE
    timeout_s: float = MAX_TIMEOUT_S
    max_bytes: int = MAX_BODY_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "board", _validated_board(self.board))
        object.__setattr__(self, "api_base_url", _validated_origin(self.api_base_url))
        object.__setattr__(self, "company_name", _validated_name(self.company_name))
        object.__setattr__(self, "careers_url", _validated_careers_url(self.careers_url))
        if not isinstance(self.detail_fetch, bool):
            raise ValueError("detail_fetch must be a boolean")
        _require_int(self.max_detail_requests, "max_detail_requests", 0, MAX_DETAIL_REQUESTS)
        _require_int(self.page_size, "page_size", 1, MAX_PAGE_SIZE)
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
    def from_mapping(cls, config: Mapping[str, Any]) -> "LeverConfig":
        """Typed construction from a binding-revision config snapshot.

        Unknown keys are refused rather than ignored: a config the adapter
        does not understand must not silently run with different semantics
        (02 §9: no host policy inside adapter manifests/config).
        """
        if not isinstance(config, Mapping):
            raise ValueError("lever binding config must be a mapping")
        unknown = sorted(str(key) for key in config if key not in _CONFIG_FIELD_NAMES)
        if unknown:
            raise ValueError(f"unknown lever binding config keys: {unknown}")
        return cls(**dict(config))


#: Accepted ``config_json`` members (a binding config the adapter does not
#: understand is refused, never partially applied).
_CONFIG_FIELD_NAMES = frozenset(
    config_field.name for config_field in dataclasses.fields(LeverConfig)
)


# ---------------------------------------------------------------------------
# Validation helpers (fail closed, never coerce silently)
# ---------------------------------------------------------------------------


def _require_int(value: Any, name: str, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be within {low}..{high}, got {value!r}")


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
            f"invalid Lever site token {value!r}: expected a slug matching "
            "[a-z0-9][a-z0-9_-]{0,63}"
        )
    if board in _RESERVED_TOKENS:
        raise ValueError(f"{board!r} is an endpoint route word, not a site token")
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


def _reviewed_hosted_url(value: Any, *, board: str, posting_id: str) -> str | None:
    """A provider-supplied link, accepted only when it is *this* posting's.

    Three gates, all host-owned: the URL must be safe http(s); the endpoint
    table — not the payload — must recognize it as a Lever hosted job URL;
    and the site token and posting id it names must equal the pinned site
    and the id being parsed.  A safe-looking URL on another host, another
    site or another posting is content, not a link candidate: origin
    resolution consumes this value as the canonical job URL (02 §32), so a
    foreign spelling here would re-attribute the job to an employer or a
    posting the host never fetched.

    Lever's ``applyUrl`` is the hosted job URL plus an ``/apply`` segment,
    which the endpoint table deliberately does not treat as job-specific
    (a form is not the posting).  It is accepted as an *evidence field* under
    the same site/id gate by matching its parent path.
    """
    safe = _safe_http_url(value)
    if safe is None:
        return None
    if _matches_this_posting(safe, board=board, posting_id=posting_id):
        return safe
    normalized = normalize_url(safe, drop_fragment=False)
    if normalized.path.rstrip("/").endswith("/apply") and not normalized.query:
        parent = f"{normalized.scheme}://{normalized.host}{normalized.path.rstrip('/')[: -len('/apply')]}"
        if _matches_this_posting(parent, board=board, posting_id=posting_id):
            return safe
    return None


def _matches_this_posting(url: str, *, board: str, posting_id: str) -> bool:
    """Endpoint-table recognition of ``url`` as *this* site's *this* posting."""
    match = identify_url(url, spec=_SPEC)
    if not match.job_specific or match.kind != "HOSTED":
        return False
    return match.board == board and match.job_id == posting_id


def _posting_id(value: Any) -> str | None:
    """A Lever source-native posting id, or ``None``.

    Only lowercase ``[a-z0-9_-]{8,40}`` is accepted — exactly what the
    endpoint table recognizes in a Lever job URL.  Anything else (uppercase,
    traversal, query, an embedded URL, a route word) is refused, never cleaned.
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


def _text_list(value: Any) -> list[str]:
    """Distinct, ordered, non-empty strings of one provider string array."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for entry in value:
        text = _text(entry)
        if text and text not in out:
            out.append(text)
    return out


def _epoch_ms_timestamp(value: Any) -> str | None:
    """Provider epoch-milliseconds → durable UTC RFC 3339 (03 §50), or ``None``.

    Lever states ``createdAt`` as an integer millisecond timestamp.  Strings,
    booleans, floats and out-of-range values are dropped, never coerced.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    try:
        return to_rfc3339(datetime.fromtimestamp(value / 1000, tz=UTC))
    except (ValueError, OverflowError, OSError):
        return None


def _excerpt(value: Any) -> str:
    if isinstance(value, str):
        return value[:_REVIEW_EXCERPT_CHARS]
    try:
        return json.dumps(value, sort_keys=True, default=str)[:_REVIEW_EXCERPT_CHARS]
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)[:_REVIEW_EXCERPT_CHARS]


def _json_payload(body: bytes) -> tuple[Any, str | None]:
    try:
        return json.loads(body.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"body is not valid JSON: {exc}"


def _evidence_refs(result: ValidatedResult) -> tuple[str, ...]:
    """ACQ-09 references to the durable evidence behind this outcome."""
    refs: list[str] = []
    for ref in (result.result_envelope_ref, result.validation_evidence_ref):
        if ref and ref not in refs:
            refs.append(ref)
    return tuple(refs)


def _ids_hash(observations: tuple[ObservationRecord, ...]) -> str:
    """Stable digest of one page's posting-id set (02 §19 repeated job set)."""
    ids = sorted({obs.source_job_id for obs in observations if obs.source_job_id})
    return hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()


#: the ``skip`` parameter exactly as this adapter itself writes it in
#: :meth:`LeverAdapter.plan` (no URL library: adapters stay pure parse/plan)
_SKIP_PARAM = re.compile(r"[?&]skip=(\d{1,9})(?:&|#|$)")


def _skip_from_url(url: str | None) -> int:
    """The ``skip`` offset the adapter itself planned, read back from its URL."""
    if not isinstance(url, str) or not url:
        return 0
    match = _SKIP_PARAM.search(url)
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class LeverAdapter:
    """plan / parse / next_cursor for one pinned Lever site."""

    manifest = MANIFEST

    #: Declared stop policy (02 §19), exposed so the host can bound a run
    #: against what this adapter itself claims.
    stop_policy = STOP_POLICY

    #: 03 §40 coverage barrier: this binding contract proves listing presence
    #: from the enumeration alone — the postings endpoint returns published
    #: postings with source-native ids — so detail completion is *not* part
    #: of the absence-authority barrier.  Detail work still has to be drained
    #: before the run may terminalize (ACQ-04 child work).
    listing_identity_sufficient = True

    def __init__(self, config: LeverConfig):
        self.config = config

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "LeverAdapter":
        """Registry construction from a binding-revision config snapshot."""
        return cls(LeverConfig.from_mapping(config or {}))

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
        elif kind is AdapterTaskKind.ENUMERATE:
            skip, _page = self._cursor_position(cursor, ctx)
            url = self._list_url(skip)
        elif kind in (AdapterTaskKind.HEALTH, AdapterTaskKind.SMOKE):
            url = self._list_url(0)
        else:
            raise ValueError(
                f"lever adapter does not support {kind.value} tasks "
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

    def _list_url(self, skip: int) -> str:
        path = LIST_PATH_TEMPLATE.format(board=self.config.board)
        return (
            f"{self.config.api_base_url}{path}"
            f"?mode=json&skip={int(skip)}&limit={self.config.page_size}"
        )

    def _detail_url(self, task: AdapterTask) -> str:
        """The detail target: pinned site token + validated source-native id.

        Only ``target_reference`` is read from the task payload.  A site, host
        or URL appearing in the payload (i.e. originating from content) is
        ignored by construction, so scraped content cannot re-point a fetch.
        """
        payload = task.payload or {}
        posting_id = _posting_id(payload.get("target_reference"))
        if posting_id is None:
            raise ValueError(
                "DETAIL task carries no usable source-native Lever posting id;"
                " refusing to plan a fetch (02 ACQ-04: a discovered reference is"
                " not I/O permission)"
            )
        return (
            f"{self.config.api_base_url}"
            f"{DETAIL_PATH_TEMPLATE.format(board=self.config.board, job_id=posting_id)}"
        )

    # ---------------------------------------------------------------- cursor

    def _cursor_position(self, cursor: CrawlCursor | None, ctx: Any) -> tuple[int, int]:
        """``(skip, page_index)`` for this plan, or ``(0, 0)`` for a fresh pass.

        The durable cursor row is keyed per binding, so it may carry the
        offset of an *earlier* run.  A cursor is honoured only when it was
        produced by the same run source plan (ACQ-09 pins); anything else
        restarts the full enumeration — coverage is full-source, never
        "since last time".
        """
        state = self._cursor_state(cursor, ctx)
        if state is None:
            return 0, 0
        return state["skip"], state["page"]

    def _cursor_state(self, cursor: CrawlCursor | None, ctx: Any) -> dict | None:
        if cursor is None or cursor.adapter_id != ADAPTER_ID:
            return None
        if cursor.cursor_schema_version != MANIFEST.cursor_schema_version:
            return None
        raw = cursor.state_json
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return None
        if not isinstance(state, Mapping):
            return None
        scope = getattr(ctx, "run_source_plan_id", None)
        if state.get("plan") != scope:
            return None
        skip, page = state.get("skip"), state.get("page")
        for value in (skip, page):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
        ids_hash = state.get("ids_hash")
        return {
            "skip": skip,
            "page": page,
            "ids_hash": ids_hash if isinstance(ids_hash, str) else None,
        }

    def next_cursor(
        self,
        task: AdapterTask,
        outcome: ParseOutcome,
        current_cursor: CrawlCursor | None,
        ctx: Any = None,
    ) -> CrawlCursor | None:
        """Propose the next offset page, or ``None``.

        ``None`` for anything that is not a full enumeration page (short page,
        recognized empty, failure), when the declared page bound is reached,
        and when the page repeats the previous page's posting-id set (a server
        ignoring ``skip``).  A ``None`` after a full page leaves the outcome
        ``continuation_required`` — bounded PARTIAL, never terminal (02 §19).
        """
        if task.kind is not AdapterTaskKind.ENUMERATE:
            return None
        if outcome.kind in (ParseOutcomeKind.FAILURE, ParseOutcomeKind.SUCCESS_EMPTY):
            return None
        if not outcome.continuation_required or not outcome.observations:
            return None
        if outcome.kind is ParseOutcomeKind.PARTIAL:
            # A page with a rejected listed member already broke the
            # membership proof for this generation (ACQ-03: absence inference
            # forbidden).  Proposing a continuation would let a later clean
            # short page terminalize the same generation as COMPLETE, laundering
            # the earlier rejection into absence authority.  Stop here; the
            # host records the bounded PARTIAL result.
            return None
        skip, page = self._cursor_position(current_cursor, ctx)
        previous = self._cursor_state(current_cursor, ctx)
        ids_hash = _ids_hash(outcome.observations)
        if previous is not None and previous["ids_hash"] == ids_hash:
            return None  # repeated job set: a trap, not progress
        if page + 1 >= STOP_POLICY.max_pages:
            return None  # declared bound reached: the host records PARTIAL
        state = {
            "plan": getattr(ctx, "run_source_plan_id", None),
            "skip": skip + self.config.page_size,
            "page": page + 1,
            "ids_hash": ids_hash,
        }
        return CrawlCursor(
            source_id=current_cursor.source_id if current_cursor else "",
            binding_id=current_cursor.binding_id if current_cursor else "",
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            cursor_schema_version=MANIFEST.cursor_schema_version,
            state_json=json.dumps(state, sort_keys=True, separators=(",", ":")),
            checkpoint_at="",
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
                f"lever adapter cannot parse {kind.value} tasks "
                "(02 ACQ-02: every supported task is explicit)"
            )
        payload, error = _json_payload(result.envelope.body)
        if error is not None:
            return self._failure(result, FailureKind.PARSE_MARKER_MISSING, error)
        if kind is AdapterTaskKind.ENUMERATE:
            return self._parse_list(payload, result)
        return self._parse_detail(payload, result, task)

    def _parse_probe(self, result: ValidatedResult) -> ParseOutcome:
        """HEALTH/SMOKE: recognize the minimum postings contract, no extraction.

        A top-level array is necessary but not sufficient evidence that the
        provider parser still recognizes the response.  For non-empty arrays
        the probe checks the same two required membership fields used by
        enumeration — source-native ``id`` and non-empty ``text`` — without
        extracting observations or proposing child work.
        """
        payload, error = _json_payload(result.envelope.body)
        if error is not None or not isinstance(payload, list):
            return self._failure(
                result,
                FailureKind.PARSE_MARKER_MISSING,
                error or "health probe did not find a postings array",
            )

        recognized = 0
        for item in payload:
            if (
                isinstance(item, Mapping)
                and _posting_id(item.get("id")) is not None
                and _text(item.get("text")) is not None
            ):
                recognized += 1
        rejected = len(payload) - recognized
        summary = {
            "reason": (
                "HEALTH_PROBE_PARTIAL_RECOGNITION"
                if rejected
                else "HEALTH_PROBE_RECOGNIZED"
            ),
            "listed_postings": len(payload),
            "recognized_postings": recognized,
            "rejected_postings": rejected,
        }

        if payload and recognized == 0:
            return self._failure(
                result,
                FailureKind.PARSE_MARKER_MISSING,
                f"all {len(payload)} health-probe postings failed required-field validation",
                review=(summary,),
            )

        return ParseOutcome(
            kind=(ParseOutcomeKind.PARTIAL if rejected else ParseOutcomeKind.SUCCESS_EMPTY),
            review_evidence=(summary,),
            evidence_refs=_evidence_refs(result),
        )

    # ------------------------------------------------------------ list parse

    def _parse_list(self, payload: Any, result: ValidatedResult) -> ParseOutcome:
        envelope = result.envelope
        if not isinstance(payload, list):
            review: dict = {"reason": "ITEMS_MARKER_MISSING"}
            if isinstance(payload, Mapping):
                review["top_level_keys"] = sorted(str(key) for key in payload)[:20]
            else:
                review["payload_type"] = type(payload).__name__
            return self._failure(
                result,
                FailureKind.PARSE_MARKER_MISSING,
                "response is not the bare postings array — the postings template changed",
                review=(review,),
            )
        skip = _skip_from_url(envelope.requested_url)
        refs = _evidence_refs(result)
        if not payload:
            # ACQ-03: a recognized empty postings page is success.  On the
            # first page it is an empty site; after a full page it is the end
            # of the enumeration.  Either way the host treats it as terminal.
            return ParseOutcome(
                kind=ParseOutcomeKind.SUCCESS_EMPTY,
                coverage_proposal={
                    "declared_total": None,
                    "skip": skip,
                    "limit": self.config.page_size,
                    "observed": 0,
                    "detail_tasks": 0,
                },
                evidence_refs=refs,
            )

        observations: list[ObservationRecord] = []
        review_items: list[dict] = []
        tasks: list[DiscoveredTask] = []
        rejected_members = 0
        for order, item in enumerate(payload):
            observation, item_review, task = self._list_observation(
                item, skip + order, envelope
            )
            if observation is not None:
                observations.append(observation)
            else:
                # The provider listed a member that could not be admitted to
                # the stable membership set.  Preserve good observations, but
                # never turn the resulting subset into absence authority.
                rejected_members += 1
            if item_review is not None:
                review_items.append(item_review)
            if task is not None:
                tasks.append(task)

        if not observations:
            return self._failure(
                result,
                FailureKind.PARSE_MARKER_MISSING,
                f"all {len(payload)} listed postings failed required-field validation",
                review=review_items,
            )

        budget = self.config.max_detail_requests if self.config.detail_fetch else 0
        if len(tasks) > budget:
            capped = tasks[budget:]
            tasks = tasks[:budget]
            review_items.append(
                {
                    "reason": "DETAIL_BUDGET_REACHED",
                    "bound": budget,
                    "deferred_posting_ids": [task.target_reference for task in capped][:50],
                }
            )

        # §19/ACQ-03/RUN-13: Lever declares no total.  A page that fills the
        # requested limit may be followed by more; only a short page proves
        # the enumeration ended.  A rejected listed member means the
        # membership proof is incomplete regardless of page length.
        page_full = len(payload) >= self.config.page_size
        return ParseOutcome(
            kind=(
                ParseOutcomeKind.PARTIAL
                if rejected_members > 0
                else ParseOutcomeKind.SUCCESS_WITH_JOBS
            ),
            observations=tuple(observations),
            discovered_tasks=tuple(tasks),
            review_evidence=tuple(review_items),
            continuation_required=page_full,
            coverage_proposal={
                "declared_total": None,
                "skip": skip,
                "limit": self.config.page_size,
                "page_full": page_full,
                "observed": len(observations),
                "rejected_members": rejected_members,
                "detail_tasks": len(tasks),
            },
            evidence_refs=refs,
        )

    def _list_observation(
        self, item: Any, order: int, envelope
    ) -> tuple[ObservationRecord | None, dict | None, DiscoveredTask | None]:
        if not isinstance(item, Mapping):
            return None, {"reason": "ITEM_NOT_AN_OBJECT", "order": order}, None
        posting_id = _posting_id(item.get("id"))
        title = _text(item.get("text"))
        if posting_id is None or title is None:
            return None, self._rejection(item, order, posting_id, title), None

        fields, evidence, field_review = self._fields(item, posting_id=posting_id, title=title)
        record = ObservationRecord(
            source_job_id=posting_id,
            raw_url=envelope.final_url,
            canonical_url_candidate=self._reviewed_link(item.get("hostedUrl"), posting_id),
            application_url_candidate=self._job_page_url(posting_id),
            fields=fields,
            field_evidence=tuple(evidence),
            source_rank_or_order=order,
        )
        task = None
        if self.config.detail_fetch and "description" not in fields:
            task = DiscoveredTask(
                kind="DETAIL",
                logical_key=f"lever:{self.config.board}:posting:{posting_id}",
                target_reference=posting_id,
                priority=DETAIL_TASK_PRIORITY,
                depth=DETAIL_TASK_DEPTH,
            )
        return record, field_review, task

    def _rejection(
        self, item: Mapping, order: int, posting_id: str | None, title: str | None
    ) -> dict:
        """Structured review evidence for one rejected item (02 §22)."""
        missing = [name for name, value in (("id", posting_id), ("text", title)) if value is None]
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
        for name in ("hostedUrl", "applyUrl"):
            raw_url = item.get(name)
            if raw_url is not None and (
                posting_id is None
                or self._reviewed_link(raw_url, posting_id) is None
            ):
                review[f"rejected_{name}_excerpt"] = _excerpt(raw_url)
        return review

    def _job_page_url(self, posting_id: str) -> str:
        """The direct application URL: endpoint table + pinned site + id."""
        return JOB_PAGE_TEMPLATE.format(board=self.config.board, job_id=posting_id)

    def _reviewed_link(self, value: Any, posting_id: str) -> str | None:
        """A provider link candidate for *this* posting on *this* site."""
        return _reviewed_hosted_url(value, board=self.config.board, posting_id=posting_id)

    # ---------------------------------------------------------- detail parse

    def _parse_detail(self, payload: Any, result: ValidatedResult, task: AdapterTask) -> ParseOutcome:
        envelope = result.envelope
        refs = _evidence_refs(result)
        if not isinstance(payload, Mapping):
            return self._failure(
                result,
                FailureKind.PARSE_MARKER_MISSING,
                f"detail response is not a posting object, got {type(payload).__name__}",
            )
        requested = _posting_id((task.payload or {}).get("target_reference"))
        posting_id = _posting_id(payload.get("id"))
        if requested is not None and posting_id is not None and requested != posting_id:
            # The response describes a different posting than the one asked
            # for.  Emitting an observation here would attribute content to a
            # posting the provider never returned (02 §22, ACQ-02).
            return ParseOutcome(
                kind=ParseOutcomeKind.FAILURE,
                failure=self._failure_record(
                    envelope,
                    FailureKind.INVALID_JOB_RECORD,
                    f"detail response describes posting {posting_id}, request asked for {requested}",
                ),
                closure_or_missing_evidence=(
                    {
                        "reason": "DETAIL_ID_MISMATCH",
                        "target_reference": requested,
                        "observed_id": posting_id,
                    },
                ),
                evidence_refs=refs,
            )

        title = _text(payload.get("text"))
        review: list[dict] = []
        if posting_id is None or title is None:
            missing = [name for name, value in (("id", posting_id), ("text", title)) if value is None]
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

        fields, evidence, field_review = self._fields(payload, posting_id=posting_id, title=title)
        if field_review is not None:
            review.append(field_review)
        observation = ObservationRecord(
            source_job_id=posting_id,
            raw_url=envelope.final_url,
            canonical_url_candidate=self._reviewed_link(payload.get("hostedUrl"), posting_id),
            application_url_candidate=self._job_page_url(posting_id),
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
        self, item: Mapping, *, posting_id: str, title: str
    ) -> tuple[dict, list[FieldEvidenceRecord], dict | None]:
        """Extract the observation fields plus their locator evidence.

        Returns ``(fields, evidence, review_or_None)``.  Values the provider
        did not send are absent — never defaulted (02 §22).
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
        put("title", title, "text")
        put("company", self.config.company_name, "company_name", kind="binding_config")
        put("careers_url", self.config.careers_url, "careers_url", kind="binding_config")

        description, description_locator = _description(item)
        put("description", description, description_locator)

        categories = item.get("categories")
        categories = categories if isinstance(categories, Mapping) else {}
        locations, locations_locator = _locations(categories)
        put("locations", locations, locations_locator)
        put("team", _text(categories.get("team")), "categories.team")
        put("department", _text(categories.get("department")), "categories.department")
        put("employment_type", _text(categories.get("commitment")), "categories.commitment")

        workplace = _text(item.get("workplaceType"))
        if workplace is not None:
            lowered = workplace.lower()
            if lowered in _WORKPLACE_TYPES:
                put("workplace_type", lowered, "workplaceType")
                if lowered == "remote":
                    put("job_location_type", "REMOTE", "workplaceType")
            else:
                refused.append({"field": "workplaceType", "excerpt": _excerpt(workplace)})

        country = _text(item.get("country"))
        if country is not None:
            put("country", country, "country")

        put("posting_created_at", _epoch_ms_timestamp(item.get("createdAt")), "createdAt")

        for name, field_name in (("hostedUrl", "hosted_url"), ("applyUrl", "apply_url")):
            raw_url = item.get(name)
            if raw_url is None:
                continue
            reviewed = self._reviewed_link(raw_url, posting_id)
            if reviewed is None:
                refused.append({"field": name, "excerpt": _excerpt(raw_url)})
            else:
                put(field_name, reviewed, name)

        review = None
        if refused:
            review = {
                "reason": "UNSAFE_URL_REFUSED" if any(
                    entry["field"] in ("hostedUrl", "applyUrl") for entry in refused
                ) else "UNRECOGNIZED_VALUE_REFUSED",
                "fields": refused,
            }
        return fields, evidence, review

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


def _description(item: Mapping) -> tuple[str | None, str]:
    """One HTML document from Lever's split content members, for the cleaner.

    ``description`` (opening + body HTML), then each ``lists`` entry as a
    heading plus its ``<li>`` content, then ``additional``.  Nothing is
    sanitized here: the host's single cleaning path owns that (01 §34).  A
    posting with only the ``*Plain`` variants falls back to those.
    """
    parts: list[str] = []
    locators: list[str] = []
    body = _text(item.get("description")) or _text(item.get("descriptionPlain"))
    if body:
        parts.append(body)
        locators.append("description" if _text(item.get("description")) else "descriptionPlain")
    lists = item.get("lists")
    if isinstance(lists, list):
        rendered = []
        for entry in lists:
            if not isinstance(entry, Mapping):
                continue
            heading = _text(entry.get("text"))
            content = _text(entry.get("content"))
            if not content:
                continue
            block = f"<ul>{content}</ul>"
            if heading:
                block = f"<h3>{html.escape(heading)}</h3>{block}"
            rendered.append(block)
        if rendered:
            parts.append("".join(rendered))
            locators.append("lists")
    additional = _text(item.get("additional")) or _text(item.get("additionalPlain"))
    if additional:
        parts.append(additional)
        locators.append("additional" if _text(item.get("additional")) else "additionalPlain")
    if not parts:
        return None, ""
    return "".join(parts), "+".join(locators)


def _locations(categories: Mapping) -> tuple[list[str], str]:
    """The posting's stated location set, richest first.

    1. ``categories.allLocations`` — every location the posting is open in;
    2. ``categories.location`` — the primary location text.

    Only one signal set is emitted so the canonical location *set* stays a
    set of distinct places (01 §33.2).
    """
    all_locations = _text_list(categories.get("allLocations"))
    if all_locations:
        return all_locations, "categories.allLocations"
    primary = _text(categories.get("location"))
    if primary:
        return [primary], "categories.location"
    return [], ""


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_MAX_DETAIL_REQUESTS",
    "DEFAULT_PAGE_SIZE",
    "DETAIL_TASK_DEPTH",
    "DETAIL_TASK_PRIORITY",
    "JOB_PAGE_TEMPLATE",
    "LeverAdapter",
    "LeverConfig",
    "MANIFEST",
    "MAX_BODY_BYTES",
    "MAX_DETAIL_REQUESTS",
    "MAX_PAGE_SIZE",
    "MAX_TIMEOUT_S",
    "PROVIDER",
    "STOP_POLICY",
]
