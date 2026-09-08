"""Canonical provenance selection (01 §39, 03 RUN-12/RUN-21, ROAD-03).

§39 prefers the highest-quality *current* provenance for the displayed
canonical fields:

```text
employer structured ATS/API → employer careers page
→ aggregator with resolved employer origin → aggregator without resolved origin
```

Slice 1 could only proxy this through strategy quality, because a presence had
no recorded content class or origin state.  Slice 2 records both, so the class
is computed once from the evidence available at ingest time, **stored on the
presence** (`job_sources.source_quality_class`), and selection becomes a
durable, replayable ordering instead of a re-derivation of opinion.

Selection controls presentation only: losers keep their rows, links and
evidence (RUN-12), and every presence remains inspectable.
"""

from __future__ import annotations

import sqlite3
from typing import Mapping, Sequence

#: Versioned selection rule (ARC-10): recorded on the projection so a later
#: rule change is distinguishable from a data change (RUN-21).
PROVENANCE_SELECTOR_VERSION = "canonical-provenance-selector-v2"

EMPLOYER_STRUCTURED_ATS = "EMPLOYER_STRUCTURED_ATS"
EMPLOYER_STRUCTURED_API = "EMPLOYER_STRUCTURED_API"
EMPLOYER_CAREERS_PAGE = "EMPLOYER_CAREERS_PAGE"
AGGREGATOR_WITH_RESOLVED_ORIGIN = "AGGREGATOR_WITH_RESOLVED_ORIGIN"
AGGREGATOR_WITHOUT_RESOLVED_ORIGIN = "AGGREGATOR_WITHOUT_RESOLVED_ORIGIN"

#: The §39 ordering, highest quality first.
SOURCE_QUALITY_CLASSES: tuple[str, ...] = (
    EMPLOYER_STRUCTURED_ATS,
    EMPLOYER_STRUCTURED_API,
    EMPLOYER_CAREERS_PAGE,
    AGGREGATOR_WITH_RESOLVED_ORIGIN,
    AGGREGATOR_WITHOUT_RESOLVED_ORIGIN,
)

QUALITY_RANK: dict[str, int] = {
    name: len(SOURCE_QUALITY_CLASSES) - index for index, name in enumerate(SOURCE_QUALITY_CLASSES)
}

#: Pre-v12 rows carry no stored class.  The fallback reproduces the Slice-1
#: strategy proxy *class*, never a better one: a strategy alone can only speak
#: to how structured the fetch was, not to whose host it was, so no aggregator
#: is ever promoted to employer quality from a strategy name.
_LEGACY_CLASS_BY_STRATEGY: Mapping[str, str] = {
    "PROVIDER_NATIVE": EMPLOYER_STRUCTURED_ATS,
    "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT": EMPLOYER_STRUCTURED_API,
    "STRUCTURED_PAGE": EMPLOYER_CAREERS_PAGE,
    "HTTP_HTML": EMPLOYER_CAREERS_PAGE,
    "PLAYWRIGHT_PUBLIC": EMPLOYER_CAREERS_PAGE,
    "PLAYWRIGHT_AUTHENTICATED": EMPLOYER_CAREERS_PAGE,
    "MANUAL_UNSUPPORTED": AGGREGATOR_WITHOUT_RESOLVED_ORIGIN,
    "GENERIC_DISCOVERY": AGGREGATOR_WITHOUT_RESOLVED_ORIGIN,
}

# Strategies that address an ATS provider's own API (S2.5+); structured content
# from one of these is the employer's authoritative structured posting.
_PROVIDER_ADAPTER_STRATEGIES = frozenset({"PROVIDER_NATIVE"})


#: Source families that serve the employer's own (or the employer's ATS's)
#: postings, as opposed to a third-party board.  ``source_family`` is the
#: durable, operator-set classification (02 §69 leaves the vocabulary open), so
#: it is what distinguishes "employer feed that links out to an ATS" from
#: "aggregator that happens to be fetched from its own host".
EMPLOYER_SOURCE_FAMILIES = frozenset(
    {
        "EMPLOYER_CAREERS",
        "EMPLOYER_SITE",
        "EMPLOYER_API",
        "ATS_BOARD",
        "ATS_PROVIDER_API",
    }
)


def classify_source_quality(
    *,
    strategy: str | None,
    execution_class: str | None,
    content_kind: str | None,
    same_host_as_source: bool | None,
    origin_status: str | None,
    origin_provider: str | None,
    source_family: str | None = None,
    origin_on_source_host: bool | None = None,
) -> str:
    """Classify one presence from evidence recorded at fetch/parse time.

    ``content_kind`` is ``STRUCTURED`` (JSON/XML-shaped payload the parser read)
    or ``HTML``/``UNKNOWN``; ``same_host_as_source`` says whether the fetched
    host *is* the source's own host; ``origin_status`` is the §32 resolver
    outcome.  Nothing here re-reads the network or guesses: an absent signal is
    treated as the weaker case.
    """
    structured = (content_kind or "").upper() == "STRUCTURED"
    resolved = (origin_status or "").upper() == "RESOLVED" and bool(origin_provider)
    # Employer-side means one of two things, and nothing else:
    # (a) the operator classified this source as the employer's own site or its
    #     ATS board — that declaration survives an apply link that points away;
    # (b) the posting link itself lives on this source's host *and* the §32
    #     resolver did not locate the posting's real home elsewhere.  A
    #     self-hosted board that mirrors an ATS posting is an aggregator even
    #     though the copy sits on its own host, so (b) is demoted whenever the
    #     resolved origin is off-host.
    family_employer = (source_family or "").upper() in EMPLOYER_SOURCE_FAMILIES
    origin_elsewhere = resolved and origin_on_source_host is False
    employer_side = family_employer or (
        bool(same_host_as_source) and not origin_elsewhere
    )

    if structured and employer_side:
        # the employer's own structured posting, at the ATS or at the employer
        return EMPLOYER_STRUCTURED_ATS if resolved else EMPLOYER_STRUCTURED_API
    if structured and resolved:
        return AGGREGATOR_WITH_RESOLVED_ORIGIN
    if not structured and employer_side:
        return EMPLOYER_CAREERS_PAGE
    if resolved:
        return AGGREGATOR_WITH_RESOLVED_ORIGIN
    return AGGREGATOR_WITHOUT_RESOLVED_ORIGIN


def _field(presence, name: str, default=None):
    """Read a column from a ``sqlite3.Row`` or a plain dict, absence-tolerantly.

    Selection is also used with projections that do not carry every column (and
    pre-v12 rows have no quality class at all), so a missing key is a missing
    signal — never a KeyError and never a fabricated value.
    """
    try:
        keys = set(presence.keys())
    except Exception:  # pragma: no cover - defensive for exotic mappers
        keys = set()
    if name not in keys:
        return default
    value = presence[name]
    return default if value is None else value


def class_for_presence(presence) -> str:
    """The presence's quality class, falling back for pre-v12 rows."""
    stored = _field(presence, "source_quality_class")
    if stored:
        return stored
    strategy = _field(presence, "strategy_source") or _field(presence, "strategy")
    return _LEGACY_CLASS_BY_STRATEGY.get(strategy or "", AGGREGATOR_WITHOUT_RESOLVED_ORIGIN)


def selection_key(presence) -> tuple:
    """Deterministic ordering key: §39 class, then strategy quality, then
    evidence recency.  The final stable-id tie-break is applied by the caller's
    iteration order, so a tie prefers the lexicographically smallest id."""
    from jobscraper.pipeline.canonical import STRATEGY_QUALITY

    strategy = _field(presence, "strategy_source") or _field(presence, "strategy") or ""
    return (
        QUALITY_RANK.get(class_for_presence(presence), 0),
        STRATEGY_QUALITY.get(strategy, 0),
        _field(presence, "last_seen_at", "") or "",
    )


def select_canonical_provenance(presences: Sequence) -> list:
    """Pick the provenance whose content is presented on the canonical job.

    Ties resolve to the smallest ``id`` (stable across runs), so the projection
    is a function of the evidence alone.
    """
    if not presences:
        raise ValueError("cannot select canonical provenance from no presences")
    best = None
    best_key = None
    for presence in sorted(presences, key=lambda p: str(p["id"])):
        key = selection_key(presence)
        if best_key is None or key > best_key:
            best, best_key = presence, key
    return best


def presences_for_job(conn: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    """All presences of a job with the inputs selection needs."""
    return conn.execute(
        """
        SELECT js.*, COALESCE(
            (SELECT o.strategy FROM job_observations o WHERE o.id = js.last_observation_id),
            'HTTP_HTML') AS strategy_source
        FROM job_sources js WHERE js.job_id = ?
        """,
        (job_id,),
    ).fetchall()


__all__ = [
    "AGGREGATOR_WITHOUT_RESOLVED_ORIGIN",
    "AGGREGATOR_WITH_RESOLVED_ORIGIN",
    "EMPLOYER_CAREERS_PAGE",
    "EMPLOYER_CAREERS_PAGE",
    "EMPLOYER_SOURCE_FAMILIES",
    "EMPLOYER_STRUCTURED_API",
    "EMPLOYER_STRUCTURED_ATS",
    "PROVENANCE_SELECTOR_VERSION",
    "QUALITY_RANK",
    "SOURCE_QUALITY_CLASSES",
    "_field",
    "class_for_presence",
    "classify_source_quality",
    "presences_for_job",
    "select_canonical_provenance",
    "selection_key",
]
