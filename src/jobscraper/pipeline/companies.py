"""Company resolution from strong signals only (01 §33.1, ROAD-03).

01 §33.1 lists the resolution signals — normalized name, domain, application
URL host, ATS board, careers URL, structured organization metadata — and states
the constraint that matters: **weak evidence must not aggressively merge
companies**.  A wrong merge here silently destroys two employers' distinct
postings; a wrong split stays recoverable through the merge ledger of a later
slice.  So:

* an attach requires a strong identifier: the same ``(ats_provider, board)``,
  or the same employer application/careers/organization host;
* shared ATS platform infrastructure is evidence, not employer identity, and
  therefore never becomes a ``company_identifiers`` merge key;
* a bare matching normalized name is *never* sufficient — it creates a new
  company and records why;
* a strong-identifier attach with a conflicting display name does not rename
  anybody; the conflict is recorded for review (a later slice owns the
  explicit merge/review workflow);
* every decision, matched-on signal and input snapshot lands in
  ``company_resolution_events`` (non-destructive, versioned).

No public-suffix list is introduced (no new runtime dependency): the host key
is the *full* lowercased host with a leading ``www.`` dropped.  Two subdomains
of one registrable domain therefore do **not** merge — the conservative
direction, deliberately chosen over guessing.

This module writes company state only from ``pipeline/`` (the Slice-2 contract
that keeps canonical writes in one place) and never performs I/O.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from jobscraper.acquisition.atsendpoints import ATS_ENDPOINT_SPECS
from jobscraper.ids import new_id
from jobscraper.pipeline.normalize import normalize_company

#: Version of the resolution rules (ARC-10): recorded on every event so a
#: later rule change is never mistaken for a data change.  v2 corrects v1's
#: treatment of shared ATS platform hosts as globally unique company keys.
COMPANY_RESOLUTION_VERSION = "company-resolution-v2"

#: Strong identifier kinds indexed in ``company_identifiers``.
IDENTIFIER_KINDS = ("ATS_BOARD", "APP_HOST", "ORG_DOMAIN", "CAREERS_HOST")

#: Attach priority.  A board identity is provider-scoped and therefore the
#: most specific; employer-owned hosts follow.  Shared ATS infrastructure is
#: filtered before it can enter this ordering.
_MATCH_PRIORITY = ("ATS_BOARD", "APP_HOST", "ORG_DOMAIN", "CAREERS_HOST")

# Hosts that belong to the ATS platform rather than to the employer.  They may
# remain visible in the observation/company-resolution evidence snapshot, but
# they are neither a company domain nor a strong ``company_identifiers`` key:
# many unrelated employers legitimately share each host.  Greenhouse/Lever/
# Ashby hosts come from the single versioned ATS endpoint authority so regional
# or newly reviewed provider hosts cannot drift out of sync here.
_PLATFORM_HOSTS = frozenset(
    {
        "boards.greenhouse.eu",
        "careers.smartrecruiters.com",
        "workday.com",
        "myworkday.com",
        "myworkdayjobs.com",
        "wd1.myworkdayjobs.com",
        "wd3.myworkdayjobs.com",
        "recruiting.lever.co",
    }
) | frozenset(
    host.lower()
    for spec in ATS_ENDPOINT_SPECS
    for host in spec.hosts()
)


def host_key(value: str | None) -> str | None:
    """Normalize a URL *or* a bare host into a comparable host key."""
    if not value:
        return None
    text = value.strip()
    if "://" not in text:
        text = "//" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _strong_host_key(value: str | None) -> str | None:
    """Return an employer-owned host suitable for a company identity key.

    ATS platform hosts are intentionally refused here rather than merely
    deprioritized: ``company_identifiers(kind, value)`` is globally unique and
    represents evidence strong enough to carry a merge, which shared provider
    infrastructure is not (01 §33.1).
    """
    key = host_key(value)
    if key is None or key in _PLATFORM_HOSTS:
        return None
    return key


@dataclass(frozen=True)
class CompanySignals:
    """Everything this observation says about the employer."""

    name: str | None = None
    application_host: str | None = None
    careers_url: str | None = None
    organization_domains: tuple[str, ...] = ()
    ats_provider: str | None = None
    ats_board: str | None = None
    country: str | None = None

    @property
    def normalized_name(self) -> str | None:
        return normalize_company(self.name) if self.name else None

    @property
    def employer_host(self) -> str | None:
        """The employer's own host (never an ATS platform host)."""
        for candidate in self._employer_candidates():
            if candidate not in _PLATFORM_HOSTS:
                return candidate
        return None

    def _employer_candidates(self) -> tuple[str, ...]:
        out: list[str] = []
        for value in self.organization_domains:
            key = host_key(value)
            if key:
                out.append(key)
        careers = host_key(self.careers_url)
        if careers:
            out.append(careers)
        app = host_key(self.application_host)
        if app:
            out.append(app)
        return tuple(out)

    def identifier_keys(self) -> tuple[tuple[str, str], ...]:
        """Strong identifier keys this observation can assert, in priority."""
        keys: list[tuple[str, str]] = []
        if self.ats_provider and self.ats_board:
            keys.append(
                (
                    "ATS_BOARD",
                    f"{self.ats_provider.strip().upper()}/{self.ats_board.strip().lower()}",
                )
            )
        app_host = _strong_host_key(self.application_host)
        if app_host:
            keys.append(("APP_HOST", app_host))
        for value in self.organization_domains:
            key = _strong_host_key(value)
            if key:
                keys.append(("ORG_DOMAIN", key))
        careers_host = _strong_host_key(self.careers_url)
        if careers_host:
            keys.append(("CAREERS_HOST", careers_host))
        # de-duplicate while keeping the deterministic priority order
        seen: set[tuple[str, str]] = set()
        ordered: list[tuple[str, str]] = []
        for kind, value in keys:
            if (kind, value) in seen:
                continue
            seen.add((kind, value))
            ordered.append((kind, value))
        return tuple(ordered)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "normalized_name": self.normalized_name,
            "application_host": host_key(self.application_host),
            "careers_url": self.careers_url,
            "organization_domains": sorted(
                {k for k in (host_key(v) for v in self.organization_domains) if k}
            ),
            "ats_provider": self.ats_provider,
            "ats_board": self.ats_board,
            "country": self.country,
        }


@dataclass(frozen=True)
class CompanyResolution:
    company_id: str | None
    decision: str  # CREATED | ATTACHED | NAME_ONLY_NEW_COMPANY | NO_SIGNAL
    matched_on: str | None = None
    name_conflict: bool = False
    reason_code: str | None = None
    resolution_version: str = COMPANY_RESOLUTION_VERSION
    signals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "company_id": self.company_id,
            "decision": self.decision,
            "matched_on": self.matched_on,
            "name_conflict": self.name_conflict,
            "reason_code": self.reason_code,
            "resolution_version": self.resolution_version,
        }


def signals_from(*, normalized, origin=None, application_url: str | None = None) -> CompanySignals:
    """Build the resolution signals from normalized evidence + origin result.

    The ATS identity only counts when §32 actually resolved it: an unresolved
    origin contributes name/host signals, never a board key (02 §32).
    """
    provider = board = None
    # explicit equality on the resolver's own status value: a substring test
    # would read UNRESOLVED as "…RESOLVED" and mint a board identity from a
    # resolution that said the opposite
    status = getattr(origin, "status", None)
    status_value = getattr(status, "value", status)
    resolved = origin is not None and str(status_value).upper() == "RESOLVED"
    if resolved:
        provider = getattr(origin, "origin_provider", None)
        board = getattr(origin, "origin_board", None)
    domains = tuple(getattr(normalized, "organization_domains", ()) or ())
    return CompanySignals(
        name=getattr(normalized, "company_name", None),
        application_host=application_url or getattr(normalized, "careers_url", None),
        careers_url=getattr(normalized, "careers_url", None),
        organization_domains=domains,
        ats_provider=provider,
        ats_board=board,
        country=getattr(normalized, "company_country", None),
    )


def _candidate_by_identifiers(
    conn: sqlite3.Connection, keys: tuple[tuple[str, str], ...]
) -> tuple[str, str] | None:
    """Highest-priority strong match: ``(company_id, kind)``, or None."""
    if not keys:
        return None
    placeholders = ",".join("(?, ?)" for _ in keys)
    params = [value for pair in keys for value in pair]
    rows = conn.execute(
        "SELECT company_id, kind, value FROM company_identifiers"
        f" WHERE (kind, value) IN ({placeholders})",
        params,
    ).fetchall()
    if not rows:
        return None
    by_pair = {(row["kind"], row["value"]): row["company_id"] for row in rows}
    for kind, value in keys:  # keys are already in priority order
        company_id = by_pair.get((kind, value))
        if company_id:
            return company_id, kind
    return None


def _register_identifiers(
    conn: sqlite3.Connection,
    company_id: str,
    keys: tuple[tuple[str, str], ...],
    *,
    observed_at: str,
    now: str,
) -> int:
    registered = 0
    for kind, value in keys:
        conn.execute(
            "INSERT OR IGNORE INTO company_identifiers (id, company_id, kind, value,"
            " first_seen_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("ci"), company_id, kind, value, observed_at, now),
        )
        registered += 1
    return registered


def resolve_company(
    conn: sqlite3.Connection,
    *,
    signals: CompanySignals,
    observation_id: str | None = None,
    observed_at: str,
    now: str,
) -> CompanyResolution:
    """Resolve one sighting to a company, or create a new one, conservatively."""
    keys = signals.identifier_keys()
    matched = _candidate_by_identifiers(conn, keys)

    if matched is not None:
        company_id, kind = matched
        company = conn.execute(
            "SELECT * FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        name_conflict = bool(
            company
            and signals.normalized_name
            and company["normalized_name"]
            and company["normalized_name"] != signals.normalized_name
        )
        conn.execute(
            """
            UPDATE companies SET
                domain = COALESCE(domain, ?),
                careers_url = COALESCE(careers_url, ?),
                ats_provider = COALESCE(ats_provider, ?),
                ats_board = COALESCE(ats_board, ?),
                country = COALESCE(country, ?),
                last_posting_at = MAX(COALESCE(last_posting_at, ?), ?),
                updated_at = ?
            WHERE id = ?
            """,
            (
                signals.employer_host,
                signals.careers_url,
                signals.ats_provider,
                signals.ats_board,
                signals.country,
                observed_at,
                observed_at,
                now,
                company_id,
            ),
        )
        _register_identifiers(conn, company_id, keys, observed_at=observed_at, now=now)
        decision = "ATTACHED"
        resolution = CompanyResolution(
            company_id=company_id,
            decision=decision,
            matched_on=kind,
            name_conflict=name_conflict,
            reason_code="NAME_CONFLICT_REVIEW" if name_conflict else None,
            signals=signals.as_dict(),
        )
        _record_event(conn, resolution, observation_id=observation_id, now=now)
        return resolution

    if not signals.normalized_name:
        # ``companies.name`` is the one thing this pipeline must never
        # fabricate (01 §33.1).  Strong identifiers *without* an employer name
        # — the shape a provider-native board sighting has before the operator
        # records the company name, or a payload that simply omits it — cannot
        # create a row, so nothing is created and the sighting stays
        # explainable through its recorded decision.  The identifiers are not
        # lost: they arrive again with the next observation, and the first
        # sighting that does carry a name creates the company and attaches
        # them (``_register_identifiers`` runs on both paths).
        resolution = CompanyResolution(
            company_id=None,
            decision="NO_SIGNAL",
            reason_code="NO_COMPANY_EVIDENCE" if not keys else "IDENTIFIERS_WITHOUT_NAME",
            signals=signals.as_dict(),
        )
        _record_event(conn, resolution, observation_id=observation_id, now=now)
        return resolution

    # A name collision *without* any shared strong identifier is the refused
    # merge: recorded explicitly, because it is the case an operator most often
    # wants to review (two employers that happen to normalize alike).
    name_collision = (
        conn.execute(
            "SELECT 1 FROM companies WHERE normalized_name = ? LIMIT 1",
            (signals.normalized_name,),
        ).fetchone()
        is not None
    )
    company_id = new_id("co")
    conn.execute(
        """
        INSERT INTO companies (id, name, normalized_name, domain, careers_url,
            ats_provider, ats_board, country, first_seen_at, last_posting_at,
            created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            signals.name,
            signals.normalized_name,
            signals.employer_host,
            signals.careers_url,
            signals.ats_provider,
            signals.ats_board,
            signals.country,
            observed_at,
            observed_at,
            now,
            now,
        ),
    )
    _register_identifiers(conn, company_id, keys, observed_at=observed_at, now=now)
    resolution = CompanyResolution(
        company_id=company_id,
        decision="NAME_ONLY_NEW_COMPANY" if name_collision else "CREATED",
        matched_on=None,
        reason_code="WEAK_EVIDENCE_NO_MERGE" if name_collision else "NEW_EMPLOYER",
        signals=signals.as_dict(),
    )
    _record_event(conn, resolution, observation_id=observation_id, now=now)
    return resolution


def _record_event(
    conn: sqlite3.Connection,
    resolution: CompanyResolution,
    *,
    observation_id: str | None,
    now: str,
) -> None:
    import json

    conn.execute(
        """
        INSERT INTO company_resolution_events (id, observation_id, company_id,
            decision, matched_on, name_conflict, reason_code, signals_json,
            resolution_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("cre"),
            observation_id,
            resolution.company_id,
            resolution.decision,
            resolution.matched_on,
            1 if resolution.name_conflict else 0,
            resolution.reason_code,
            json.dumps(resolution.signals, sort_keys=True, default=str),
            resolution.resolution_version,
            now,
        ),
    )


__all__ = [
    "COMPANY_RESOLUTION_VERSION",
    "signals_from",
    "IDENTIFIER_KINDS",
    "CompanyResolution",
    "CompanySignals",
    "host_key",
    "resolve_company",
]
