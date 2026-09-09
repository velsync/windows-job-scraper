"""Host-owned provisioning of immutable source/routing objects (02 §8/§9, 03 §50).

A ``Source`` is one real collection target (one board/feed/careers target), not
a shared provider hostname. Adapter configuration is validated before it can
become current runnable authority. Built-in adapter-definition conflicts are
reported explicitly and cannot be activated; rows remain append-only.

Released schema bytes are unchanged. ``canonical_host`` remains descriptive
metadata; source idempotency is derived from the normalized entry target.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from jobscraper.ids import new_id
from jobscraper.net.urlnorm import UrlNormalizationError, normalize_url
from jobscraper.runtime.clock import db_utc_now


class ProvisioningError(Exception):
    """Raised when a provisioning operation cannot complete safely."""


def _canonical_json(config: Mapping | None) -> str:
    return json.dumps(dict(config or {}), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class BuiltinAdapterDefinition:
    adapter_id: str
    adapter_version: str
    adapter_api_version: str
    created: bool
    #: False means an append-only pre-existing row conflicts with this build's
    #: registered manifest. It is retained for history but cannot be activated.
    verified: bool = True
    conflict_reason: str | None = None


@dataclass(frozen=True)
class ProvisionedSource:
    source_id: str
    binding_id: str
    binding_revision_id: str
    fingerprint_id: str | None = None
    route_decision_id: str | None = None


def _source_target_key(value: str) -> str:
    """Stable identity for one collection target, preserving board/path scope."""
    try:
        return normalize_url(value).normalized
    except (UrlNormalizationError, ValueError, TypeError) as exc:
        raise ProvisioningError(f"entry_url is not a usable source target: {exc}") from exc


def _require_existing_source(conn: sqlite3.Connection, source_id: str) -> None:
    if conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
        raise ProvisioningError(f"Source {source_id!r} does not exist; refusing orphan evidence")


def _validate_fingerprint_decision(fingerprint, decision) -> None:
    if decision is None:
        return
    if fingerprint is None:
        raise ProvisioningError("route decision requires fingerprint evidence")
    if decision.fingerprint_family != fingerprint.family:
        raise ProvisioningError("route decision fingerprint family does not match fingerprint evidence")
    if decision.fingerprint_confidence != fingerprint.confidence:
        raise ProvisioningError("route decision fingerprint confidence does not match fingerprint evidence")


def _registered_definition_state(
    conn: sqlite3.Connection,
    adapter_id: str,
    adapter_version: str,
) -> tuple[bool, str | None]:
    """Check the stored built-in identity against this process's registry.

    Slice 1's `json_api_feed` predates manifest-derived provisioning and may
    have a historical `{}` manifest row. That one legacy representation remains
    runnable for backwards compatibility; all S2.5+ manifest-derived built-ins
    are exact-match/fail-closed.
    """
    from jobscraper.adapters.contract import validate_manifest
    from jobscraper.adapters.registry import BUILTIN_ADAPTERS

    adapter_cls = BUILTIN_ADAPTERS.get(adapter_id)
    if adapter_cls is None:
        return True, None  # FK/external definition policy is owned elsewhere
    row = conn.execute(
        "SELECT adapter_api_version, manifest_json, is_builtin FROM adapter_definitions"
        " WHERE adapter_id = ? AND adapter_version = ?",
        (adapter_id, adapter_version),
    ).fetchone()
    if row is None:
        return False, "adapter definition is missing"

    manifest = validate_manifest(dataclasses.asdict(adapter_cls.manifest))
    if manifest.version != adapter_version:
        return False, "registered manifest version disagrees with durable identity"

    # Accepted Slice-1 compatibility representation. New provider adapters do
    # not get this exception.
    if adapter_id == "json_api_feed" and (row["manifest_json"] or "").strip() == "{}":
        return True, None

    try:
        stored = json.loads(row["manifest_json"])
        stored_json = _canonical_json(stored)
    except (ValueError, TypeError):
        return False, "stored manifest is not a canonical JSON object"
    expected_json = _canonical_json(dataclasses.asdict(manifest))
    if int(row["is_builtin"] or 0) != 1:
        return False, "stored definition is not marked built-in"
    if row["adapter_api_version"] != manifest.adapter_api_version:
        return False, "stored adapter_api_version disagrees with registered manifest"
    if stored_json != expected_json:
        return False, "stored manifest disagrees with registered built-in manifest"
    return True, None


def _validate_adapter_config(
    adapter_id: str,
    adapter_version: str,
    config: Mapping | None,
) -> bool:
    """Whether this revision is valid to make current runnable authority.

    ``None`` may still be retained as a draft/historical empty config for
    backwards compatibility. If the adapter cannot construct from it, the
    revision is not activated. A supplied invalid config is rejected before any
    Source/Binding mutation.
    """
    from jobscraper.adapters.registry import BUILTIN_ADAPTERS, build_adapter

    adapter_cls = BUILTIN_ADAPTERS.get(adapter_id)
    if adapter_cls is None:
        return False
    if adapter_cls.manifest.version != adapter_version:
        raise ProvisioningError(
            f"registered adapter {adapter_id!r} is version "
            f"{adapter_cls.manifest.version!r}, not {adapter_version!r}"
        )
    try:
        build_adapter(adapter_id, config)
    except (ValueError, TypeError) as exc:
        if config is None:
            return False
        raise ProvisioningError(
            f"invalid config for built-in adapter {adapter_id!r}: {exc}"
        ) from exc
    return True


def _next_revision_number(
    conn: sqlite3.Connection,
    binding_id: str,
    adapter_id: str,
    adapter_version: str,
    strategy: str,
    execution_class: str,
    config_json: str = "{}",
) -> tuple[int, str | None]:
    existing = conn.execute(
        "SELECT id, revision FROM source_adapter_binding_revisions"
        " WHERE binding_id = ? AND adapter_id = ? AND adapter_version = ?"
        " AND strategy = ? AND execution_class = ? AND config_json = ?",
        (binding_id, adapter_id, adapter_version, strategy, execution_class, config_json),
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
    fingerprint,
    now: str | None = None,
    commit: bool = True,
) -> str:
    """Insert append-only fingerprint evidence.

    v14 retains nullable source ids for released-schema compatibility. The
    production provisioning path calls this only after Source creation.
    """
    _require_existing_source(conn, source_id)
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
    fingerprint,
    decision,
    now: str | None = None,
    commit: bool = True,
) -> str:
    """Insert append-only route-decision evidence with durable causal authority."""
    _require_existing_source(conn, source_id)
    _validate_fingerprint_decision(fingerprint, decision)
    # ``IS ?`` is the NULL-safe equality: a family-less (generic) fingerprint
    # stores family NULL, and ``family = NULL`` would match no row — the
    # honest GENERIC_DISCOVERY_FALLBACK decision would be unrecordable
    # (S2.8 acceptance finding F1).
    prior = conn.execute(
        "SELECT 1 FROM ats_fingerprints WHERE source_id = ? AND family IS ? AND confidence = ? LIMIT 1",
        (source_id, fingerprint.family, fingerprint.confidence),
    ).fetchone()
    if prior is None:
        raise ProvisioningError("route decision requires a matching persisted fingerprint first")
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
    fingerprint=None,
    decision=None,
    commit: bool = True,
) -> ProvisionedSource:
    """Idempotently provision one source target and binding revision."""
    ts = now or db_utc_now(conn)
    config_json = _canonical_json(config)
    # Reject impossible evidence pairs before any Source/Binding mutation.
    _validate_fingerprint_decision(fingerprint, decision)

    adapter_def = conn.execute(
        "SELECT adapter_id FROM adapter_definitions"
        " WHERE adapter_id = ? AND adapter_version = ?",
        (adapter_id, adapter_version),
    ).fetchone()
    if adapter_def is None:
        raise ProvisioningError(
            f"adapter_definitions row not found for "
            f"(adapter_id={adapter_id!r}, adapter_version={adapter_version!r})"
        )
    definition_ok, definition_reason = _registered_definition_state(
        conn, adapter_id, adapter_version
    )
    if not definition_ok:
        raise ProvisioningError(
            f"adapter definition {adapter_id!r}/{adapter_version!r} cannot be "
            f"activated: {definition_reason}"
        )

    # Both checks occur before any Source/Binding mutation.
    activatable = _validate_adapter_config(adapter_id, adapter_version, config)
    _source_target_key(entry_url)

    source_id = _provision_source(
        conn, display_name, source_family, entry_url, canonical_host, ts
    )
    binding_id = _provision_binding(conn, source_id, adapter_id, ts)

    rev_number, existing_rev_id = _next_revision_number(
        conn,
        binding_id,
        adapter_id,
        adapter_version,
        strategy,
        execution_class,
        config_json,
    )
    binding_revision_id = existing_rev_id or _create_binding_revision(
        conn,
        binding_id,
        rev_number,
        adapter_id,
        adapter_version,
        strategy,
        execution_class,
        ts,
        config_json=config_json,
    )

    if activatable:
        conn.execute(
            "UPDATE source_adapter_bindings SET current_revision_id = ?"
            " WHERE id = ? AND (current_revision_id IS NULL OR current_revision_id != ?)",
            (binding_revision_id, binding_id, binding_revision_id),
        )

    fingerprint_id = None
    if fingerprint is not None:
        fingerprint_id = record_fingerprint(
            conn,
            source_id=source_id,
            url=entry_url,
            fingerprint=fingerprint,
            now=ts,
            commit=False,
        )

    route_decision_id = None
    if decision is not None:
        route_decision_id = record_route_decision(
            conn,
            source_id=source_id,
            fingerprint=fingerprint,
            decision=decision,
            now=ts,
            commit=False,
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
    """Insert/reuse the same normalized target, never hostname-only identity."""
    target_key = _source_target_key(entry_url)
    for existing in conn.execute("SELECT id, entry_url FROM sources").fetchall():
        try:
            existing_key = _source_target_key(existing["entry_url"])
        except ProvisioningError:
            continue
        if existing_key == target_key:
            return existing["id"]
    source_id = new_id("src")
    conn.execute(
        "INSERT INTO sources"
        " (id, display_name, source_family, entry_url, canonical_host, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, display_name, source_family, entry_url, canonical_host, ts, ts),
    )
    return source_id


def _provision_binding(
    conn: sqlite3.Connection,
    source_id: str,
    display_name: str,
    ts: str,
) -> str:
    existing = conn.execute(
        "SELECT id FROM source_adapter_bindings"
        " WHERE source_id = ? AND display_name = ?",
        (source_id, display_name),
    ).fetchone()
    if existing:
        return existing["id"]
    binding_id = new_id("bnd")
    conn.execute(
        "INSERT INTO source_adapter_bindings (id, source_id, display_name, created_at)"
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
    perm_profile = conn.execute(
        "SELECT id FROM adapter_permission_profiles ORDER BY id LIMIT 1"
    ).fetchone()
    if perm_profile is None:
        raise ProvisioningError(
            "no adapter_permission_profiles row exists — cannot create a binding revision"
        )
    perm_profile_id = perm_profile["id"]
    perm_rev = conn.execute(
        "SELECT revision FROM adapter_permission_profile_revisions"
        " WHERE permission_profile_id = ? ORDER BY revision DESC LIMIT 1",
        (perm_profile_id,),
    ).fetchone()
    if perm_rev is None:
        raise ProvisioningError(f"no revision for permission profile {perm_profile_id!r}")
    revision_id = new_id("bndrev")
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions"
        " (id, binding_id, revision, adapter_id, adapter_version, strategy,"
        " priority, config_json, auth_requirement, auth_scope_id, execution_class,"
        " permission_profile_id, permission_profile_revision, created_at)"
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
            perm_rev["revision"],
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
    """Pin/verify a built-in identity without mutating conflicting history.

    A pre-existing conflicting row is returned with ``verified=False`` rather
    than silently treated as trustworthy. Provisioning refuses to activate such
    a definition. This preserves append-only history while making the conflict
    explicit and fail-closed at the authorization boundary.
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
            f"adapter {adapter_id!r} manifest declares version {manifest.version!r}, "
            f"not {adapter_version!r}"
        )
    ts = now or db_utc_now(conn)
    manifest_json = _canonical_json(dataclasses.asdict(manifest))

    existing = conn.execute(
        "SELECT adapter_version, adapter_api_version, manifest_json, is_builtin"
        " FROM adapter_definitions WHERE adapter_id = ? AND adapter_version = ?",
        (adapter_id, manifest.version),
    ).fetchone()
    if existing is not None:
        ok, reason = _registered_definition_state(conn, adapter_id, manifest.version)
        return BuiltinAdapterDefinition(
            adapter_id=adapter_id,
            adapter_version=existing["adapter_version"],
            adapter_api_version=existing["adapter_api_version"],
            created=False,
            verified=ok,
            conflict_reason=reason,
        )

    conn.execute(
        "INSERT INTO adapter_definitions (adapter_id, adapter_version,"
        " adapter_api_version, manifest_json, is_builtin, created_at)"
        " VALUES (?, ?, ?, ?, 1, ?)",
        (adapter_id, manifest.version, manifest.adapter_api_version, manifest_json, ts),
    )
    if commit:
        conn.commit()
    return BuiltinAdapterDefinition(
        adapter_id=adapter_id,
        adapter_version=manifest.version,
        adapter_api_version=manifest.adapter_api_version,
        created=True,
        verified=True,
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
