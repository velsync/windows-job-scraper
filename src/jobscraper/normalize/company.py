"""Company resolution.

Authority: module 01 section 33.1. Weak evidence must not aggressively merge
companies.
"""

from __future__ import annotations

import re
import secrets
from urllib.parse import urlsplit

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import utc_now_s

LEGAL_SUFFIXES = (
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "ag", "sa", "sas", "bv", "nv",
    "plc", "pty", "corp", "corp.", "corporation", "co", "co.", "company", "holdings",
)


def normalize_company_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = re.sub(r"[^\w\s&.-]", "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    tokens = [t for t in cleaned.split() if t not in {s.lower() for s in LEGAL_SUFFIXES}]
    return " ".join(tokens) if tokens else cleaned


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url.strip()).netloc.lower()
    except ValueError:
        return None
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host or None


def resolve_company(
    db: Database,
    *,
    name: str | None,
    domain: str | None = None,
    careers_url: str | None = None,
    ats_provider: str | None = None,
    ats_board: str | None = None,
) -> str:
    """Resolve or create a company.

    Resolution signals: exact domain match is strong; normalized-name-only is
    weaker and requires the domain to be absent on both sides.
    """
    now = utc_now_s()
    normalized = normalize_company_name(name)
    domain = (domain or "").lower() or None

    if domain:
        row = db.query_one(
            "SELECT id FROM companies WHERE domain = ? ORDER BY (normalized_name = ?) DESC LIMIT 1",
            (domain, normalized or ""),
        )
        if row is not None:
            company_id = row["id"]
            with immediate_transaction(db.conn) as tx:
                tx.execute(
                    "UPDATE companies SET name = COALESCE(NULLIF(?, ''), name),"
                    " careers_url = COALESCE(?, careers_url), ats_provider = COALESCE(?, ats_provider),"
                    " ats_board = COALESCE(?, ats_board), updated_at = ? WHERE id = ?",
                    (name or "", careers_url, ats_provider, ats_board, now, company_id),
                )
            return company_id

    if normalized:
        row = db.query_one(
            "SELECT id FROM companies WHERE normalized_name = ? AND (domain IS NULL OR ? IS NULL) LIMIT 1",
            (normalized, domain),
        )
        if row is not None:
            company_id = row["id"]
            with immediate_transaction(db.conn) as tx:
                tx.execute(
                    "UPDATE companies SET domain = COALESCE(domain, ?), careers_url = COALESCE(careers_url, ?),"
                    " ats_provider = COALESCE(ats_provider, ?), ats_board = COALESCE(ats_board, ?),"
                    " updated_at = ? WHERE id = ?",
                    (domain, careers_url, ats_provider, ats_board, now, company_id),
                )
            return company_id

    company_id = "co-" + secrets.token_hex(8)
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO companies(id, name, normalized_name, domain, careers_url, ats_provider,"
            " ats_board, first_seen_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (company_id, name or normalized or "Unknown", normalized or "unknown", domain,
             careers_url, ats_provider, ats_board, now, now, now),
        )
    return company_id
