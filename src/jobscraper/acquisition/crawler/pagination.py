"""Restart-safe bounded pagination/no-progress guard."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from jobscraper.adapters.contract import StopPolicy
from .canonicalize import crawl_identity

_GUARD_VERSION = 1
_MAX_TRACKED_SIGNATURES = 256


def _hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaginationGuardState:
    version: int = _GUARD_VERSION
    seen_cursor_hashes: tuple[str, ...] = ()
    seen_url_identities: tuple[str, ...] = ()
    seen_page_hashes: tuple[str, ...] = ()
    seen_job_set_hashes: tuple[str, ...] = ()
    consecutive_empty_pages: int = 0
    consecutive_no_new_jobs_pages: int = 0
    duplicate_pages: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "seen_cursor_hashes": list(self.seen_cursor_hashes),
                "seen_url_identities": list(self.seen_url_identities),
                "seen_page_hashes": list(self.seen_page_hashes),
                "seen_job_set_hashes": list(self.seen_job_set_hashes),
                "consecutive_empty_pages": self.consecutive_empty_pages,
                "consecutive_no_new_jobs_pages": self.consecutive_no_new_jobs_pages,
                "duplicate_pages": self.duplicate_pages,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "PaginationGuardState":
        if raw in (None, "", "{}"):
            return cls()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid pagination guard state") from exc
        if not isinstance(data, dict) or int(data.get("version", -1)) != _GUARD_VERSION:
            raise ValueError("unsupported pagination guard state version")
        def items(name: str) -> tuple[str, ...]:
            value = data.get(name, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"invalid {name}")
            return tuple(value[-_MAX_TRACKED_SIGNATURES:])
        def count(name: str) -> int:
            value = data.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid {name}")
            return value
        return cls(
            version=_GUARD_VERSION,
            seen_cursor_hashes=items("seen_cursor_hashes"),
            seen_url_identities=items("seen_url_identities"),
            seen_page_hashes=items("seen_page_hashes"),
            seen_job_set_hashes=items("seen_job_set_hashes"),
            consecutive_empty_pages=count("consecutive_empty_pages"),
            consecutive_no_new_jobs_pages=count("consecutive_no_new_jobs_pages"),
            duplicate_pages=count("duplicate_pages"),
        )


@dataclass(frozen=True)
class PaginationSignature:
    next_cursor_hash: str | None = None
    next_url_identity: str | None = None
    page_hash: str | None = None
    job_set_hash: str | None = None
    observations_added: int = 0
    recognized_empty: bool = False

    @classmethod
    def from_values(
        cls,
        *,
        next_cursor: str | None = None,
        next_url: str | None = None,
        page_hash: str | None = None,
        job_ids: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None = None,
        observations_added: int = 0,
        recognized_empty: bool = False,
    ) -> "PaginationSignature":
        next_url_identity = None
        if next_url:
            try:
                next_url_identity = crawl_identity(next_url)
            except ValueError:
                next_url_identity = None
        job_set_hash = None
        if job_ids is not None:
            canonical = json.dumps(sorted(str(v) for v in job_ids), separators=(",", ":"))
            job_set_hash = _hash(canonical)
        return cls(
            next_cursor_hash=_hash(next_cursor),
            next_url_identity=next_url_identity,
            page_hash=page_hash,
            job_set_hash=job_set_hash,
            observations_added=max(0, int(observations_added)),
            recognized_empty=bool(recognized_empty),
        )


class PaginationStopKind(Enum):
    CONTINUE = "CONTINUE"
    TERMINAL = "TERMINAL"
    PAGINATION_LOOP = "PAGINATION_LOOP"
    DUPLICATE_PAGE = "DUPLICATE_PAGE"
    EMPTY_LIMIT = "EMPTY_LIMIT"
    NO_PROGRESS_LIMIT = "NO_PROGRESS_LIMIT"


@dataclass(frozen=True)
class PaginationDecision:
    kind: PaginationStopKind
    reason: str
    failure_kind: str | None = None


def _append_bounded(values: tuple[str, ...], value: str | None) -> tuple[str, ...]:
    if value is None:
        return values
    return (*values, value)[-_MAX_TRACKED_SIGNATURES:]


def advance_guard(
    state: PaginationGuardState,
    signature: PaginationSignature,
    stop_policy: StopPolicy,
) -> tuple[PaginationGuardState, PaginationDecision]:
    if state.version != _GUARD_VERSION:
        raise ValueError("unsupported pagination guard version")

    cursor_loop = bool(signature.next_cursor_hash and signature.next_cursor_hash in state.seen_cursor_hashes)
    url_loop = bool(signature.next_url_identity and signature.next_url_identity in state.seen_url_identities)
    repeated_page = bool(signature.page_hash and signature.page_hash in state.seen_page_hashes)
    repeated_jobs = bool(signature.job_set_hash and signature.job_set_hash in state.seen_job_set_hashes)

    duplicate_pages = state.duplicate_pages + (1 if repeated_page or repeated_jobs else 0)
    empty_pages = state.consecutive_empty_pages + 1 if signature.recognized_empty else 0
    no_new = state.consecutive_no_new_jobs_pages + 1 if signature.observations_added == 0 else 0

    new_state = PaginationGuardState(
        version=_GUARD_VERSION,
        seen_cursor_hashes=_append_bounded(state.seen_cursor_hashes, signature.next_cursor_hash),
        seen_url_identities=_append_bounded(state.seen_url_identities, signature.next_url_identity),
        seen_page_hashes=_append_bounded(state.seen_page_hashes, signature.page_hash),
        seen_job_set_hashes=_append_bounded(state.seen_job_set_hashes, signature.job_set_hash),
        consecutive_empty_pages=empty_pages,
        consecutive_no_new_jobs_pages=no_new,
        duplicate_pages=duplicate_pages,
    )

    if cursor_loop or url_loop:
        return new_state, PaginationDecision(
            PaginationStopKind.PAGINATION_LOOP,
            "repeated cursor" if cursor_loop else "repeated normalized next URL",
            "PAGINATION_LOOP",
        )
    if (repeated_page or repeated_jobs) and duplicate_pages >= max(1, int(stop_policy.max_duplicate_pages)):
        return new_state, PaginationDecision(
            PaginationStopKind.DUPLICATE_PAGE,
            "duplicate page hash" if repeated_page else "duplicate job set",
            "DUPLICATE_PAGE",
        )
    if empty_pages >= max(1, int(stop_policy.max_consecutive_empty_pages)):
        return new_state, PaginationDecision(PaginationStopKind.EMPTY_LIMIT, "consecutive empty-page stop policy")
    if no_new >= max(1, int(stop_policy.max_consecutive_no_new_jobs_pages)):
        return new_state, PaginationDecision(PaginationStopKind.NO_PROGRESS_LIMIT, "consecutive no-new-jobs stop policy")
    return new_state, PaginationDecision(PaginationStopKind.CONTINUE, "progress within stop policy")


__all__ = [
    "PaginationDecision", "PaginationGuardState", "PaginationSignature", "PaginationStopKind", "advance_guard",
]
