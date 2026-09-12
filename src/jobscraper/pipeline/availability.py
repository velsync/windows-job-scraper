"""Evidence-ordered source presence and canonical availability.

Slice 3 S3.10 (03 RUN-11/RUN-13/RUN-14/RUN-14A/RUN-21) owns the
mutable availability projection for ``job_sources`` and the policy used to
derive ``jobs.listing_status``.

Historical observations and coverage rows remain immutable.  This module
updates only the current per-source projection, and it does so with one
comparable evidence order:

    effective/source time -> local receipt time -> accepted revision

The first two components reject a late-finishing old worker.  The revision is
the deterministic local tie-breaker only after effective and receipt time tie.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobscraper.pipeline.provenance import (
    EMPLOYER_CAREERS_PAGE,
    EMPLOYER_SOURCE_FAMILIES,
    EMPLOYER_STRUCTURED_API,
    EMPLOYER_STRUCTURED_ATS,
    QUALITY_RANK,
    class_for_presence,
)

ACTIVE_OBSERVATION = "ACTIVE_OBSERVATION"
ACTIVE_REVALIDATION = "ACTIVE_REVALIDATION"
AUTHORITATIVE_ABSENCE = "AUTHORITATIVE_ABSENCE"
EXPLICIT_TRUSTED_CLOSE = "EXPLICIT_TRUSTED_CLOSE"

LEGACY_ACTIVE = "LEGACY_ACTIVE"
LEGACY_ABSENCE = "LEGACY_ABSENCE"
LEGACY_CLOSE = "LEGACY_CLOSE"
LEGACY_UNKNOWN = "LEGACY_UNKNOWN"

_ACTIVE_KINDS = frozenset({ACTIVE_OBSERVATION, ACTIVE_REVALIDATION, LEGACY_ACTIVE})
_ABSENCE_KINDS = frozenset({AUTHORITATIVE_ABSENCE, LEGACY_ABSENCE})
_CLOSE_KINDS = frozenset({EXPLICIT_TRUSTED_CLOSE, LEGACY_CLOSE})

_TRUSTED_QUALITY_CLASSES = frozenset(
    {
        EMPLOYER_STRUCTURED_ATS,
        EMPLOYER_STRUCTURED_API,
        EMPLOYER_CAREERS_PAGE,
    }
)


@dataclass(frozen=True)
class PresenceEvidenceResult:
    """Result of attempting to advance one source-presence projection."""

    presence_id: str
    job_id: str
    accepted: bool
    state_changed: bool
    prior_state: str
    new_state: str
    current_kind: str
    revision: int
    reason: str


@dataclass(frozen=True)
class CanonicalAvailability:
    """Derived canonical availability plus the winning evidence pointer."""

    status: str
    presence_id: str | None
    source_id: str | None
    evidence_kind: str | None
    evidence_ref: str | None
    conflict: bool


def _field(row, name: str, default=None):
    try:
        keys = row.keys()
    except Exception:
        return default
    if name not in keys:
        return default
    value = row[name]
    return default if value is None else value


def _effective_at(row) -> str:
    return str(
        _field(row, "availability_effective_at")
        or _field(row, "last_verified_at")
        or _field(row, "last_seen_at")
        or ""
    )


def _received_at(row) -> str:
    return str(
        _field(row, "availability_received_at")
        or _field(row, "updated_at")
        or _field(row, "created_at")
        or ""
    )


def _revision(row) -> int:
    try:
        return int(_field(row, "availability_revision", 0) or 0)
    except (TypeError, ValueError):
        return 0


def evidence_order(row) -> tuple[str, str, int]:
    """Comparable accepted current-evidence order for one presence."""

    return (_effective_at(row), _received_at(row), _revision(row))


def is_trusted_presence(row) -> bool:
    """Whether a presence is employer/origin authoritative for availability.

    Stored S2 provenance quality is the primary signal.  The durable source
    family is the conservative fallback for migrated/legacy rows; strategy
    alone is deliberately insufficient because an aggregator may also be
    fetched with a structured strategy.
    """

    stored = str(_field(row, "source_quality_class") or "")
    if stored:
        # A durable explicit class is authoritative.  Never upgrade a row that
        # was deliberately classified as aggregator merely because its source
        # family name looks employer-like; family is only a legacy fallback
        # when the stored class did not yet exist.
        return stored in _TRUSTED_QUALITY_CLASSES
    family = str(_field(row, "source_family") or "").upper()
    return family in EMPLOYER_SOURCE_FAMILIES


def _load_presence(conn: sqlite3.Connection, presence_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT js.*, s.source_family,
               COALESCE(
                   (SELECT o.strategy
                      FROM job_observations o
                     WHERE o.id = js.last_observation_id),
                   ''
               ) AS strategy_source
          FROM job_sources js
          JOIN sources s ON s.id = js.source_id
         WHERE js.id = ?
        """,
        (presence_id,),
    ).fetchone()


def _state_for_evidence(prior_state: str, evidence_kind: str) -> str:
    if evidence_kind in _ACTIVE_KINDS:
        return "ACTIVE"
    if evidence_kind in _CLOSE_KINDS:
        return "CLOSED"
    if evidence_kind in _ABSENCE_KINDS:
        if prior_state in {"CLOSED", "WITHDRAWN"}:
            return prior_state
        if prior_state in {"ACTIVE", "UNKNOWN"}:
            return "UNCERTAIN"
        if prior_state == "UNCERTAIN":
            return "EXPIRED"
        return prior_state
    raise ValueError(f"unknown availability evidence kind {evidence_kind!r}")


def apply_presence_evidence(
    conn: sqlite3.Connection,
    *,
    presence_id: str,
    evidence_kind: str,
    effective_at: str,
    received_at: str,
    evidence_ref: str | None,
    coverage_id: str | None = None,
    scope_key: str | None = None,
) -> PresenceEvidenceResult:
    """Conditionally advance one mutable source-presence projection.

    The caller owns the transaction.  The immutable evidence itself is stored
    by its owning subsystem (observation, coverage, or acquisition evidence);
    this function stores only the pointer/order needed for the current
    projection.
    """

    if not effective_at:
        raise ValueError("availability evidence effective_at must be non-empty")
    if not received_at:
        raise ValueError("availability evidence received_at must be non-empty")
    if evidence_kind not in (_ACTIVE_KINDS | _ABSENCE_KINDS | _CLOSE_KINDS):
        raise ValueError(f"unknown availability evidence kind {evidence_kind!r}")

    row = _load_presence(conn, presence_id)
    if row is None:
        raise ValueError(f"unknown job source presence {presence_id!r}")

    prior_state = str(row["presence_state"])
    current_kind = str(
        _field(row, "availability_evidence_kind", LEGACY_UNKNOWN) or LEGACY_UNKNOWN
    )
    current_ref = _field(row, "availability_evidence_ref")
    current_effective = _effective_at(row)
    current_received = _received_at(row)
    current_revision = _revision(row)

    # Replay of the exact accepted current evidence is a strict no-op.
    # Coverage absence can advance the ordering coordinate while preserving an
    # earlier direct-close semantic pointer; in that shape the durable
    # last_absence_coverage_id is the replay key rather than current_kind/ref.
    duplicate_absence = (
        evidence_kind == AUTHORITATIVE_ABSENCE
        and coverage_id is not None
        and str(_field(row, "last_absence_coverage_id") or "") == str(coverage_id)
        and current_effective == effective_at
        and current_received == received_at
    )
    if duplicate_absence or (
        current_kind == evidence_kind
        and (current_ref or None) == (evidence_ref or None)
        and current_effective == effective_at
        and current_received == received_at
    ):
        return PresenceEvidenceResult(
            presence_id=str(row["id"]),
            job_id=str(row["job_id"]),
            accepted=False,
            state_changed=False,
            prior_state=prior_state,
            new_state=prior_state,
            current_kind=current_kind,
            revision=current_revision,
            reason="DUPLICATE_CURRENT_EVIDENCE",
        )

    # RUN-14A/RUN-21: source/effective time dominates local completion time.
    # A later local receipt may only break a tie when source/effective time is
    # equal; it can never rescue an older effective event.
    incoming_base = (str(effective_at), str(received_at))
    current_base = (current_effective, current_received)
    if incoming_base < current_base:
        return PresenceEvidenceResult(
            presence_id=str(row["id"]),
            job_id=str(row["job_id"]),
            accepted=False,
            state_changed=False,
            prior_state=prior_state,
            new_state=prior_state,
            current_kind=current_kind,
            revision=current_revision,
            reason="STALE_EVIDENCE",
        )

    # Only employer/origin-authoritative direct evidence may create CLOSED.
    if evidence_kind == EXPLICIT_TRUSTED_CLOSE and not is_trusted_presence(row):
        return PresenceEvidenceResult(
            presence_id=str(row["id"]),
            job_id=str(row["job_id"]),
            accepted=False,
            state_changed=False,
            prior_state=prior_state,
            new_state=prior_state,
            current_kind=current_kind,
            revision=current_revision,
            reason="UNTRUSTED_EXPLICIT_CLOSE",
        )

    # Absence is weaker than an explicit direct close *as a state transition*,
    # but it is still newer availability evidence.  Accept it into the current
    # order while _state_for_evidence preserves CLOSED/WITHDRAWN.  This matters
    # when an older positive worker finishes after a newer absence: that older
    # positive must not reopen merely because the absence did not change state.
    new_state = _state_for_evidence(prior_state, evidence_kind)
    state_changed = new_state != prior_state
    next_revision = current_revision + 1

    # A newer absence after CLOSED/WITHDRAWN is important for stale-update
    # rejection, but it is not new *close authority*.  Advance the ordering
    # coordinate while preserving the semantic evidence pointer that actually
    # established the terminal state.  Otherwise a weak later absence would
    # falsely refresh the close time and could defeat a newer trusted ACTIVE
    # presence on another source.
    preserve_terminal_semantics = (
        evidence_kind == AUTHORITATIVE_ABSENCE
        and prior_state in {"CLOSED", "WITHDRAWN"}
    )
    stored_kind = current_kind if preserve_terminal_semantics else evidence_kind
    stored_ref = current_ref if preserve_terminal_semantics else evidence_ref

    conn.execute(
        """
        UPDATE job_sources
           SET presence_state = ?,
               availability_effective_at = ?,
               availability_received_at = ?,
               availability_evidence_kind = ?,
               availability_evidence_ref = ?,
               availability_revision = ?,
               last_verified_at = CASE
                   WHEN last_verified_at IS NULL OR last_verified_at < ?
                   THEN ? ELSE last_verified_at END,
               last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END,
               last_absence_coverage_id = CASE
                   WHEN ? = ? THEN COALESCE(?, last_absence_coverage_id)
                   ELSE last_absence_coverage_id END,
               last_authoritative_scope_key = CASE
                   WHEN ? = ? THEN COALESCE(?, last_authoritative_scope_key)
                   ELSE last_authoritative_scope_key END,
               updated_at = ?
         WHERE id = ?
        """,
        (
            new_state,
            effective_at,
            received_at,
            stored_kind,
            stored_ref,
            next_revision,
            effective_at,
            effective_at,
            1 if state_changed else 0,
            received_at,
            evidence_kind,
            AUTHORITATIVE_ABSENCE,
            coverage_id,
            evidence_kind,
            AUTHORITATIVE_ABSENCE,
            scope_key,
            received_at,
            presence_id,
        ),
    )
    return PresenceEvidenceResult(
        presence_id=str(row["id"]),
        job_id=str(row["job_id"]),
        accepted=True,
        state_changed=state_changed,
        prior_state=prior_state,
        new_state=new_state,
        current_kind=stored_kind,
        revision=next_revision,
        reason="ACCEPTED",
    )


def _availability_rows(
    conn: sqlite3.Connection, job_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT js.*, s.source_family,
               COALESCE(
                   (SELECT o.strategy
                      FROM job_observations o
                     WHERE o.id = js.last_observation_id),
                   ''
               ) AS strategy_source
          FROM job_sources js
          JOIN sources s ON s.id = js.source_id
         WHERE js.job_id = ?
         ORDER BY js.id
        """,
        (job_id,),
    ).fetchall()


def _quality_rank(row) -> int:
    return int(QUALITY_RANK.get(class_for_presence(row), 0))


def _state_effective_at(row) -> str:
    """Effective time of the evidence that established the *current state*.

    The latest accepted ordering coordinate can be newer than the state itself
    (for example, CLOSED at T1 followed by a weak absence at T3).  Such absence
    must block a late positive at T2 on the same presence, but it must not make
    the T1 close look newer than a trusted T2 ACTIVE on another source.
    ``last_changed_at`` is updated only when the presence state changes, so it
    is the conservative state-authority coordinate for terminal states.
    """

    state = str(_field(row, "presence_state") or "UNKNOWN")
    if state in {"CLOSED", "WITHDRAWN"}:
        return str(_field(row, "last_changed_at") or _effective_at(row))
    return _effective_at(row)


def _state_received_at(row) -> str:
    state = str(_field(row, "presence_state") or "UNKNOWN")
    if state in {"CLOSED", "WITHDRAWN"}:
        return str(_field(row, "last_changed_at") or _received_at(row))
    return _received_at(row)


def _canonical_order(row) -> tuple[str, int, str]:
    # Canonical resolution compares the evidence that established the current
    # state, not merely the newest weaker evidence used for stale-update
    # rejection. Source quality breaks same-effective-time conflicts before the
    # local receipt tie-breaker. ``availability_revision`` is intentionally not
    # compared across different presences: it is a per-presence counter, not a
    # globally comparable revision.
    return (
        _state_effective_at(row),
        _quality_rank(row),
        _state_received_at(row),
    )


def _winner(rows: list[sqlite3.Row]) -> sqlite3.Row:
    # Stable-id is only a deterministic selector between otherwise equivalent
    # rows of the *same candidate class*. It never decides ACTIVE-vs-CLOSED.
    best = None
    best_order = None
    for row in sorted(rows, key=lambda item: str(item["id"])):
        order = _canonical_order(row)
        if best_order is None or order > best_order:
            best, best_order = row, order
    return best


def _availability_result(
    status: str,
    row: sqlite3.Row | None,
    *,
    conflict: bool,
) -> CanonicalAvailability:
    return CanonicalAvailability(
        status=status,
        presence_id=str(row["id"]) if row is not None else None,
        source_id=str(row["source_id"]) if row is not None else None,
        evidence_kind=(
            str(_field(row, "availability_evidence_kind") or LEGACY_UNKNOWN)
            if row is not None
            else None
        ),
        evidence_ref=(
            _field(row, "availability_evidence_ref") if row is not None else None
        ),
        conflict=conflict,
    )


def resolve_canonical_availability(
    conn: sqlite3.Connection, job_id: str
) -> CanonicalAvailability:
    """Resolve ``jobs.listing_status`` from current source-presence evidence.

    Trusted employer/ATS evidence is considered first.  A direct trusted close
    and trusted active sighting are ordered temporally; ordinary absence never
    defeats another current trusted ACTIVE presence.  When no trusted presence
    exists, third-party evidence remains useful but is intentionally unable to
    silently produce canonical CLOSED.
    """

    rows = _availability_rows(conn, job_id)
    if not rows:
        return _availability_result("UNKNOWN", None, conflict=False)

    trusted = [row for row in rows if is_trusted_presence(row)]
    considered = trusted or rows
    states = {str(row["presence_state"]) for row in considered}
    conflict = len(states) > 1

    if trusted:
        active = [row for row in trusted if str(row["presence_state"]) == "ACTIVE"]
        # CLOSED is only created by trusted direct closure (or preserved from
        # a legacy CLOSED projection).  Later absence may advance the evidence
        # order without weakening that state, so do not require the *current*
        # evidence kind itself to remain a close marker.
        closed = [
            row for row in trusted if str(row["presence_state"]) == "CLOSED"
        ]
        if active and closed:
            active_winner = _winner(active)
            close_winner = _winner(closed)
            # Exact meaningful ties remain conservative: direct closure must
            # be strictly newer/better to defeat a trusted ACTIVE presence.
            if _canonical_order(close_winner) > _canonical_order(active_winner):
                return _availability_result("CLOSED", close_winner, conflict=True)
            return _availability_result("ACTIVE", active_winner, conflict=True)
        if closed:
            return _availability_result("CLOSED", _winner(closed), conflict=conflict)
        if active:
            # A current trusted positive presence beats absence/expiry from
            # another source.  Explicit closure is the only negative evidence
            # allowed to defeat it.
            return _availability_result("ACTIVE", _winner(active), conflict=conflict)

        if all(str(row["presence_state"]) == "EXPIRED" for row in trusted):
            return _availability_result("EXPIRED", _winner(trusted), conflict=conflict)
        if all(str(row["presence_state"]) == "WITHDRAWN" for row in trusted):
            return _availability_result("WITHDRAWN", _winner(trusted), conflict=conflict)
        return _availability_result("UNCERTAIN", _winner(trusted), conflict=conflict)

    active = [row for row in rows if str(row["presence_state"]) == "ACTIVE"]
    if active:
        return _availability_result("ACTIVE", _winner(active), conflict=conflict)
    if any(str(row["presence_state"]) == "CLOSED" for row in rows):
        # No third-party evidence path can create CLOSED (explicit close is
        # trusted-gated; absence only preserves an existing close), so a
        # CLOSED row here is necessarily a preserved legacy projection.
        # Keep resolving it as CLOSED rather than silently reopening a
        # settled listing to UNCERTAIN when no evidence defeats the close.
        closed = [row for row in rows if str(row["presence_state"]) == "CLOSED"]
        return _availability_result("CLOSED", _winner(closed), conflict=conflict)
    if all(str(row["presence_state"]) == "EXPIRED" for row in rows):
        return _availability_result("EXPIRED", _winner(rows), conflict=conflict)
    if all(str(row["presence_state"]) == "WITHDRAWN" for row in rows):
        return _availability_result("WITHDRAWN", _winner(rows), conflict=conflict)

    # Third-party close/absence is intentionally conservative: without
    # employer/origin authority it cannot silently become canonical CLOSED.
    return _availability_result("UNCERTAIN", _winner(rows), conflict=conflict)


def _presence_for_detail_target(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    target_reference: str | None,
) -> sqlite3.Row | None:
    if not target_reference:
        return None

    native_rows = conn.execute(
        """
        SELECT js.*, s.source_family
          FROM job_sources js
          JOIN sources s ON s.id = js.source_id
         WHERE js.source_id = ? AND js.source_job_id = ?
         ORDER BY js.source_identity_generation DESC, js.created_at DESC, js.id
        """,
        (source_id, target_reference),
    ).fetchall()
    if native_rows:
        # RUN-15: a bare native id is no longer an unambiguous current target
        # once that id has been reused across generations.  A delayed DETAIL
        # request created for generation 1 could otherwise finish after
        # generation 2 exists and close the wrong canonical job.  Until the
        # durable request itself carries generation identity, fail closed on
        # *any* reuse rather than guessing the newest row.
        return native_rows[0] if len(native_rows) == 1 else None

    rows = conn.execute(
        """
        SELECT js.*, s.source_family
          FROM job_sources js
          JOIN sources s ON s.id = js.source_id
         WHERE js.source_id = ?
           AND ? IN (
               js.discovery_url, js.raw_source_url, js.canonical_job_url,
               js.application_url
           )
         ORDER BY js.source_identity_generation DESC, js.created_at DESC
        """,
        (source_id, target_reference),
    ).fetchall()
    # URL reuse across generations/identities is ambiguous.  Preserve the
    # closure evidence for review but refuse a guessed current projection.
    return rows[0] if len(rows) == 1 else None


def apply_trusted_detail_closure(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    target_reference: str | None,
    effective_at: str,
    received_at: str,
    evidence_ref: str,
) -> PresenceEvidenceResult | None:
    """Apply one typed DETAIL close/missing result to its exact trusted target.

    Opaque provider-native ids are preferred.  URL fallback is accepted only
    when it resolves to exactly one presence for the source, which protects the
    RUN-15 native-id generation guard and avoids closing an unrelated job.
    """

    row = _presence_for_detail_target(
        conn, source_id=source_id, target_reference=target_reference
    )
    if row is None:
        return None
    return apply_presence_evidence(
        conn,
        presence_id=str(row["id"]),
        evidence_kind=EXPLICIT_TRUSTED_CLOSE,
        effective_at=effective_at,
        received_at=received_at,
        evidence_ref=evidence_ref,
    )


__all__ = [
    "ACTIVE_OBSERVATION",
    "ACTIVE_REVALIDATION",
    "AUTHORITATIVE_ABSENCE",
    "CanonicalAvailability",
    "EXPLICIT_TRUSTED_CLOSE",
    "PresenceEvidenceResult",
    "apply_presence_evidence",
    "apply_trusted_detail_closure",
    "evidence_order",
    "is_trusted_presence",
    "resolve_canonical_availability",
]
