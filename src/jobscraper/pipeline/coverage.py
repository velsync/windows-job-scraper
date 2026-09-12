"""Enumeration coverage and absence authority (03 §40, RUN-13, RUN-14A).

S3.8 makes coverage generation-wide, scope-explicit, and replay-safe:

* coverage identity is derived from the immutable RunSourcePlan;
* contributing requests and the durable seen-identity union are validated
  against that exact plan;
* authoritative scope membership is explicit per source presence, binding
  revision, and scope key -- never inferred from canonical job fields;
* COMPLETE is a barrier: terminal enumeration, successful contributors, no
  relevant continuation, and required detail completion when listing identity
  is insufficient;
* absence is applied at most once per coverage/presence/scope; and
* generation order plus newer presence evidence prevents an older generation
  from regressing current presence merely because it finished later.

A PARTIAL/degraded generation can retain useful observations and scope
membership evidence, but it can never create absence transitions.
"""

from __future__ import annotations

import json
import sqlite3

from jobscraper.ids import new_id

_ABSENCE_AUTHORITIES = frozenset(
    {"AUTHORITATIVE_FULL_SOURCE", "AUTHORITATIVE_DECLARED_SCOPE"}
)
_COMPLETION_STATES = frozenset(
    {"COMPLETE", "PARTIAL", "CANCELLED", "FAILED", "BUDGET_EXHAUSTED", "UNKNOWN"}
)
_OPEN_REQUEST_STATES = frozenset({"PENDING", "RUNNING", "RETRY_WAIT"})
_META_CRAWL_ROLES = frozenset({"ROBOTS", "SITEMAP"})


class CoverageFinalizationError(RuntimeError):
    """The coverage generation cannot be finalized as requested."""


class CoverageIdentityError(RuntimeError):
    """Coverage/request/source identity conflicts with the immutable run plan."""


def _plan_identity(conn: sqlite3.Connection, run_source_plan_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, run_id, source_plan_group_id, source_id, binding_id,
               binding_revision_id
          FROM run_source_plans
         WHERE id = ?
        """,
        (run_source_plan_id,),
    ).fetchone()
    if row is None:
        raise CoverageIdentityError(
            f"unknown RunSourcePlan {run_source_plan_id!r}"
        )
    return row


def _assert_optional_identity(
    plan: sqlite3.Row,
    *,
    source_id: str | None,
    binding_id: str | None,
    binding_revision_id: str | None,
) -> None:
    supplied = (
        ("source_id", source_id),
        ("binding_id", binding_id),
        ("binding_revision_id", binding_revision_id),
    )
    for name, value in supplied:
        if value is not None and str(value) != str(plan[name]):
            raise CoverageIdentityError(
                f"caller {name}={value!r} conflicts with immutable "
                f"RunSourcePlan value {plan[name]!r}"
            )


def _order_key(row: sqlite3.Row) -> str:
    stored = row["generation_order_key"] if "generation_order_key" in row.keys() else None
    if stored:
        return str(stored)
    started = row["started_at"] or row["created_at"] or ""
    return f"{started}|{row['id']}"


def _validate_resumable_generation(
    row: sqlite3.Row,
    plan: sqlite3.Row,
    *,
    coverage_authority: str,
    listing_identity_sufficient: bool,
) -> None:
    expected = {
        "run_source_plan_id": plan["id"],
        "source_plan_group_id": plan["source_plan_group_id"],
        "source_id": plan["source_id"],
        "binding_id": plan["binding_id"],
        "binding_revision_id": plan["binding_revision_id"],
    }
    mismatches = [
        name
        for name, wanted in expected.items()
        if row[name] is None or str(row[name]) != str(wanted)
    ]
    if mismatches:
        raise CoverageIdentityError(
            "unfinished coverage conflicts with immutable RunSourcePlan: "
            + ", ".join(mismatches)
        )
    if str(row["coverage_authority"]) != str(coverage_authority):
        raise CoverageIdentityError(
            "unfinished coverage authority differs from the resumed pass"
        )
    if bool(row["listing_identity_sufficient"]) != bool(listing_identity_sufficient):
        # v18 deliberately degrades every pre-S3.8 unfinished generation
        # because that generation never durably recorded this barrier bit.
        # Such a generation may be resumed only to preserve/use its positive
        # evidence and finish non-authoritatively; a fresh generation must
        # establish absence authority. New authoritative generations still
        # require an exact match.
        if bool(row["absence_inference_allowed"]):
            raise CoverageIdentityError(
                "unfinished coverage listing-identity sufficiency differs from resumed binding"
            )


def open_coverage(
    conn: sqlite3.Connection,
    *,
    run_source_plan_id: str,
    scope_key: str,
    generation_key: str,
    coverage_authority: str,
    now: str,
    source_id: str | None = None,
    binding_id: str | None = None,
    binding_revision_id: str | None = None,
    listing_identity_sufficient: bool = False,
) -> str:
    """Open one coverage generation pinned to its immutable RunSourcePlan.

    ``source_id``/``binding_id``/``binding_revision_id`` are compatibility
    arguments for older call sites. They are never trusted: when supplied they
    must exactly equal the RunSourcePlan, and persisted identity is always
    derived from that plan.
    """
    if not scope_key:
        raise CoverageIdentityError("coverage scope_key must be non-empty")
    if not generation_key:
        raise CoverageIdentityError("coverage generation_key must be non-empty")

    plan = _plan_identity(conn, run_source_plan_id)
    _assert_optional_identity(
        plan,
        source_id=source_id,
        binding_id=binding_id,
        binding_revision_id=binding_revision_id,
    )

    coverage_id = new_id("cov")
    absence_allowed = coverage_authority in _ABSENCE_AUTHORITIES
    generation_order_key = f"{now}|{coverage_id}"
    conn.execute(
        """
        INSERT INTO enumeration_coverage (
            id, run_source_plan_id, source_plan_group_id, source_id, binding_id,
            binding_revision_id, scope_key, generation_key, coverage_authority,
            absence_inference_allowed, listing_identity_sufficient,
            generation_order_key, started_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            coverage_id,
            plan["id"],
            plan["source_plan_group_id"],
            plan["source_id"],
            plan["binding_id"],
            plan["binding_revision_id"],
            scope_key,
            generation_key,
            coverage_authority,
            1 if absence_allowed else 0,
            1 if listing_identity_sufficient else 0,
            generation_order_key,
            now,
            now,
        ),
    )
    conn.commit()
    return coverage_id


def open_or_resume_coverage(
    conn: sqlite3.Connection,
    *,
    run_source_plan_id: str,
    scope_key: str,
    generation_key: str,
    coverage_authority: str,
    now: str,
    source_id: str | None = None,
    binding_id: str | None = None,
    binding_revision_id: str | None = None,
    listing_identity_sufficient: bool = False,
) -> tuple[str, bool]:
    """Open this pass's generation or resume the one unfinished generation.

    Restart/retry preserves one durable seen union. A finalized generation is
    immutable; a later pass receives a deterministic ``#pass-N`` key. Any
    unfinished generation whose plan/group/binding revision/authority no
    longer matches the immutable plan is refused rather than silently reused.
    """
    plan = _plan_identity(conn, run_source_plan_id)
    _assert_optional_identity(
        plan,
        source_id=source_id,
        binding_id=binding_id,
        binding_revision_id=binding_revision_id,
    )
    existing = conn.execute(
        """
        SELECT * FROM enumeration_coverage
         WHERE run_source_plan_id = ? AND scope_key = ?
         ORDER BY created_at, id
        """,
        (run_source_plan_id, scope_key),
    ).fetchall()
    unfinished = [row for row in existing if row["finalized_at"] is None]
    if len(unfinished) > 1:
        raise CoverageIdentityError(
            "multiple unfinished generations exist for one plan/scope; refusing ambiguous resume"
        )
    if unfinished:
        row = unfinished[0]
        _validate_resumable_generation(
            row,
            plan,
            coverage_authority=coverage_authority,
            listing_identity_sufficient=listing_identity_sufficient,
        )
        return str(row["id"]), True

    taken = {str(row["generation_key"]) for row in existing}
    key = generation_key
    attempt = len(existing) + 1
    while key in taken:
        key = f"{generation_key}#pass-{attempt}"
        attempt += 1
    return (
        open_coverage(
            conn,
            run_source_plan_id=run_source_plan_id,
            scope_key=scope_key,
            generation_key=key,
            coverage_authority=coverage_authority,
            now=now,
            source_id=source_id,
            binding_id=binding_id,
            binding_revision_id=binding_revision_id,
            listing_identity_sufficient=listing_identity_sufficient,
        ),
        False,
    )


def record_contributing_request(
    conn: sqlite3.Connection,
    coverage_id: str,
    request_id: str,
    *,
    commit: bool = True,
) -> bool:
    """Link a request only to coverage owned by the same immutable plan."""
    coverage = conn.execute(
        """
        SELECT run_source_plan_id, source_id, binding_id, finalized_at
          FROM enumeration_coverage WHERE id = ?
        """,
        (coverage_id,),
    ).fetchone()
    if coverage is None:
        raise CoverageIdentityError(f"unknown coverage {coverage_id!r}")
    if coverage["finalized_at"] is not None:
        raise CoverageIdentityError("cannot add a contributor to finalized coverage")
    request = conn.execute(
        """
        SELECT run_source_plan_id, source_id, binding_id
          FROM scrape_requests WHERE id = ?
        """,
        (request_id,),
    ).fetchone()
    if request is None:
        raise CoverageIdentityError(f"unknown request {request_id!r}")
    for name in ("run_source_plan_id", "source_id", "binding_id"):
        if request[name] is None or str(request[name]) != str(coverage[name]):
            raise CoverageIdentityError(
                f"request {request_id!r} {name} does not match coverage generation"
            )
    cur = conn.execute(
        """
        INSERT INTO coverage_contributing_request(coverage_id, request_id)
        VALUES (?, ?)
        ON CONFLICT DO NOTHING
        """,
        (coverage_id, request_id),
    )
    if commit:
        conn.commit()
    return bool(cur.rowcount)


def degrade_coverage(
    conn: sqlite3.Connection,
    coverage_id: str,
    *,
    reason: str,
    commit: bool = True,
) -> None:
    """Irreversibly withdraw absence authority from one open generation."""
    conn.execute(
        """
        UPDATE enumeration_coverage
           SET absence_inference_allowed = 0,
               stop_reason = COALESCE(stop_reason, ?)
         WHERE id = ? AND finalized_at IS NULL AND absence_inference_allowed = 1
        """,
        (reason, coverage_id),
    )
    if commit:
        conn.commit()


def is_coverage_degraded(conn: sqlite3.Connection, coverage_id: str) -> bool:
    row = conn.execute(
        "SELECT coverage_authority, absence_inference_allowed"
        " FROM enumeration_coverage WHERE id = ?",
        (coverage_id,),
    ).fetchone()
    if row is None:
        return False
    return (
        row["coverage_authority"] in _ABSENCE_AUTHORITIES
        and not bool(row["absence_inference_allowed"])
    )


def _record_scope_membership(
    conn: sqlite3.Connection,
    coverage: sqlite3.Row,
    *,
    stable_source_identity: str,
    source_identity_generation: int,
    now: str,
) -> None:
    """Persist explicit authoritative scope membership for a resolved presence.

    The relation is created only from a seen source-native identity under an
    authoritative coverage declaration. Missing legacy rows are *not*
    backfilled by guessing from canonical fields.
    """
    if coverage["coverage_authority"] not in _ABSENCE_AUTHORITIES:
        return
    presence = conn.execute(
        """
        SELECT id
          FROM job_sources
         WHERE source_id = ?
           AND source_job_id = ?
           AND source_identity_generation = ?
        """,
        (
            coverage["source_id"],
            stable_source_identity,
            source_identity_generation,
        ),
    ).fetchone()
    if presence is None:
        # Retained membership can legitimately outlive a canonical projection
        # in fixture/recovery scenarios. The seen union is still valid; absence
        # simply has no source-presence row to affect.
        return

    order_key = _order_key(coverage)
    conn.execute(
        """
        INSERT INTO source_presence_scope_membership(
            job_source_id, binding_revision_id, scope_key,
            first_seen_coverage_id, last_seen_coverage_id,
            last_seen_order_key, last_absence_coverage_id,
            last_absence_order_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        ON CONFLICT(job_source_id, binding_revision_id, scope_key) DO UPDATE SET
            last_seen_coverage_id = CASE
                WHEN excluded.last_seen_order_key > source_presence_scope_membership.last_seen_order_key
                THEN excluded.last_seen_coverage_id
                ELSE source_presence_scope_membership.last_seen_coverage_id END,
            last_seen_order_key = CASE
                WHEN excluded.last_seen_order_key > source_presence_scope_membership.last_seen_order_key
                THEN excluded.last_seen_order_key
                ELSE source_presence_scope_membership.last_seen_order_key END,
            updated_at = CASE
                WHEN excluded.last_seen_order_key > source_presence_scope_membership.last_seen_order_key
                THEN excluded.updated_at
                ELSE source_presence_scope_membership.updated_at END
        """,
        (
            presence["id"],
            coverage["binding_revision_id"],
            coverage["scope_key"],
            coverage["id"],
            coverage["id"],
            order_key,
            now,
            now,
        ),
    )


def record_seen_identity(
    conn: sqlite3.Connection,
    coverage_id: str,
    stable_source_identity: str,
    generation: int = 1,
    evidence_ref: str | None = None,
    *,
    now: str | None = None,
    commit: bool = True,
) -> bool:
    """Add one identity to the generation-wide union and explicit scope map.

    Returns True only when this call inserted a new row into the seen union.
    Replays are idempotent. ``now`` defaults to the coverage start timestamp
    solely for scope-membership bookkeeping; the caller should pass the DB UTC
    observation/verification time when available.
    """
    if not stable_source_identity:
        raise CoverageIdentityError("seen identity must be non-empty")
    coverage = conn.execute(
        "SELECT * FROM enumeration_coverage WHERE id = ?",
        (coverage_id,),
    ).fetchone()
    if coverage is None:
        raise CoverageIdentityError(f"unknown coverage {coverage_id!r}")
    if coverage["finalized_at"] is not None:
        raise CoverageIdentityError("cannot add seen identity to finalized coverage")
    if coverage["binding_revision_id"] is None:
        raise CoverageIdentityError("coverage is missing its pinned binding revision")

    cur = conn.execute(
        """
        INSERT INTO coverage_seen_identity (
            coverage_id, stable_source_identity, source_identity_generation,
            observation_or_listing_evidence_ref)
        VALUES (?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (coverage_id, stable_source_identity, generation, evidence_ref),
    )
    _record_scope_membership(
        conn,
        coverage,
        stable_source_identity=stable_source_identity,
        source_identity_generation=generation,
        now=now or coverage["started_at"] or coverage["created_at"],
    )
    if commit:
        conn.commit()
    return bool(cur.rowcount)


def _payload_role(payload_json: str | None) -> str:
    try:
        value = json.loads(payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(value, dict):
        return ""
    return str(value.get("role") or "").upper()


def _validate_complete_barrier(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    contributors = conn.execute(
        """
        SELECT r.id, r.status, r.run_source_plan_id, r.source_id, r.binding_id
          FROM coverage_contributing_request c
          JOIN scrape_requests r ON r.id = c.request_id
         WHERE c.coverage_id = ?
         ORDER BY r.id
        """,
        (row["id"],),
    ).fetchall()
    if not contributors:
        raise CoverageFinalizationError(
            "COMPLETE requires at least one durable contributing request"
        )
    mismatched = [
        r["id"]
        for r in contributors
        if r["run_source_plan_id"] != row["run_source_plan_id"]
        or r["source_id"] != row["source_id"]
        or r["binding_id"] != row["binding_id"]
    ]
    if mismatched:
        raise CoverageFinalizationError(
            "COMPLETE refuses cross-plan/source/binding contributors: "
            + ", ".join(str(v) for v in mismatched[:5])
        )
    not_accepted_terminal = [r for r in contributors if r["status"] != "SUCCEEDED"]
    if not_accepted_terminal:
        raise CoverageFinalizationError(
            "COMPLETE requires every contributing request to be accepted SUCCEEDED: "
            + ", ".join(
                f"{r['id']}={r['status']}" for r in not_accepted_terminal[:5]
            )
        )

    plan = conn.execute(
        """
        SELECT p.run_id, r.cancel_requested_at, r.status AS run_status
          FROM run_source_plans p
          JOIN scrape_runs r ON r.id = p.run_id
         WHERE p.id = ?
        """,
        (row["run_source_plan_id"],),
    ).fetchone()
    if plan is None:
        raise CoverageFinalizationError("coverage RunSourcePlan no longer resolves")
    if plan["cancel_requested_at"] is not None or plan["run_status"] == "CANCELLED":
        raise CoverageFinalizationError("COMPLETE refused after run cancellation")

    # A continuation can be accepted durably before it has been claimed and
    # linked to the generation. Refuse COMPLETE while any relevant
    # enumeration request remains open. Host-owned ROBOTS/SITEMAP metadata is
    # advisory and cannot itself grant/withhold list-membership authority.
    open_enum = conn.execute(
        """
        SELECT id, status, payload_json
          FROM scrape_requests
         WHERE run_source_plan_id = ?
           AND request_type IN ('LIST_FETCH', 'SOURCE_CRAWL')
           AND status IN ('PENDING', 'RUNNING', 'RETRY_WAIT')
        """,
        (row["run_source_plan_id"],),
    ).fetchall()
    relevant_open_enum = [
        r for r in open_enum if _payload_role(r["payload_json"]) not in _META_CRAWL_ROLES
    ]
    if relevant_open_enum:
        raise CoverageFinalizationError(
            "COMPLETE refuses while enumeration continuation remains open: "
            + ", ".join(
                f"{r['id']}={r['status']}" for r in relevant_open_enum[:5]
            )
        )

    if not bool(row["listing_identity_sufficient"]):
        open_detail = conn.execute(
            """
            SELECT id, status
              FROM scrape_requests
             WHERE run_source_plan_id = ?
               AND request_type = 'DETAIL_FETCH'
               AND status IN ('PENDING', 'RUNNING', 'RETRY_WAIT')
            """,
            (row["run_source_plan_id"],),
        ).fetchall()
        if open_detail:
            raise CoverageFinalizationError(
                "COMPLETE refuses while required detail work remains open: "
                + ", ".join(
                    f"{r['id']}={r['status']}" for r in open_detail[:5]
                )
            )


def finalize_coverage(
    conn: sqlite3.Connection,
    coverage_id: str,
    *,
    completion_state: str,
    stop_reason: str,
    terminal_enumeration_proven: bool,
    now: str,
    pages_completed: int | None = None,
    items_observed: int | None = None,
) -> None:
    """Finalize a generation and apply same-scope absence exactly once.

    Replaying an already-finalized generation with the same terminal facts is
    an idempotent no-op (the per-presence application table is checked again).
    A conflicting re-finalization is rejected because finalized generations
    are immutable.
    """
    if completion_state not in _COMPLETION_STATES:
        raise CoverageFinalizationError(
            f"invalid completion state {completion_state!r}"
        )

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM enumeration_coverage WHERE id = ?",
            (coverage_id,),
        ).fetchone()
        if row is None:
            raise CoverageFinalizationError(f"unknown coverage {coverage_id!r}")
        if row["binding_revision_id"] is None:
            raise CoverageFinalizationError(
                "coverage is missing immutable binding-revision identity"
            )
        if row["finalized_at"] is not None:
            if (
                row["completion_state"] != completion_state
                or bool(row["terminal_enumeration_proven"])
                != bool(terminal_enumeration_proven)
            ):
                raise CoverageFinalizationError(
                    f"coverage {coverage_id!r} is already finalized with different facts"
                )
            if (
                completion_state == "COMPLETE"
                and bool(row["absence_inference_allowed"])
                and row["coverage_authority"] in _ABSENCE_AUTHORITIES
            ):
                _apply_absence(conn, row, now)
                conn.execute(
                    "UPDATE enumeration_coverage SET applied_at = COALESCE(applied_at, ?)"
                    " WHERE id = ?",
                    (now, coverage_id),
                )
            if owns_transaction:
                conn.execute("COMMIT")
            return

        if completion_state == "COMPLETE":
            if not terminal_enumeration_proven:
                raise CoverageFinalizationError(
                    "COMPLETE requires proven terminal enumeration"
                )
            if (
                row["coverage_authority"] in _ABSENCE_AUTHORITIES
                and not bool(row["absence_inference_allowed"])
            ):
                raise CoverageFinalizationError(
                    "COMPLETE refused: this generation was degraded and cannot "
                    "become absence-authoritative"
                )
            _validate_complete_barrier(conn, row)

        contributing = conn.execute(
            "SELECT COUNT(*) FROM coverage_contributing_request WHERE coverage_id = ?",
            (coverage_id,),
        ).fetchone()[0]
        seen_count = conn.execute(
            "SELECT COUNT(*) FROM coverage_seen_identity WHERE coverage_id = ?",
            (coverage_id,),
        ).fetchone()[0]
        effective_items = seen_count if items_observed is None else items_observed

        conn.execute(
            """
            UPDATE enumeration_coverage SET
                finished_at = ?, completion_state = ?, stop_reason = ?,
                pages_completed = COALESCE(?, pages_completed),
                items_observed = ?, cursor_terminal = ?,
                terminal_enumeration_proven = ?, contributing_request_count = ?,
                finalized_at = ?
            WHERE id = ?
            """,
            (
                now,
                completion_state,
                stop_reason,
                pages_completed,
                effective_items,
                1 if terminal_enumeration_proven else 0,
                1 if terminal_enumeration_proven else 0,
                contributing,
                now,
                coverage_id,
            ),
        )
        finalized = conn.execute(
            "SELECT * FROM enumeration_coverage WHERE id = ?",
            (coverage_id,),
        ).fetchone()
        if (
            completion_state == "COMPLETE"
            and bool(finalized["absence_inference_allowed"])
            and finalized["coverage_authority"] in _ABSENCE_AUTHORITIES
        ):
            _apply_absence(conn, finalized, now)
        conn.execute(
            "UPDATE enumeration_coverage SET applied_at = ? WHERE id = ?",
            (now, coverage_id),
        )
        if owns_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if owns_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        raise


def _newer_presence_than_generation(presence: sqlite3.Row, coverage: sqlite3.Row) -> bool:
    started = str(coverage["started_at"] or coverage["created_at"] or "")
    latest_presence = max(
        str(presence["last_seen_at"] or ""),
        str(presence["last_verified_at"] or ""),
    )
    return bool(started and latest_presence and latest_presence > started)


def _record_application(
    conn: sqlite3.Connection,
    *,
    coverage: sqlite3.Row,
    presence: sqlite3.Row,
    decision: str,
    prior_state: str,
    new_state: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO coverage_presence_application(
            coverage_id, job_source_id, binding_revision_id, scope_key,
            decision, prior_presence_state, new_presence_state,
            coverage_order_key, applied_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            coverage["id"],
            presence["job_source_id"],
            coverage["binding_revision_id"],
            coverage["scope_key"],
            decision,
            prior_state,
            new_state,
            _order_key(coverage),
            now,
        ),
    )


def _apply_absence(
    conn: sqlite3.Connection,
    coverage: sqlite3.Row,
    now: str,
) -> None:
    """Apply one COMPLETE authoritative generation to explicit same-scope members.

    S3.8 remains the absence-authority owner; S3.10 supplies the one comparable
    per-presence evidence order.  Coverage generation time is the effective
    source time and ``now`` is local receipt/application time, so an old
    generation finishing late cannot overwrite newer accepted evidence.
    """

    from jobscraper.pipeline.availability import (
        AUTHORITATIVE_ABSENCE,
        LEGACY_ABSENCE,
        apply_presence_evidence,
    )
    from jobscraper.pipeline.obligations import reconcile_job

    seen = {
        (str(r["stable_source_identity"]), int(r["source_identity_generation"]))
        for r in conn.execute(
            """
            SELECT stable_source_identity, source_identity_generation
              FROM coverage_seen_identity WHERE coverage_id = ?
            """,
            (coverage["id"],),
        )
    }
    current_order = _order_key(coverage)
    effective_at = str(coverage["started_at"] or coverage["created_at"] or now)
    members = conn.execute(
        """
        SELECT m.*, js.job_id, js.source_id, js.source_job_id,
               js.source_identity_generation, js.presence_state,
               js.last_seen_at, js.last_verified_at,
               js.last_absence_coverage_id, js.availability_evidence_kind
          FROM source_presence_scope_membership m
          JOIN job_sources js ON js.id = m.job_source_id
         WHERE m.binding_revision_id = ?
           AND m.scope_key = ?
           AND js.source_id = ?
         ORDER BY m.job_source_id
        """,
        (
            coverage["binding_revision_id"],
            coverage["scope_key"],
            coverage["source_id"],
        ),
    ).fetchall()

    for presence in members:
        identity = (
            str(presence["source_job_id"]),
            int(presence["source_identity_generation"]),
        )
        if identity in seen:
            continue
        already = conn.execute(
            """
            SELECT 1 FROM coverage_presence_application
             WHERE coverage_id = ? AND job_source_id = ?
               AND binding_revision_id = ? AND scope_key = ?
            """,
            (
                coverage["id"],
                presence["job_source_id"],
                coverage["binding_revision_id"],
                coverage["scope_key"],
            ),
        ).fetchone()
        if already is not None:
            continue

        prior_state = str(presence["presence_state"])
        decision = "NO_STATE_CHANGE"
        new_state = prior_state
        last_absence_order = str(presence["last_absence_order_key"] or "")

        # Keep the S3.8 scope-generation safeguards.  The S3.10 availability
        # comparator below is the cross-scope/current-evidence authority.
        if str(presence["last_seen_order_key"]) > current_order:
            decision = "SKIPPED_NEWER_PRESENCE"
            result = None
        elif (
            last_absence_order
            and last_absence_order > current_order
            and prior_state in {"CLOSED", "WITHDRAWN"}
        ):
            # S3.10 can accept newer absence while preserving the semantic
            # terminal evidence kind that originally established CLOSED or
            # WITHDRAWN. That accepted absence also advances last_verified_at,
            # so the generic timestamp guard below would otherwise mislabel an
            # older generation as newer positive presence. A genuinely newer
            # accepted positive would have reopened the presence to ACTIVE.
            decision = "SKIPPED_NEWER_ABSENCE"
            result = None
        elif _newer_presence_than_generation(presence, coverage) and str(
            presence["availability_evidence_kind"] or ""
        ) not in {AUTHORITATIVE_ABSENCE, LEGACY_ABSENCE}:
            # Cross-scope/cross-binding current presence is still current
            # presence.  But S3.10 advances last_verified_at when it accepts an
            # absence, so absence-driven verification must not read as newer
            # presence: when the current evidence itself is absence-kind, the
            # absence-order check below owns the attribution.
            decision = "SKIPPED_NEWER_PRESENCE"
            result = None
        elif last_absence_order and last_absence_order > current_order:
            decision = "SKIPPED_NEWER_ABSENCE"
            result = None
        else:
            result = apply_presence_evidence(
                conn,
                presence_id=str(presence["job_source_id"]),
                evidence_kind=AUTHORITATIVE_ABSENCE,
                effective_at=effective_at,
                received_at=now,
                evidence_ref=f"coverage:{coverage['id']}",
                coverage_id=str(coverage["id"]),
                scope_key=str(coverage["scope_key"]),
            )
            new_state = result.new_state
            if not result.accepted:
                decision = (
                    "SKIPPED_NEWER_ABSENCE"
                    if result.current_kind in {"AUTHORITATIVE_ABSENCE", "LEGACY_ABSENCE"}
                    else "SKIPPED_NEWER_PRESENCE"
                )
            elif new_state in {"UNCERTAIN", "EXPIRED"} and new_state != prior_state:
                decision = new_state

        if decision not in {"SKIPPED_NEWER_PRESENCE", "SKIPPED_NEWER_ABSENCE"}:
            conn.execute(
                """
                UPDATE source_presence_scope_membership
                   SET last_absence_coverage_id = ?,
                       last_absence_order_key = ?, updated_at = ?
                 WHERE job_source_id = ? AND binding_revision_id = ?
                   AND scope_key = ?
                   AND (last_absence_order_key IS NULL OR last_absence_order_key < ?)
                """,
                (
                    coverage["id"],
                    current_order,
                    now,
                    presence["job_source_id"],
                    coverage["binding_revision_id"],
                    coverage["scope_key"],
                    current_order,
                ),
            )

        _record_application(
            conn,
            coverage=coverage,
            presence=presence,
            decision=decision,
            prior_state=prior_state,
            new_state=new_state,
            now=now,
        )

        # Missing identities produce no observation and therefore no ordinary
        # RECONCILE obligation.  Recompute locally inside the same coverage
        # transaction whenever S3.10 accepted a new absence revision.
        if result is not None and result.accepted:
            reconcile_job(conn, str(presence["job_id"]), now=now)

__all__ = [
    "CoverageFinalizationError",
    "CoverageIdentityError",
    "degrade_coverage",
    "finalize_coverage",
    "is_coverage_degraded",
    "open_coverage",
    "open_or_resume_coverage",
    "record_contributing_request",
    "record_seen_identity",
]
