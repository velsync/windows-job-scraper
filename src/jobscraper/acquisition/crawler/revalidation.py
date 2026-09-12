"""Durable compatible HTTP revalidation cache and 304 membership reuse (S3.7).

A 304 is never an empty page. Conditional validators are attached only by the
host after resolving a retained representation whose immutable identity is
compatible with the current RunSourcePlan/request variant/parser contract.
Authoritative list reuse additionally requires durable retained membership.

The module is network-inert. It plans headers and owns cache/hold persistence;
all I/O continues through the ordinary service-owned HTTP dispatcher.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.ids import new_id
from jobscraper.net.urlnorm import normalize_url


_CONDITIONAL_HEADERS = frozenset({"if-none-match", "if-modified-since"})
_CACHEABLE_PAGE_CLASSES = frozenset({"VALID_LIST", "VALID_JOB", "EMPTY"})
_PUBLIC_AUTH_REF = "auth://public-none"


class RevalidationCompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class CachedMembership:
    stable_source_identity: str
    source_identity_generation: int = 1
    evidence_ref: str | None = None


@dataclass(frozen=True)
class RevalidationPreparation:
    request_plan: RequestPlan
    representation_id: str | None = None
    hold_id: str | None = None
    conditional: bool = False
    reason: str = "NO_COMPATIBLE_REPRESENTATION"


@dataclass(frozen=True)
class RevalidationReuse:
    accepted: bool
    reason: str
    representation_id: str | None = None
    validated_page_class: str | None = None
    body: bytes | None = None
    content_type: str | None = None
    body_hash: str | None = None
    normalized_content_hash: str | None = None
    membership: tuple[CachedMembership, ...] = ()


def _row_get(row: Mapping[str, object], key: str):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise RevalidationCompatibilityError(f"plan is missing {key}") from exc


def auth_scope_reference(plan_row: Mapping[str, object]) -> str:
    value = _row_get(plan_row, "auth_scope_id")
    return str(value) if value else _PUBLIC_AUTH_REF


def parser_recipe_compatibility_key(
    plan_row: Mapping[str, object], *, normalization_version: str | int
) -> str:
    material = [
        str(_row_get(plan_row, "adapter_id")),
        str(_row_get(plan_row, "adapter_version")),
        str(_row_get(plan_row, "recipe_version_id") or ""),
        int(_row_get(plan_row, "cursor_schema_version")),
        str(normalization_version),
    ]
    encoded = json.dumps(material, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def request_variant_key(plan: RequestPlan) -> str:
    """Stable request variant excluding host-owned conditional validators."""

    headers = sorted(
        (str(k).lower(), str(v))
        for k, v in plan.headers.items()
        if str(k).lower() not in _CONDITIONAL_HEADERS
    )
    material = [
        plan.method.upper(),
        normalize_url(plan.url).normalized,
        headers,
        plan.purpose,
        tuple(plan.expected_content_types),
    ]
    encoded = json.dumps(material, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            text = str(value).strip()
            return text or None
    return None


def _cleanup_stale_revalidation_holds(
    conn: sqlite3.Connection, *, now: str, commit: bool = False
) -> None:
    conn.execute(
        """
        UPDATE cache_representation_hold
           SET released_at = COALESCE(released_at, ?)
         WHERE released_at IS NULL
           AND owner_kind = 'REVALIDATION'
           AND owner_attempt_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM request_attempts a
                WHERE a.attempt_id = cache_representation_hold.owner_attempt_id
                  AND a.outcome IS NOT NULL
           )
        """,
        (now,),
    )
    if commit:
        conn.commit()


def _acquire_hold(
    conn: sqlite3.Connection,
    representation_id: str,
    *,
    owner_kind: str,
    owner_ref: str,
    owner_attempt_id: str | None,
    now: str,
    commit: bool = True,
) -> str:
    if owner_kind not in {"REVALIDATION", "BACKUP"}:
        raise ValueError("unsupported cache hold owner kind")
    owns_transaction = not conn.in_transaction
    if commit and owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        hold_id = new_id("crh")
        conn.execute(
            """
            INSERT INTO cache_representation_hold(
                id, representation_id, owner_kind, owner_ref,
                owner_attempt_id, created_at, released_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT DO NOTHING
            """,
            (
                hold_id,
                representation_id,
                owner_kind,
                owner_ref,
                owner_attempt_id,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT id FROM cache_representation_hold
             WHERE representation_id = ? AND owner_kind = ? AND owner_ref = ?
               AND released_at IS NULL
            """,
            (representation_id, owner_kind, owner_ref),
        ).fetchone()
        if row is None:
            raise RevalidationCompatibilityError(
                "failed to acquire cache representation hold"
            )
        if commit and owns_transaction:
            conn.execute("COMMIT")
        return str(row["id"])
    except BaseException:
        if commit and owns_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        raise

def acquire_backup_hold(
    conn: sqlite3.Connection,
    representation_id: str,
    *,
    backup_ref: str,
    now: str,
    commit: bool = True,
) -> str:
    return _acquire_hold(
        conn,
        representation_id,
        owner_kind="BACKUP",
        owner_ref=backup_ref,
        owner_attempt_id=None,
        now=now,
        commit=commit,
    )


def release_hold(
    conn: sqlite3.Connection,
    hold_id: str | None,
    *,
    now: str,
    commit: bool = True,
) -> None:
    if hold_id:
        conn.execute(
            "UPDATE cache_representation_hold SET released_at = COALESCE(released_at, ?)"
            " WHERE id = ?",
            (now, hold_id),
        )
        if commit:
            conn.commit()


def prepare_revalidation(
    conn: sqlite3.Connection,
    *,
    plan_row: Mapping[str, object],
    request_plan: RequestPlan,
    attempt_id: str,
    expected_page_classes: Sequence[str],
    require_membership: bool,
    normalization_version: str | int,
    now: str,
    force_unconditional: bool = False,
) -> RevalidationPreparation:
    """Attach validators only while atomically acquiring the representation hold."""

    incoming_conditionals = [
        name for name in request_plan.headers if name.lower() in _CONDITIONAL_HEADERS
    ]
    if incoming_conditionals:
        raise RevalidationCompatibilityError(
            "conditional request headers are host-owned; adapter supplied "
            + ", ".join(sorted(incoming_conditionals))
        )

    if force_unconditional:
        return RevalidationPreparation(
            replace(
                request_plan,
                headers={
                    k: v
                    for k, v in request_plan.headers.items()
                    if k.lower() not in _CONDITIONAL_HEADERS
                },
                revalidation_headers_allowed=False,
                cache_policy="UNCONDITIONAL",
            ),
            reason="FORCED_UNCONDITIONAL",
        )

    allowed_classes = tuple(
        c for c in expected_page_classes if c in _CACHEABLE_PAGE_CLASSES
    )
    if not allowed_classes:
        return RevalidationPreparation(request_plan, reason="PAGE_CLASS_NOT_CACHEABLE")

    # Selection + hold acquisition is one bounded write transaction. This
    # prevents a retention worker from pruning the chosen body after lookup
    # but before the hold becomes durable. Join a caller transaction if one
    # already exists; never commit unrelated caller work.
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _cleanup_stale_revalidation_holds(conn, now=now, commit=False)
        variant = request_variant_key(request_plan)
        parser_key = parser_recipe_compatibility_key(
            plan_row, normalization_version=normalization_version
        )
        auth_ref = auth_scope_reference(plan_row)
        placeholders = ",".join("?" for _ in allowed_classes)
        sql = f"""
            SELECT *
              FROM cache_representation
             WHERE source_id = ?
               AND binding_revision_id = ?
               AND auth_scope_ref = ?
               AND request_variant_key = ?
               AND parser_recipe_compatibility_key = ?
               AND validated_page_class IN ({placeholders})
               AND superseded_at IS NULL
               AND pruned_at IS NULL
               AND body_blob IS NOT NULL
               {"AND membership_complete = 1 AND membership_ref IS NOT NULL" if require_membership else ""}
             ORDER BY last_verified_at DESC, stored_at DESC, id DESC
        """
        rows = conn.execute(
            sql,
            (
                str(_row_get(plan_row, "source_id")),
                str(_row_get(plan_row, "binding_revision_id")),
                auth_ref,
                variant,
                parser_key,
                *allowed_classes,
            ),
        ).fetchall()
        chosen = next(
            (row for row in rows if row["etag"] or row["last_modified"]),
            None,
        )
        if chosen is None:
            if owns_transaction:
                conn.execute("COMMIT")
            return RevalidationPreparation(request_plan, reason="NO_USABLE_VALIDATORS")

        hold_id = _acquire_hold(
            conn,
            str(chosen["id"]),
            owner_kind="REVALIDATION",
            owner_ref=attempt_id,
            owner_attempt_id=attempt_id,
            now=now,
            commit=False,
        )
        headers = dict(request_plan.headers)
        if chosen["etag"]:
            headers["If-None-Match"] = str(chosen["etag"])
        if chosen["last_modified"]:
            headers["If-Modified-Since"] = str(chosen["last_modified"])
        planned = replace(
            request_plan,
            headers=headers,
            revalidation_headers_allowed=True,
            cache_policy="REVALIDATE",
            auth_scope_ref=auth_ref,
        )
        if owns_transaction:
            conn.execute("COMMIT")
        return RevalidationPreparation(
            planned,
            representation_id=str(chosen["id"]),
            hold_id=hold_id,
            conditional=True,
            reason="CONDITIONAL",
        )
    except BaseException:
        if owns_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        raise

def resolve_304(
    conn: sqlite3.Connection,
    *,
    preparation: RevalidationPreparation,
    plan_row: Mapping[str, object],
    request_plan: RequestPlan,
    expected_page_classes: Sequence[str],
    require_membership: bool,
    normalization_version: str | int,
) -> RevalidationReuse:
    if not preparation.conditional or not preparation.representation_id:
        return RevalidationReuse(False, "304_WITHOUT_HOST_CACHE_BINDING")

    row = conn.execute(
        "SELECT * FROM cache_representation WHERE id = ?",
        (preparation.representation_id,),
    ).fetchone()
    if row is None:
        return RevalidationReuse(False, "CACHE_REPRESENTATION_MISSING")
    if not preparation.hold_id:
        return RevalidationReuse(False, "CACHE_HOLD_MISSING")
    hold = conn.execute(
        "SELECT 1 FROM cache_representation_hold"
        " WHERE id = ? AND representation_id = ? AND released_at IS NULL",
        (preparation.hold_id, preparation.representation_id),
    ).fetchone()
    if hold is None:
        return RevalidationReuse(False, "CACHE_HOLD_NOT_ACTIVE")

    expected = {
        "source_id": str(_row_get(plan_row, "source_id")),
        "binding_revision_id": str(_row_get(plan_row, "binding_revision_id")),
        "auth_scope_ref": auth_scope_reference(plan_row),
        "request_variant_key": request_variant_key(request_plan),
        "parser_recipe_compatibility_key": parser_recipe_compatibility_key(
            plan_row, normalization_version=normalization_version
        ),
    }
    for name, wanted in expected.items():
        if str(row[name]) != wanted:
            return RevalidationReuse(False, f"CACHE_IDENTITY_MISMATCH:{name}")
    if row["validated_page_class"] not in set(expected_page_classes):
        return RevalidationReuse(False, "CACHE_PAGE_CLASS_MISMATCH")
    if row["pruned_at"] is not None or row["body_blob"] is None:
        return RevalidationReuse(False, "CACHE_BODY_PRUNED")
    if require_membership and (
        not bool(row["membership_complete"]) or not row["membership_ref"]
    ):
        return RevalidationReuse(False, "CACHE_MEMBERSHIP_MISSING")

    members = tuple(
        CachedMembership(
            str(member["stable_source_identity"]),
            int(member["source_identity_generation"]),
            member["evidence_ref"],
        )
        for member in conn.execute(
            """
            SELECT stable_source_identity, source_identity_generation, evidence_ref
              FROM cache_representation_membership
             WHERE representation_id = ?
             ORDER BY stable_source_identity, source_identity_generation
            """,
            (row["id"],),
        )
    )
    return RevalidationReuse(
        True,
        "COMPATIBLE_304",
        representation_id=str(row["id"]),
        validated_page_class=str(row["validated_page_class"]),
        body=bytes(row["body_blob"]),
        content_type=row["content_type"],
        body_hash=str(row["body_hash"]),
        normalized_content_hash=str(row["normalized_content_hash"]),
        membership=members,
    )


def _membership_rows(observations: Iterable[object]) -> tuple[CachedMembership, ...]:
    seen: dict[tuple[str, int], CachedMembership] = {}
    for observation in observations:
        identity = getattr(observation, "source_job_id", None)
        if not identity:
            continue
        generation = 1
        key = (str(identity), generation)
        seen[key] = CachedMembership(
            str(identity),
            generation,
            getattr(observation, "raw_url", None),
        )
    return tuple(seen[key] for key in sorted(seen))


def store_representation(
    conn: sqlite3.Connection,
    *,
    plan_row: Mapping[str, object],
    request_plan: RequestPlan,
    validated_page_class: str,
    body: bytes,
    body_hash: str | None,
    normalized_content_hash: str | None,
    content_type: str | None,
    response_headers: Mapping[str, str],
    observations: Iterable[object],
    membership_complete: bool,
    normalization_version: str | int,
    now: str,
    commit: bool = True,
) -> str | None:
    """Store/update one accepted 2xx representation under the caller's fence."""

    if validated_page_class not in _CACHEABLE_PAGE_CLASSES:
        return None
    if body_hash is None or normalized_content_hash is None:
        return None
    if body is None:
        return None

    variant = request_variant_key(request_plan)
    parser_key = parser_recipe_compatibility_key(
        plan_row, normalization_version=normalization_version
    )
    auth_ref = auth_scope_reference(plan_row)
    source_id = str(_row_get(plan_row, "source_id"))
    binding_revision_id = str(_row_get(plan_row, "binding_revision_id"))
    etag = _header(response_headers, "ETag")
    last_modified = _header(response_headers, "Last-Modified")
    members = _membership_rows(observations)
    row = conn.execute(
        """
        SELECT id FROM cache_representation
         WHERE source_id = ? AND binding_revision_id = ? AND auth_scope_ref = ?
           AND request_variant_key = ? AND validated_page_class = ?
           AND normalized_content_hash = ? AND parser_recipe_compatibility_key = ?
        """,
        (
            source_id,
            binding_revision_id,
            auth_ref,
            variant,
            validated_page_class,
            normalized_content_hash,
            parser_key,
        ),
    ).fetchone()
    representation_id = str(row["id"]) if row is not None else new_id("cr")
    membership_ref = (
        f"cache-membership://{representation_id}" if membership_complete else None
    )

    if row is None:
        conn.execute(
            """
            INSERT INTO cache_representation(
                id, source_id, binding_revision_id, auth_scope_ref,
                request_variant_key, validated_page_class, body_hash,
                normalized_content_hash, content_type, body_blob,
                parser_recipe_compatibility_key, membership_ref,
                membership_complete, etag, last_modified,
                stored_at, last_verified_at, superseded_at, pruned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                representation_id,
                source_id,
                binding_revision_id,
                auth_ref,
                variant,
                validated_page_class,
                body_hash,
                normalized_content_hash,
                content_type,
                sqlite3.Binary(bytes(body)),
                parser_key,
                membership_ref,
                1 if membership_complete else 0,
                etag,
                last_modified,
                now,
                now,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE cache_representation
               SET body_hash = ?, normalized_content_hash = ?,
                   content_type = ?, body_blob = ?,
                   membership_ref = ?, membership_complete = ?,
                   etag = ?, last_modified = ?, last_verified_at = ?,
                   superseded_at = NULL, pruned_at = NULL
             WHERE id = ?
            """,
            (
                body_hash,
                normalized_content_hash,
                content_type,
                sqlite3.Binary(bytes(body)),
                membership_ref,
                1 if membership_complete else 0,
                etag,
                last_modified,
                now,
                representation_id,
            ),
        )

    # The newest accepted body becomes the current retained version for this
    # exact request/parser identity. Older rows remain explainable history.
    conn.execute(
        """
        UPDATE cache_representation
           SET superseded_at = COALESCE(superseded_at, ?)
         WHERE source_id = ? AND binding_revision_id = ? AND auth_scope_ref = ?
           AND request_variant_key = ? AND validated_page_class = ?
           AND parser_recipe_compatibility_key = ?
           AND id <> ? AND superseded_at IS NULL
        """,
        (
            now,
            source_id,
            binding_revision_id,
            auth_ref,
            variant,
            validated_page_class,
            parser_key,
            representation_id,
        ),
    )

    conn.execute(
        "DELETE FROM cache_representation_membership WHERE representation_id = ?",
        (representation_id,),
    )
    for member in members:
        conn.execute(
            """
            INSERT INTO cache_representation_membership(
                representation_id, stable_source_identity,
                source_identity_generation, evidence_ref)
            VALUES (?, ?, ?, ?)
            """,
            (
                representation_id,
                member.stable_source_identity,
                member.source_identity_generation,
                member.evidence_ref,
            ),
        )
    if commit:
        conn.commit()
    return representation_id


def bind_fetch_representation(
    conn: sqlite3.Connection,
    fetch_attempt_id: str,
    representation_id: str,
    *,
    commit: bool = True,
) -> None:
    conn.execute(
        "UPDATE fetch_attempts SET cache_representation_id = ? WHERE id = ?",
        (representation_id, fetch_attempt_id),
    )
    if commit:
        conn.commit()


def touch_representation(
    conn: sqlite3.Connection,
    representation_id: str,
    *,
    now: str,
    commit: bool = True,
) -> None:
    conn.execute(
        "UPDATE cache_representation SET last_verified_at = ? WHERE id = ?",
        (now, representation_id),
    )
    if commit:
        conn.commit()


def restore_membership(
    conn: sqlite3.Connection,
    *,
    representation_id: str,
    coverage_id: str,
    plan_row: Mapping[str, object],
    now: str,
    commit: bool = True,
) -> int:
    """Reuse retained seen identities without granting any new scope authority."""

    representation = conn.execute(
        "SELECT * FROM cache_representation WHERE id = ?",
        (representation_id,),
    ).fetchone()
    if representation is None or not bool(representation["membership_complete"]):
        raise RevalidationCompatibilityError("retained membership is not complete")
    if (
        representation["source_id"] != _row_get(plan_row, "source_id")
        or representation["binding_revision_id"]
        != _row_get(plan_row, "binding_revision_id")
    ):
        raise RevalidationCompatibilityError("membership binding/source mismatch")

    coverage = conn.execute(
        "SELECT run_source_plan_id, source_id, binding_id, binding_revision_id"
        " FROM enumeration_coverage WHERE id = ?",
        (coverage_id,),
    ).fetchone()
    if coverage is None:
        raise RevalidationCompatibilityError("coverage generation missing")
    if (
        coverage["run_source_plan_id"] != _row_get(plan_row, "id")
        or coverage["source_id"] != _row_get(plan_row, "source_id")
        or coverage["binding_id"] != _row_get(plan_row, "binding_id")
        or coverage["binding_revision_id"] != _row_get(plan_row, "binding_revision_id")
    ):
        raise RevalidationCompatibilityError(
            "coverage generation does not match immutable RunSourcePlan"
        )

    inserted = 0
    members = conn.execute(
        """
        SELECT stable_source_identity, source_identity_generation, evidence_ref
          FROM cache_representation_membership
         WHERE representation_id = ?
        """,
        (representation_id,),
    ).fetchall()
    # S3.8 keeps coverage_seen_identity + explicit same-scope membership under
    # the single coverage owner; 304 reuse cannot bypass that authority path.
    from jobscraper.pipeline.coverage import record_seen_identity

    for member in members:
        was_inserted = record_seen_identity(
            conn,
            coverage_id,
            member["stable_source_identity"],
            generation=member["source_identity_generation"],
            evidence_ref=(
                member["evidence_ref"]
                or f"cache-membership://{representation_id}"
            ),
            now=now,
            commit=False,
        )
        inserted += 1 if was_inserted else 0
        # S3.10 owns the current presence projection for retained-membership
        # verification as well. A 304 is positive current evidence, but it still
        # passes the same temporal comparator as parsed observations.
        from jobscraper.pipeline.availability import (
            ACTIVE_REVALIDATION,
            apply_presence_evidence,
        )
        from jobscraper.pipeline.obligations import reconcile_job

        presence = conn.execute(
            """
            SELECT id
              FROM job_sources
             WHERE source_id = ?
               AND source_job_id = ?
               AND source_identity_generation = ?
            """,
            (
                representation["source_id"],
                member["stable_source_identity"],
                member["source_identity_generation"],
            ),
        ).fetchone()
        if presence is not None:
            result = apply_presence_evidence(
                conn,
                presence_id=str(presence["id"]),
                evidence_kind=ACTIVE_REVALIDATION,
                effective_at=now,
                received_at=now,
                evidence_ref=(
                    member["evidence_ref"]
                    or f"cache-membership://{representation_id}"
                ),
            )
            if result.accepted:
                reconcile_job(conn, result.job_id, now=now)

    if commit:
        conn.commit()
    return inserted


def prune_representation(
    conn: sqlite3.Connection,
    representation_id: str,
    *,
    now: str,
    commit: bool = True,
) -> bool:
    """Prune retained bytes/membership only when no live revalidation/backup hold exists."""

    _cleanup_stale_revalidation_holds(conn, now=now, commit=False)
    live_hold = conn.execute(
        """
        SELECT 1 FROM cache_representation_hold
         WHERE representation_id = ? AND released_at IS NULL
         LIMIT 1
        """,
        (representation_id,),
    ).fetchone()
    if live_hold is not None:
        if commit:
            conn.commit()
        return False
    conn.execute(
        "DELETE FROM cache_representation_membership WHERE representation_id = ?",
        (representation_id,),
    )
    conn.execute(
        """
        UPDATE cache_representation
           SET body_blob = NULL, membership_ref = NULL,
               membership_complete = 0, pruned_at = ?
         WHERE id = ?
        """,
        (now, representation_id),
    )
    if commit:
        conn.commit()
    return True


__all__ = [
    "CachedMembership",
    "RevalidationCompatibilityError",
    "RevalidationPreparation",
    "RevalidationReuse",
    "acquire_backup_hold",
    "auth_scope_reference",
    "bind_fetch_representation",
    "parser_recipe_compatibility_key",
    "prepare_revalidation",
    "prune_representation",
    "release_hold",
    "request_variant_key",
    "resolve_304",
    "restore_membership",
    "store_representation",
    "touch_representation",
]
