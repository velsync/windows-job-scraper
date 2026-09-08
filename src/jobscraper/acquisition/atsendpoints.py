"""Known ATS endpoint patterns (02 §12.1 "known ATS endpoint patterns", §32).

A single, versioned, data-only owner for the board/API URL shapes of the ATS
providers Slice 2 supports.  Both the origin resolver (S2.1) and the
fingerprint classifier (S2.4) consume this table, so "what counts as a
Greenhouse job URL" is decided in exactly one reviewed place.

The table never invents identifiers: a job-level match requires the job id to
be present in the URL itself, and a board-only URL never yields a job id
(02 §31: a generic careers/home/apply URL alone must not identify a job).
No I/O, no database, no policy decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Bumped whenever a pattern is added/changed; recorded with every match so a
#: historical resolution stays reproducible (ARC-10).
ENDPOINT_RULES_VERSION = 1

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-_]*[a-z0-9])?(:\d{2,5})?$", re.IGNORECASE)

#: Path segments that are route words, never a board or job token.  Guards
#: against reading ``/v1/boards/acme/jobs`` as board ``v1``.
_RESERVED_SEGMENTS = frozenset(
    {
        "jobs", "job", "postings", "posting", "api", "v0", "v1", "boards",
        "board", "company", "companies", "search", "all", "list", "apply",
        "application", "applications", "careers", "index", "job-board",
        "posting-api", "departments", "offices", "teams", "content",
    }
)


@dataclass(frozen=True)
class AtsEndpointSpec:
    """One provider's public board + API shapes.

    ``kind`` is ``HOSTED`` (the public board page a human opens) or ``API``
    (the provider's structured endpoint).  Every pattern exposes a named
    ``board`` group; job patterns additionally expose ``job_id``.
    """

    provider: str
    display_name: str
    host_suffixes: tuple[str, ...]
    hosted_hosts: tuple[str, ...]
    api_hosts: tuple[str, ...]
    job_patterns: tuple[tuple[str, str], ...]
    board_patterns: tuple[tuple[str, str], ...]
    #: public, job-specific board page a user should be sent to (PROD-06)
    job_url_template: str
    list_url_template: str
    detail_url_template: str
    #: JSON member of a list response carrying the items (used by adapters)
    items_key: str = "jobs"

    def hosts(self) -> tuple[str, ...]:
        return tuple(sorted({*self.hosted_hosts, *self.api_hosts}))


@dataclass(frozen=True)
class AtsMatch:
    """Outcome of matching one URL against the table (never partial-trusted)."""

    provider: str | None
    board: str | None
    job_id: str | None
    kind: str | None
    host: str | None
    application_url: str | None
    strength: str = "NONE"
    pattern_id: str | None = None
    rules_version: int = ENDPOINT_RULES_VERSION
    evidence: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def matched(self) -> bool:
        return self.provider is not None

    @property
    def job_specific(self) -> bool:
        return self.provider is not None and self.job_id is not None

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "board": self.board,
            "job_id": self.job_id,
            "kind": self.kind,
            "host": self.host,
            "application_url": self.application_url,
            "strength": self.strength,
            "pattern_id": self.pattern_id,
            "rules_version": self.rules_version,
            "evidence": [dict(item) for item in self.evidence],
        }


_NO_MATCH = AtsMatch(None, None, None, None, None, None)

_BOARD = r"(?P<board>[a-z0-9][a-z0-9_-]*)"

ATS_ENDPOINT_SPECS: tuple[AtsEndpointSpec, ...] = (
    AtsEndpointSpec(
        provider="GREENHOUSE",
        display_name="Greenhouse Job Board",
        host_suffixes=("greenhouse.io",),
        hosted_hosts=("boards.greenhouse.io", "job-boards.greenhouse.io"),
        api_hosts=("boards-api.greenhouse.io",),
        job_patterns=(
            ("HOSTED", rf"^/{_BOARD}/jobs/(?P<job_id>\d+)$"),
            ("API", rf"^/v1/boards/{_BOARD}/jobs/(?P<job_id>\d+)$"),
        ),
        board_patterns=(
            ("HOSTED", rf"^/{_BOARD}$"),
            ("API", rf"^/v1/boards/{_BOARD}/jobs$"),
        ),
        job_url_template="https://boards.greenhouse.io/{board}/jobs/{job_id}",
        list_url_template="https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
        detail_url_template="https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}",
    ),
    AtsEndpointSpec(
        provider="LEVER",
        display_name="Lever Postings",
        host_suffixes=("lever.co",),
        hosted_hosts=("jobs.lever.co", "hk.jobs.lever.co", "eu.jobs.lever.co"),
        api_hosts=("api.lever.co",),
        job_patterns=(
            ("HOSTED", rf"^/{_BOARD}/(?P<job_id>[a-z0-9_-]{{8,40}})$"),
            ("API", rf"^/v0/postings/{_BOARD}/(?P<job_id>[a-z0-9_-]{{8,40}})$"),
        ),
        board_patterns=(
            ("HOSTED", rf"^/{_BOARD}$"),
            ("API", rf"^/v0/postings/{_BOARD}$"),
        ),
        job_url_template="https://jobs.lever.co/{board}/{job_id}",
        list_url_template="https://api.lever.co/v0/postings/{board}",
        detail_url_template="https://api.lever.co/v0/postings/{board}/{job_id}",
        items_key="",  # the Lever postings endpoint returns a bare array
    ),
    AtsEndpointSpec(
        provider="ASHBY",
        display_name="Ashby Job Board",
        host_suffixes=("ashbyhq.com",),
        hosted_hosts=("jobs.ashbyhq.com", "apps.ashbyhq.com"),
        api_hosts=("api.ashbyhq.com",),
        job_patterns=(
            ("HOSTED", rf"^/{_BOARD}/(?P<job_id>[a-z0-9_-]{{8,64}})$"),
            ("API", rf"^/posting-api/job-board/{_BOARD}/job/(?P<job_id>[a-z0-9_-]{{8,64}})$"),
        ),
        board_patterns=(
            ("HOSTED", rf"^/{_BOARD}$"),
            ("API", rf"^/posting-api/job-board/{_BOARD}/?$"),
        ),
        job_url_template="https://jobs.ashbyhq.com/{board}/{job_id}",
        list_url_template="https://api.ashbyhq.com/posting-api/job-board/{board}",
        detail_url_template="https://api.ashbyhq.com/posting-api/job-board/{board}/job/{job_id}",
    ),
)

_SPECS_BY_PROVIDER = {spec.provider: spec for spec in ATS_ENDPOINT_SPECS}
_COMPILED: dict[tuple[str, str], re.Pattern[str]] = {}


def spec_for_provider(provider: str) -> AtsEndpointSpec:
    """Exact lookup of one provider spec (KeyError for an unknown provider)."""
    return _SPECS_BY_PROVIDER[provider]


def _compiled(key: str, pattern: str) -> re.Pattern[str]:
    compiled_key = (key, pattern)
    compiled = _COMPILED.get(compiled_key)
    if compiled is None:
        compiled = re.compile(pattern, re.IGNORECASE)
        _COMPILED[compiled_key] = compiled
    return compiled


def _segment_ok(value: str | None) -> bool:
    return bool(value) and value.lower() not in _RESERVED_SEGMENTS


def _split_url(url: str) -> tuple[str, str, str] | None:
    """Return ``(scheme, host, path)`` or None when the URL is unusable.

    Anything with embedded credentials, a non-http(s) scheme, control
    characters or whitespace is refused here rather than being "cleaned" — the
    caller records the refusal as evidence.  Query and fragment are dropped
    before path matching, so a tracking query never changes an endpoint match
    (02 §31 tracking-parameter removal stays owned by :mod:`net.urlnorm`).
    """
    if not isinstance(url, str) or not url.strip():
        return None
    candidate = url.strip()
    if any(ord(ch) < 33 for ch in candidate) or any(ch.isspace() for ch in candidate):
        return None
    if not _SCHEME_RE.match(candidate):
        return None
    rest = candidate.split("://", 1)[1]
    if re.match(r"^[^/?#]*@", rest):  # userinfo -> credentials embedded in URL
        return None
    authority, sep, remainder = rest.partition("/")
    path = ("/" + remainder) if sep else "/"
    path = path.split("?", 1)[0].split("#", 1)[0]
    host = authority.lower()
    if _port_like(host):
        host = host.rsplit(":", 1)[0]
    if not host or not _HOST_RE.match(authority.lower()):
        return None
    return candidate.split("://", 1)[0].lower(), host, path


def _port_like(authority: str) -> bool:
    _host, sep, port = authority.rpartition(":")
    return bool(sep) and port.isdigit()


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    """Label-boundary suffix match (``greenhouse.io.evil.test`` never matches)."""
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _host_evidence(host: str, spec: AtsEndpointSpec, strength: str) -> dict:
    return {
        "pattern_id": f"{spec.provider}:host",
        "field": "host",
        "value": host,
        "strength": strength,
    }


def identify_url(url: str, *, spec: AtsEndpointSpec | None = None) -> AtsMatch:
    """Match one URL against the ATS endpoint table.

    Unmatched or unusable URLs come back with every identifier ``None`` and no
    evidence — the table never guesses a provider from an unknown host and
    never fabricates an id that the URL does not contain.
    """
    split = _split_url(url)
    if split is None:
        return _NO_MATCH
    _scheme, host, path = split
    for candidate in ((spec,) if spec is not None else ATS_ENDPOINT_SPECS):
        if not _host_matches(host, candidate.host_suffixes):
            continue
        if host in candidate.api_hosts:
            kinds: tuple[str, ...] = ("API", "HOSTED")
            host_strength = "STRONG"
        elif host in candidate.hosted_hosts:
            kinds = ("HOSTED", "API")
            host_strength = "STRONG"
        else:
            kinds = ()  # known suffix, unknown subdomain: a host signal only
            host_strength = "WEAK"
        for kind in kinds:
            for pattern_kind, pattern in candidate.job_patterns:
                if pattern_kind != kind:
                    continue
                match = _compiled(f"{candidate.provider}:JOB", pattern).match(path)
                if match is None:
                    continue
                groups = match.groupdict()
                board, job_id = groups.get("board"), groups.get("job_id")
                if not _segment_ok(board) or not _segment_ok(job_id):
                    continue
                return AtsMatch(
                    provider=candidate.provider,
                    board=board,
                    job_id=job_id,
                    kind=kind,
                    host=host,
                    application_url=candidate.job_url_template.format(
                        board=board, job_id=job_id
                    ),
                    strength="JOB",
                    pattern_id=f"{candidate.provider}:{kind}:job",
                    evidence=(
                        {
                            "pattern_id": f"{candidate.provider}:{kind}:job",
                            "field": "path",
                            "value": path,
                            "strength": "JOB",
                        },
                        _host_evidence(host, candidate, host_strength),
                    ),
                )
            for pattern_kind, pattern in candidate.board_patterns:
                if pattern_kind != kind:
                    continue
                match = _compiled(f"{candidate.provider}:BOARD", pattern).match(path)
                if match is None:
                    continue
                board = match.groupdict().get("board")
                if not _segment_ok(board):
                    continue
                return AtsMatch(
                    provider=candidate.provider,
                    board=board,
                    job_id=None,
                    kind=kind,
                    host=host,
                    application_url=None,
                    strength="BOARD",
                    pattern_id=f"{candidate.provider}:{kind}:board",
                    evidence=(
                        {
                            "pattern_id": f"{candidate.provider}:{kind}:board",
                            "field": "path",
                            "value": path,
                            "strength": "BOARD",
                        },
                        _host_evidence(host, candidate, host_strength),
                    ),
                )
        if not kinds:
            return AtsMatch(
                provider=candidate.provider,
                board=None,
                job_id=None,
                kind="HOST_PATTERN",
                host=host,
                application_url=None,
                strength="HOST_ONLY",
                evidence=(_host_evidence(host, candidate, "WEAK"),),
            )
    return _NO_MATCH


__all__ = [
    "ATS_ENDPOINT_SPECS",
    "ENDPOINT_RULES_VERSION",
    "AtsEndpointSpec",
    "AtsMatch",
    "identify_url",
    "spec_for_provider",
]
