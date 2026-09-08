"""Host-owned, revision-checked search-document maintenance (S2.3, 01 §45).

The fenced canonical pipeline calls :func:`sync_search_doc` after a job's
canonical projection changes.  Maintenance is idempotent: the content hash
of the document (title/company/description/locations/fact text) is compared
against ``job_search_state.indexed_content_hash`` and nothing is written
when they match, so re-indexing can never duplicate or lose rows.

The plain ``job_search_docs`` row is the durable content source (it also
serves the ``SUBSTRING_FALLBACK`` mode); when FTS5 is provisioned, the sync
triggers mirror the row into the FTS5 table in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

#: Layout marker included in the indexed-content hash: a future layout change
#: forces re-indexing instead of silently reusing rows written in an older
#: shape.
SEARCH_DOC_LAYOUT = "search-doc-v1"

_WS_RUN = re.compile(r"[ \t\n\r]+")


def _fold(value: object) -> str:
    """Fold whitespace runs for index text (never changes meaning)."""
    return _WS_RUN.sub(" ", str(value or "")).strip()

#: Canonical fact labels surfaced today.  ``job_facts`` (ROAD-05) will feed
#: skills/contact fact text later; until then the index carries what the
#: canonical row already holds: employment type, experience level and the
#: remote-mode wording, when the source stated them.
_EMPLOYMENT_LABEL = {
    "FULL_TIME": "full-time",
    "PART_TIME": "part-time",
    "CONTRACT": "contract",
    "TEMPORARY": "temporary",
    "INTERNSHIP": "internship",
    "FREELANCE": "freelance",
    "SEASONAL": "seasonal",
    "APPRENTICESHIP": "apprenticeship",
}
_EXPERIENCE_LABEL = {
    "INTERN": "intern",
    "ENTRY": "entry",
    "JUNIOR": "junior",
    "MID": "mid-level",
    "SENIOR": "senior",
    "STAFF": "staff",
    "PRINCIPAL": "principal",
    "LEAD": "lead",
    "MANAGER": "manager",
    "HEAD": "head",
    "DIRECTOR": "director",
    "VP": "vp",
    "EXECUTIVE": "executive",
}
_REMOTE_LABEL = {"REMOTE": "remote", "HYBRID": "hybrid", "ONSITE": "onsite"}


def _fact_text(row: sqlite3.Row) -> str:
    parts: list[str] = []
    employment = _EMPLOYMENT_LABEL.get(row["employment_type"] or "")
    if employment:
        parts.append(employment)
    experience = _EXPERIENCE_LABEL.get(row["experience_level"] or "")
    if experience:
        parts.append(experience)
    remote = _REMOTE_LABEL.get(row["remote_mode"] or "")
    if remote:
        parts.append(remote)
    if row["remote_worldwide"]:
        parts.append("worldwide")
    return " ".join(parts)


def _job_doc_fields(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute(
        "SELECT j.title, j.description_text, j.remote_mode, j.remote_worldwide,"
        " j.employment_type, j.experience_level, c.name AS company_name"
        " FROM jobs j LEFT JOIN companies c ON c.id = j.company_id"
        " WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    locations: list[str] = []
    seen: set[str] = set()
    for location in conn.execute(
        "SELECT raw_text FROM job_locations WHERE job_id = ?"
        " ORDER BY raw_text, rowid",
        (job_id,),
    ).fetchall():
        raw = _fold(location["raw_text"])
        if raw and raw not in seen:
            seen.add(raw)
            locations.append(raw)
    return {
        # title/company/locations/facts are folded for indexing; the
        # description is kept byte-identical to the canonical text (its
        # paragraph breaks are meaningful and already normalized)
        "title": _fold(row["title"]),
        "company": _fold(row["company_name"]),
        "description_text": str(row["description_text"] or ""),
        "locations_text": " ".join(locations),
        "fact_text": _fact_text(row),
    }


def _content_digest(fields: dict) -> str:
    basis = {"layout": SEARCH_DOC_LAYOUT, **fields}
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def sync_search_doc(
    conn: sqlite3.Connection, *, job_id: str, now: str
) -> bool:
    """Re-sync one job's search document; returns True when content changed.

    Runs inside the caller's transaction (the pipeline fence).  Revision
    check first: unchanged content is a no-op.
    """
    fields = _job_doc_fields(conn, job_id)
    if fields is None:
        return False
    digest = _content_digest(fields)
    state = conn.execute(
        "SELECT indexed_content_hash FROM job_search_state WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if state is not None and state["indexed_content_hash"] == digest:
        return False
    conn.execute(
        "INSERT INTO job_search_docs"
        " (job_id, title, company, description_text, locations_text, fact_text,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(job_id) DO UPDATE SET title = excluded.title,"
        " company = excluded.company, description_text = excluded.description_text,"
        " locations_text = excluded.locations_text, fact_text = excluded.fact_text,"
        " updated_at = excluded.updated_at",
        (
            job_id,
            fields["title"],
            fields["company"],
            fields["description_text"],
            fields["locations_text"],
            fields["fact_text"],
            now,
        ),
    )
    conn.execute(
        "INSERT INTO job_search_state (job_id, indexed_content_hash, indexed_at,"
        " updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(job_id) DO UPDATE SET indexed_content_hash = excluded.indexed_content_hash,"
        " indexed_at = excluded.indexed_at, updated_at = excluded.updated_at",
        (job_id, digest, now, now),
    )
    return True


def index_summary(conn: sqlite3.Connection) -> dict:
    """Read-only index state for Doctor/health surfaces (no writes)."""
    capability = conn.execute(
        "SELECT mode, warning, provisioned_at FROM search_capability WHERE id = 1"
    ).fetchone()
    docs = conn.execute("SELECT COUNT(*) FROM job_search_state").fetchone()[0]
    return {
        "mode": capability["mode"] if capability else None,
        "warning": capability["warning"] if capability else None,
        "provisioned_at": capability["provisioned_at"] if capability else None,
        "indexed_jobs": int(docs),
    }


__all__ = [
    "SEARCH_DOC_LAYOUT",
    "index_summary",
    "sync_search_doc",
]
