"""Idempotent provisioning of immutable routing decision objects (03 §50).

Provisioning is the **host-owned** surface that creates the immutable objects
authorized by a routing decision:

* ``adapter_definitions`` — reusable adapter identity (idempotent, derived
  from the registered adapter's own manifest by
  :func:`ensure_builtin_adapter_definition`)
* ``sources`` — the real-world collection target (idempotent per canonical host)
* ``source_adapter_bindings`` — stable binding identity (idempotent per source + display name)
* ``source_adapter_binding_revisions`` — immutable revision (idempotent per
  binding + adapter + strategy + execution class + pinned adapter config)
* ``ats_fingerprints`` — append-only fingerprint evidence
* ``source_route_decisions`` — append-only route decision evidence

Adapters must not gain direct authority to write these rows — they are
provisioned by this module at the host's direction only.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md §50;
docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md §8, §9.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from jobscraper.ids import new_id
from jobscraper.runtime.clock import db_utc_now


class ProvisioningError(Exception):
    """Raised when a provisioning operation cannot complete safely."""


def _canonical_json(config: Mapping | None) -> str:
    """Deterministic serialization of a pinned adapter config.

    Key order is not identity: two spellings of the same config must pin the
    *same* immutable revision, so serialization is canonical (sorted keys,
    compact separators) exactly like every other durable JSON projection.
    """
    return json.dumps(dict(config or {}), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class BuiltinAdapterDefinition:
    """Result of ensuring one built-in adapter's durable identity row."""

    adapter_id: str
    adapter_version: str
    adapter_api_version: str
    #: ``True`` when this call inserted the row, ``False`` when it already existed
    created: bool


@dataclass(frozen=True)
class ProvisionedSource:
    """Result of provisioning a source and its binding."""
    source_id: str
    binding_id: str
    binding_revision_id: str
    fingerprint_id: str | None = None
    route_decision_id: str | None = None


def _next_revision_number(
    conn: sqlite3.Connection,
    binding_id: str,
    adapter_id: str,
    adapter_version: str,
    strategy: str,
    execution_class: str,
    config_json: str = "{}",
) -> tuple[int, str | None]:
    """Return (next_revision_number, existing_revision_id | None).

    If a binding revision with the exact same adapter + strategy + execution
    class + pinned config already exists, return its revision number and id
    (idempotent).  Otherwise return the next available revision number.

    The config is part of revision identity because a revision is the
    authorization a run replays against (03 §50): re-pointing a provider board
    is a *new* authorization, never an edit of the old one.
    """
    existing = conn.execute(
        "SELECT id, revision FROM source_adapter_binding_revisions"
        " WHERE binding_id = ? AND adapter_id = ? AND adapter_version = ?"
        " AND strategy = ? AND execution_class = ? AND config_json = ?",
        (binding_id, adapter_id, adapter_version, strategy, execution_class,
         config_json),
    ).fetchone()
    if existing:
        return existing["revision"], existing["id"]

    max_rev = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM source_adapter_binding_revisions"
        " WHERE binding_id = ?",
        (binding_id,),
    ).fetchone()[0]
    return max_rev + 1, None


def record_fingerprint(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    url: str,
    fingerprint,  # AtsFingerprint — avoid circular import at module level
    now: str | None = None,
    commit: bool = True,
) -> str:
    """Insert an append-only fingerprint evidence row.

    Parameters
    ----------
    conn:
        Database write connection.
    source_id:
        The source this fingerprint classifies.
    url:
        The URL that was fingerprinted.
    fingerprint:
        The :class:`~jobscraper.adapters.fingerprint.AtsFingerprint` result.
    now:
        Timestamp override (for testing).  Defaults to database UTC.
    commit:
        Whether to commit after the insert.

    Returns
    -------
    str
        The id of the inserted row.
    """
    ts = now or db_utc_now(conn)
    row_id = new_id("fp")
    evidence_json = json.dumps(
        [dict(e) for e in fingerprint.evidence],
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO ats_fingerprints"
        " (id, source_id, url, family, confidence, evidence_json,"
        " recommended_adapter_id, fingerprint_version, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            source_id,
            url,
            fingerprint.family,
            fingerprint.confidence,
            evidence_json,
            fingerprint.recommended_adapter_id,
            fingerprint.fingerprint_version,
            ts,
        ),
    )
    if commit:
        conn.commit()
    return row_id


def record_route_decision(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    fingerprint,  # AtsFingerprint
    decision,  # RouteDecision
    now: str | None = None,
    commit: bool = True,
) -> str:
    """Insert an append-only route decision evidence row.

    Parameters
    ----------
    conn:
        Database write connection.
    source_id:
        The source this decision applies to.
    fingerprint:
        The input fingerprint (recorded for audit).
    decision:
        The :class:`~jobscraper.adapters.router.RouteDecision` result.
    now:
        Timestamp override (for testing).
    commit:
        Whether to commit after the insert.

    Returns
    -------
    str
        The id of the inserted row.
    """
    ts = now or db_utc_now(conn)
    row_id = new_id("rd")
    candidates_json = json.dumps(
        [
            {
                "strategy": c.strategy,
                "execution_class": c.execution_class,
                "adapter_id": c.adapter_id,
                "adapter_version": c.adapter_version,
                "priority": c.priority,
            }
            for c in decision.candidates
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    unsupported_json = json.dumps(
        [
            {
                "strategy": u.strategy,
                "execution_class": u.execution_class,
                "reason": u.reason,
            }
            for u in decision.unsupported_candidates
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO source_route_decisions"
        " (id, source_id, outcome, fingerprint_family, fingerprint_confidence,"
        " candidates_json, unsupported_candidates_json, fallback_reason,"
        " router_version, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            source_id,
            decision.outcome.value,
            decision.fingerprint_family,
            decision.fingerprint_confidence,
            candidates_json,
            unsupported_json,
            decision.fallback_reason,
            decision.router_version,
            ts,
        ),
    )
    if commit:
        conn.commit()
    return row_id


def provision_source_and_binding(
    conn: sqlite3.Connection,
    *,
    display_name: str,
    source_family: str,
    entry_url: str,
    canonical_host: str | None,
    adapter_id: str,
    adapter_version: str,
    strategy: str,
    execution_class: str,
    config: Mapping | None = None,
    now: str | None = None,
    fingerprint=None,  # AtsFingerprint | None
    decision=None,  # RouteDecision | None
    commit: bool = True,
) -> ProvisionedSource:
    """Idempotently provision source, binding, and binding revision.

    Creates (or reuses) the following immutable objects:

    1. ``adapter_definitions`` — the adapter identity row (if missing).
    2. ``sources`` — the source row, keyed by ``canonical_host``.
    3. ``source_adapter_bindings`` — the stable binding identity.
    4. ``source_adapter_binding_revisions`` — one immutable revision per
       (binding, adapter, strategy, execution_class, pinned config)
       combination, pinned as the binding's ``current_revision_id`` so the
       host's run planner can actually select it.
    5. ``ats_fingerprints`` — fingerprint evidence (if provided).
    6. ``source_route_decisions`` — route decision evidence (if provided).

    Parameters
    ----------
    conn:
        Database write connection.
    display_name:
        Human-readable source name.
    source_family:
        Source family classifier (e.g. ``ATS``).
    entry_url:
        The URL used to discover this source.
    canonical_host:
        The canonical host for deduplication.
    adapter_id:
        The adapter id to bind.
    adapter_version:
        The adapter version to bind.
    strategy:
        The acquisition strategy (e.g. ``PROVIDER_NATIVE``).
    execution_class:
        The execution class (e.g. ``HTTP``).
    config:
        The adapter configuration pinned into the immutable revision (e.g.
        ``{"board": "acme"}`` for a provider-native adapter).  It participates
        in revision identity: the same binding with a different config pins a
        new revision and leaves the previous one byte-identical.  ``None`` pins
        an empty config, which is what Slice 1's feed adapter needs.
    now:
        Timestamp override (for testing).
    fingerprint:
        Optional fingerprint to record as evidence.
    decision:
        Optional route decision to record as evidence.
    commit:
        Whether to commit after all inserts.

    Returns
    -------
    ProvisionedSource
        The ids of the provisioned (or reused) objects.

    Raises
    ------
    ProvisioningError
        If a required FK target (e.g. adapter_definitions) does not exist.
    """
    ts = now or db_utc_now(conn)
    config_json = _canonical_json(config)

    # 1. Verify adapter definition exists
    adapter_def = conn.execute(
        "SELECT adapter_id FROM adapter_definitions"
        " WHERE adapter_id = ? AND adapter_version = ?",
        (adapter_id, adapter_version),
    ).fetchone()
    if adapter_def is None:
        raise ProvisioningError(
            f"adapter_definitions row not found for "
            f"(adapter_id={adapter_id!r}, adapter_version={adapter_version!r}). "
            f"Insert it before provisioning a binding."
        )

    # 2. Provision source (idempotent per canonical_host)
    source_id = _provision_source(conn, display_name, source_family, entry_url,
                                  canonical_host, ts)

    # 3. Provision binding (idempotent per source + adapter_id)
    binding_display = adapter_id
    binding_id = _provision_binding(conn, source_id, binding_display, ts)

    # 4. Provision binding revision (idempotent per binding + adapter + strategy)
    rev_number, existing_rev_id = _next_revision_number(
        conn, binding_id, adapter_id, adapter_version, strategy, execution_class,
        config_json,
    )
    if existing_rev_id:
        binding_revision_id = existing_rev_id
    else:
        binding_revision_id = _create_binding_revision(
            conn, binding_id, rev_number, adapter_id, adapter_version,
            strategy, execution_class, ts, config_json=config_json,
        )

    # 4b. Pin it as the binding's current revision.  A revision nobody points
    # at cannot run: the host's run planner selects plans through
    # ``source_adapter_bindings.current_revision_id``, so provisioning — the
    # surface that just authorized this adapter+config for this source — is
    # also the surface that makes it the binding's live authorization.  The
    # binding row is mutable identity/state (only *revisions* are immutable),
    # the pointer move is idempotent, and the previous revision row stays
    # byte-identical and inspectable.  (``source_adapter_bindings`` carries no
    # ``updated_at``: the revision rows are the timeline.)
    conn.execute(
        "UPDATE source_adapter_bindings SET current_revision_id = ?"
        " WHERE id = ? AND (current_revision_id IS NULL OR current_revision_id != ?)",
        (binding_revision_id, binding_id, binding_revision_id),
    )

    # 5. Record fingerprint evidence (append-only, always a new row)
    fingerprint_id = None
    if fingerprint is not None:
        fingerprint_id = record_fingerprint(
            conn, source_id=source_id, url=entry_url,
            fingerprint=fingerprint, now=ts, commit=False,
        )

    # 6. Record route decision evidence (append-only, always a new row)
    route_decision_id = None
    if decision is not None:
        route_decision_id = record_route_decision(
            conn, source_id=source_id, fingerprint=fingerprint,
            decision=decision, now=ts, commit=False,
        )

    if commit:
        conn.commit()

    return ProvisionedSource(
        source_id=source_id,
        binding_id=binding_id,
        binding_revision_id=binding_revision_id,
        fingerprint_id=fingerprint_id,
        route_decision_id=route_decision_id,
    )


def _provision_source(
    conn: sqlite3.Connection,
    display_name: str,
    source_family: str,
    entry_url: str,
    canonical_host: str | None,
    ts: str,
) -> str:
    """Insert or reuse a source row (idempotent per canonical_host)."""
    if canonical_host:
        existing = conn.execute(
            "SELECT id FROM sources WHERE canonical_host = ?",
            (canonical_host,),
        ).fetchone()
        if existing:
            return existing["id"]

    source_id = new_id("src")
    conn.execute(
        "INSERT INTO sources"
        " (id, display_name, source_family, entry_url, canonical_host,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, display_name, source_family, entry_url, canonical_host,
         ts, ts),
    )
    return source_id


def _provision_binding(
    conn: sqlite3.Connection,
    source_id: str,
    display_name: str,
    ts: str,
) -> str:
    """Insert or reuse a binding row (idempotent per source + display_name)."""
    existing = conn.execute(
        "SELECT id FROM source_adapter_bindings"
        " WHERE source_id = ? AND display_name = ?",
        (source_id, display_name),
    ).fetchone()
    if existing:
        return existing["id"]

    binding_id = new_id("bnd")
    conn.execute(
        "INSERT INTO source_adapter_bindings"
        " (id, source_id, display_name, created_at)"
        " VALUES (?, ?, ?, ?)",
        (binding_id, source_id, display_name, ts),
    )
    return binding_id


def _create_binding_revision(
    conn: sqlite3.Connection,
    binding_id: str,
    revision: int,
    adapter_id: str,
    adapter_version: str,
    strategy: str,
    execution_class: str,
    ts: str,
    config_json: str = "{}",
) -> str:
    """Create a new immutable binding revision.

    Requires a default permission profile to exist.
    """
    # Find the default permission profile
    perm_profile = conn.execute(
        "SELECT id FROM adapter_permission_profiles LIMIT 1",
    ).fetchone()
    if perm_profile is None:
        raise ProvisioningError(
            "no adapter_permission_profiles row exists — "
            "cannot create a binding revision without a permission profile"
        )
    perm_profile_id = perm_profile["id"]

    perm_rev = conn.execute(
        "SELECT revision FROM adapter_permission_profile_revisions"
        " WHERE permission_profile_id = ? ORDER BY revision DESC LIMIT 1",
        (perm_profile_id,),
    ).fetchone()
    if perm_rev is None:
        raise ProvisioningError(
            f"no revision for permission profile {perm_profile_id!r}"
        )
    perm_revision = perm_rev["revision"]

    revision_id = new_id("bndrev")
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id, binding_id, revision, adapter_id, adapter_version, strategy,"
        " priority, config_json, auth_requirement, auth_scope_id,"
        " execution_class, permission_profile_id,"
        " permission_profile_revision, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'NONE', NULL, ?, ?, ?, ?)",
        (
            revision_id,
            binding_id,
            revision,
            adapter_id,
            adapter_version,
            strategy,
            config_json,
            execution_class,
            perm_profile_id,
            perm_revision,
            ts,
        ),
    )
    return revision_id


def ensure_builtin_adapter_definition(
    conn: sqlite3.Connection,
    adapter_id: str,
    *,
    adapter_version: str | None = None,
    now: str | None = None,
    commit: bool = True,
) -> BuiltinAdapterDefinition:
    """Idempotently pin one built-in adapter's durable identity row.

    The row is derived from the adapter's own manifest as registered in
    :mod:`jobscraper.adapters.registry` — never from caller-supplied strings.
    That keeps ``adapter_definitions`` (what the durable record authorizes)
    and the code that runs (what the registry can build) in agreement, so
    provenance written under an ``adapter_id``/``adapter_version`` pair always
    names a manifest that existed (02 §8, ARC-04.3).

    An existing row is reused byte-for-byte: adapter identity rows are
    append-only, so a previously pinned identity is never rewritten.

    Raises
    ------
    ProvisioningError
        If the adapter is not registered as built-in, or if ``adapter_version``
        names a version the registered manifest does not declare.
    """
    from jobscraper.adapters.contract import validate_manifest
    from jobscraper.adapters.registry import BUILTIN_ADAPTERS

    adapter_cls = BUILTIN_ADAPTERS.get(adapter_id)
    if adapter_cls is None:
        raise ProvisioningError(
            f"no built-in adapter registered for adapter_id={adapter_id!r}"
            f" (registered: {sorted(BUILTIN_ADAPTERS)})"
        )
    manifest = validate_manifest(dataclasses.asdict(adapter_cls.manifest))
    if adapter_version is not None and adapter_version != manifest.version:
        raise ProvisioningError(
            f"adapter {adapter_id!r} manifest declares version"
            f" {manifest.version!r}, not {adapter_version!r}"
        )
    ts = now or db_utc_now(conn)
    manifest_json = _canonical_json(dataclasses.asdict(manifest))

    existing = conn.execute(
        "SELECT adapter_version, adapter_api_version FROM adapter_definitions"
        " WHERE adapter_id = ? AND adapter_version = ?",
        (adapter_id, manifest.version),
    ).fetchone()
    if existing is not None:
        return BuiltinAdapterDefinition(
            adapter_id=adapter_id,
            adapter_version=existing["adapter_version"],
            adapter_api_version=existing["adapter_api_version"],
            created=False,
        )
    conn.execute(
        "INSERT INTO adapter_definitions (adapter_id, adapter_version,"
        " adapter_api_version, manifest_json, is_builtin, created_at)"
        " VALUES (?, ?, ?, ?, 1, ?)",
        (adapter_id, manifest.version, manifest.adapter_api_version,
         manifest_json, ts),
    )
    if commit:
        conn.commit()
    return BuiltinAdapterDefinition(
        adapter_id=adapter_id,
        adapter_version=manifest.version,
        adapter_api_version=manifest.adapter_api_version,
        created=True,
    )


__all__ = [
    "BuiltinAdapterDefinition",
    "ProvisionedSource",
    "ProvisioningError",
    "ensure_builtin_adapter_definition",
    "provision_source_and_binding",
    "record_fingerprint",
    "record_route_decision",
]
