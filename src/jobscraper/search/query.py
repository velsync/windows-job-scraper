"""Job search queries (01 §45): BM25 when FTS5 is active, honest fallback.

Read-model only — never writes canonical state.  Structured filters
(listing status, company, source, remote, discovery window) are applied in
SQL outside FTS; only the free-text ranking is delegated to FTS5 (BM25,
lower is better) or to a substring scan under ``SUBSTRING_FALLBACK``.

Both modes search exactly the same corpus — the ``job_search_docs``
snapshots (which the FTS5 virtual table mirrors) — so the fallback never
claims more than FTS would find.

Capability honesty: the response always carries the effective mode; scores
are present only when FTS5/BM25 really produced them.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from jobscraper.search.capability import (
    SEARCH_MODE_FTS5,
    SEARCH_MODE_SUBSTRING,
    read_capability,
)
from jobscraper.search.provision import (
    FTS_TABLE,
    fts_table_present,
    triggers_exist,
)
from jobscraper.timeutil.rfc3339 import parse_rfc3339, to_rfc3339

LISTING_STATUSES = ("ACTIVE", "UNCERTAIN", "EXPIRED", "CLOSED", "WITHDRAWN", "ANY")
MAX_LIMIT = 100
DEFAULT_LIMIT = 20

#: Columns of the searchable document, shared by FTS5 and the fallback.
_DOC_TEXT_COLUMNS = ("d.title", "d.company", "d.description_text",
                     "d.locations_text", "d.fact_text")


@dataclass(frozen=True)
class SearchHit:
    job_id: str
    title: str
    company: str | None
    locations: tuple[str, ...]
    posted_at: str | None
    listing_status: str
    score: float | None  # BM25 (lower = better); None in substring mode


@dataclass(frozen=True)
class SearchResult:
    query: str
    mode: str
    warning: str | None
    filters: dict
    total: int
    limit: int
    offset: int
    hits: tuple[SearchHit, ...]


def _effective_capability(conn: sqlite3.Connection) -> tuple[str, str | None]:
    """The effective search mode, degrading honestly when objects are gone.

    A recorded ``FTS5_ACTIVE`` must not be claimed unless the FTS5 table AND
    its sync triggers really exist: a table without its triggers silently
    serves a stale index, which is exactly the BM25-is-not-really-active
    situation 01 §45 forbids claiming.  Either missing ⇒ substring fallback
    with an explicit warning.  With no capability row at all, the presence of
    the complete provisioned surface is the ground truth.
    """
    complete = fts_table_present(conn) and triggers_exist(conn)
    row = read_capability(conn)
    if row is not None and row["mode"] == SEARCH_MODE_FTS5:
        if complete:
            return SEARCH_MODE_FTS5, None
        return SEARCH_MODE_SUBSTRING, (
            "FTS5 was recorded active but its index objects are missing or"
            " incomplete — restart the service so provisioning rebuilds the"
            " index; substring fallback is active meanwhile."
        )
    if row is not None:
        return row["mode"], row["warning"]
    if complete:
        return SEARCH_MODE_FTS5, None
    return SEARCH_MODE_SUBSTRING, (
        "Search is not provisioned yet — start the service once to provision"
        " FTS5; substring fallback is active meanwhile."
    )


def _terms(query: str) -> list[str]:
    """Whitespace terms with surrounding punctuation trimmed.

    Every term becomes a quoted FTS5 phrase, so quotes and operators in the
    user's input are literal text, never query syntax.
    """
    terms: list[str] = []
    for raw in re.split(r"\s+", (query or "").strip()):
        term = raw.strip(".,;:!?()[]{}<>\"'")
        if term:
            terms.append(term)
    return terms


def _fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _validate_listing_status(listing_status: str) -> str | None:
    if listing_status is None or listing_status == "ANY":
        return None
    if listing_status not in LISTING_STATUSES:
        raise ValueError(
            f"listing_status must be one of {sorted(LISTING_STATUSES)}"
        )
    return listing_status


def _canonical_timestamp(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        return to_rfc3339(parse_rfc3339(value))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {value!r}") from exc


def _job_filters(listing_status, company_id, source_id, remote_only, after, before):
    """Structured (non-FTS) predicate list over canonical ``jobs j``."""
    clauses: list[str] = []
    values: list[object] = []
    if listing_status is not None:
        clauses.append("j.listing_status = ?")
        values.append(listing_status)
    if company_id is not None:
        clauses.append("j.company_id = ?")
        values.append(company_id)
    if source_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM job_sources js"
            " WHERE js.job_id = j.id AND js.source_id = ?)"
        )
        values.append(source_id)
    if remote_only:
        clauses.append(
            "(j.remote_worldwide = 1 OR EXISTS (SELECT 1 FROM job_locations l"
            " WHERE l.job_id = j.id AND l.remote = 1))"
        )
    if after is not None:
        clauses.append("j.discovered_at >= ?")
        values.append(after)
    if before is not None:
        clauses.append("j.discovered_at <= ?")
        values.append(before)
    return clauses, values


def _hit_from_row(conn: sqlite3.Connection, row: sqlite3.Row, score) -> SearchHit:
    return SearchHit(
        job_id=row["job_id"],
        title=row["title"],
        company=row["company_name"],
        locations=_locations_of(conn, row["job_id"]),
        posted_at=row["posted_at"],
        listing_status=row["listing_status"],
        score=score,
    )


def search_jobs(
    conn: sqlite3.Connection,
    *,
    query: str,
    listing_status: str = "ACTIVE",
    company_id: str | None = None,
    source_id: str | None = None,
    remote_only: bool = False,
    discovered_after: str | None = None,
    discovered_before: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """Search indexed jobs; structured filters live outside FTS.

    Raises ``ValueError`` for an invalid status or timestamp.
    """
    terms = _terms(query)
    status_filter = _validate_listing_status(listing_status)
    after = _canonical_timestamp(discovered_after)
    before = _canonical_timestamp(discovered_before)
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    filters = {
        "listing_status": listing_status,
        "company_id": company_id,
        "source_id": source_id,
        "remote_only": bool(remote_only),
        "discovered_after": after,
        "discovered_before": before,
    }
    mode, warning = _effective_capability(conn)
    if not terms:
        return SearchResult(query, mode, warning, filters, 0, limit, offset, ())

    filter_clauses, filter_values = _job_filters(
        status_filter, company_id, source_id, remote_only, after, before
    )

    if mode == SEARCH_MODE_FTS5:
        return _fts_search(
            conn, query, terms, filter_clauses, filter_values,
            filters, limit, offset, warning,
        )
    return _substring_search(
        conn, query, terms, filter_clauses, filter_values,
        filters, limit, offset, warning,
    )


def _fts_search(
    conn, query, terms, filter_clauses, filter_values, filters, limit, offset,
    warning,
) -> SearchResult:
    match = " AND ".join(_fts_phrase(term) for term in terms)
    where_parts = [f"{FTS_TABLE} MATCH ?"] + list(filter_clauses)
    where_sql = " WHERE " + " AND ".join(where_parts)
    params = [match] + list(filter_values)
    from_sql = (
        f"FROM {FTS_TABLE} JOIN jobs j ON j.id = {FTS_TABLE}.job_id"
        " LEFT JOIN companies c ON c.id = j.company_id"
    )
    total = int(
        conn.execute(f"SELECT COUNT(*) {from_sql} {where_sql}", params).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT j.id AS job_id, j.title AS title, j.posted_at AS posted_at,"
        f" j.listing_status AS listing_status, c.name AS company_name,"
        f" bm25({FTS_TABLE}) AS score"
        f" {from_sql} {where_sql}"
        f" ORDER BY score, j.id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    hits = tuple(
        _hit_from_row(conn, row, float(row["score"])) for row in rows
    )
    return SearchResult(query, SEARCH_MODE_FTS5, warning, filters, total, limit, offset, hits)


def _substring_search(
    conn, query, terms, filter_clauses, filter_values, filters, limit, offset,
    warning,
) -> SearchResult:
    per_term = " OR ".join(
        f"({col} LIKE ? ESCAPE '\\')" for col in _DOC_TEXT_COLUMNS
    )
    like_sql = " AND ".join(f"({per_term})" for _ in terms)
    where_sql = " WHERE " + " AND ".join(list(filter_clauses) + [like_sql])
    like_values: list[object] = []
    for term in terms:
        pattern = _like_pattern(term)
        like_values.extend([pattern] * len(_DOC_TEXT_COLUMNS))
    params = list(filter_values) + like_values
    from_sql = (
        "FROM jobs j JOIN job_search_docs d ON d.job_id = j.id"
        " LEFT JOIN companies c ON c.id = j.company_id"
    )
    total = int(
        conn.execute(f"SELECT COUNT(*) {from_sql} {where_sql}", params).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT j.id AS job_id, j.title AS title, j.posted_at AS posted_at,"
        f" j.listing_status AS listing_status, c.name AS company_name"
        f" {from_sql} {where_sql}"
        f" ORDER BY j.last_seen_at DESC, j.id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    hits = tuple(_hit_from_row(conn, row, None) for row in rows)
    return SearchResult(
        query, SEARCH_MODE_SUBSTRING, warning, filters, total, limit, offset, hits
    )


def _locations_of(conn: sqlite3.Connection, job_id: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT raw_text FROM job_locations WHERE job_id = ?"
        " ORDER BY raw_text, rowid",
        (job_id,),
    ).fetchall()
    return tuple(re.sub(r"[ \t\n\r]+", " ", str(r["raw_text"] or "")).strip() for r in rows)


__all__ = [
    "DEFAULT_LIMIT",
    "LISTING_STATUSES",
    "MAX_LIMIT",
    "SearchHit",
    "SearchResult",
    "search_jobs",
]
