"""Source/binding/permission-profile domain management.

Authority: module 02 section 8 (Source/adapter/binding), section 9.1
(permission profiles), ACQ-01 (binding-owned auth/execution).

Editing adapter version, strategy, priority, auth, execution class,
permission profile or binding config creates a NEW immutable revision.
"""

from __future__ import annotations

import json
import secrets

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import utc_now_s

DEFAULT_PERMISSION_PROFILE_ID = "pp-default"
HTTP_READ_POLICY = {
    "network_hosts": ["*"],
    "authenticated_session_access": False,
    "browser_capability": [],
    "snapshot_read": True,
    "fixture_read": True,
    "egress_modes": ["DIRECT"],
    "filesystem_access": "DENIED",
    "subprocess_access": "DENIED",
}


def seed_builtin_permission_profile(db: Database) -> str:
    """Create/refresh the default read-only HTTP permission profile."""
    now = utc_now_s()
    with immediate_transaction(db.conn) as tx:
        row = tx.execute(
            "SELECT id FROM adapter_permission_profiles WHERE id=?", (DEFAULT_PERMISSION_PROFILE_ID,)
        ).fetchone()
        if row is None:
            rev_id = "ppr-" + secrets.token_hex(8)
            tx.execute(
                "INSERT INTO adapter_permission_profiles(id, display_name, administrative_state,"
                " current_revision_id, created_at) VALUES (?,?,?,?,?)",
                (DEFAULT_PERMISSION_PROFILE_ID, "Default read-only HTTP", "NORMAL", rev_id, now),
            )
            tx.execute(
                "INSERT INTO adapter_permission_profile_revisions(id, permission_profile_id,"
                " revision, policy_json, created_at) VALUES (?,?,?,?,?)",
                (rev_id, DEFAULT_PERMISSION_PROFILE_ID, 1, json.dumps(HTTP_READ_POLICY), now),
            )
    return DEFAULT_PERMISSION_PROFILE_ID


def get_permission_profile_snapshot(db: Database, profile_id: str) -> dict | None:
    row = db.query_one(
        "SELECT p.administrative_state, r.policy_json FROM adapter_permission_profiles p"
        " JOIN adapter_permission_profile_revisions r ON r.id = p.current_revision_id"
        " WHERE p.id=?",
        (profile_id,),
    )
    if row is None:
        return None
    return {
        "administrative_state": row["administrative_state"],
        "policy": json.loads(row["policy_json"]),
        "revision_id": row["policy_json"] and _current_revision_id(db, profile_id),
    }


def _current_revision_id(db: Database, profile_id: str) -> str | None:
    row = db.query_one(
        "SELECT current_revision_id FROM adapter_permission_profiles WHERE id=?", (profile_id,)
    )
    return row["current_revision_id"] if row else None


def create_source(
    db: Database,
    *,
    display_name: str,
    entry_url: str,
    canonical_host: str,
    source_family: str | None = None,
    source_access_policy: str = "PUBLIC",
    config: dict | None = None,
    cadence_seconds: int | None = None,
) -> str:
    from jobscraper.security.netpolicy import parse_url

    parse_url(entry_url)  # validate scheme/host shape
    source_id = "src-" + secrets.token_hex(8)
    now = utc_now_s()
    snapshot = {
        "display_name": display_name,
        "entry_url": entry_url,
        "canonical_host": canonical_host,
        "source_family": source_family,
        "config": config or {},
    }
    import hashlib

    content_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO sources(id, display_name, source_family, entry_url, canonical_host,"
            " source_access_policy, config_json, cadence_seconds, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, display_name, source_family, entry_url, canonical_host,
             source_access_policy, json.dumps(config or {}), cadence_seconds, now, now),
        )
        rev_id = "srev-" + secrets.token_hex(8)
        tx.execute(
            "INSERT INTO source_revisions(id, source_id, revision, snapshot_json, content_hash,"
            " created_at) VALUES (?,?,1,?,?,?)",
            (rev_id, source_id, json.dumps(snapshot, sort_keys=True), content_hash, now),
        )
        tx.execute("UPDATE sources SET current_revision_id=? WHERE id=?", (rev_id, source_id))
    return source_id


def create_binding(
    db: Database,
    *,
    source_id: str,
    display_name: str,
    adapter_id: str,
    adapter_version: str,
    strategy: str,
    execution_class: str = "HTTP",
    config: dict | None = None,
    priority: int = 100,
    fallback_group: str | None = None,
    fallback_rank: int = 1,
    permission_profile_id: str = DEFAULT_PERMISSION_PROFILE_ID,
    auth_requirement: str = "NONE",
    auth_scope_id: str | None = None,
    listing_identity_sufficient: bool = True,
) -> str:
    """Create a binding with its first immutable revision."""
    binding_id = "bnd-" + secrets.token_hex(8)
    now = utc_now_s()
    pp_revision = _current_revision_id(db, permission_profile_id) or "ppr-unknown"
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO source_adapter_bindings(id, source_id, display_name, desired_state,"
            " administrative_state, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (binding_id, source_id, display_name, "ENABLED", "NORMAL", now, now),
        )
        rev_id = "brv-" + secrets.token_hex(8)
        tx.execute(
            "INSERT INTO source_adapter_binding_revisions(id, binding_id, revision, adapter_id,"
            " adapter_version, strategy, priority, fallback_group, fallback_rank, config_json,"
            " auth_requirement, auth_scope_id, execution_class, permission_profile_id,"
            " permission_profile_revision, listing_identity_sufficient, created_at)"
            " VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rev_id, binding_id, adapter_id, adapter_version, strategy, priority,
             fallback_group or f"grp-{source_id[:12]}", fallback_rank,
             json.dumps(config or {}, sort_keys=True), auth_requirement, auth_scope_id,
             execution_class, permission_profile_id, pp_revision,
             1 if listing_identity_sufficient else 0, now),
        )
        tx.execute("UPDATE source_adapter_bindings SET current_revision_id=? WHERE id=?", (rev_id, binding_id))
    return binding_id


def revise_binding(
    db: Database,
    *,
    binding_id: str,
    config: dict | None = None,
    strategy: str | None = None,
    adapter_version: str | None = None,
    auth_requirement: str | None = None,
    execution_class: str | None = None,
) -> str:
    """Create a new immutable binding revision (promotion affects new plans only)."""
    now = utc_now_s()
    current = db.query_one(
        "SELECT b.source_id, r.* FROM source_adapter_bindings b"
        " JOIN source_adapter_binding_revisions r ON r.id = b.current_revision_id"
        " WHERE b.id=?",
        (binding_id,),
    )
    if current is None:
        raise KeyError(f"unknown binding {binding_id}")
    next_rev = int(db.query_one(
        "SELECT MAX(revision) m FROM source_adapter_binding_revisions WHERE binding_id=?", (binding_id,)
    )["m"] or 0) + 1
    rev_id = "brv-" + secrets.token_hex(8)
    new_config = json.dumps(config if config is not None else json.loads(current["config_json"]), sort_keys=True)
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "UPDATE source_adapter_binding_revisions SET superseded_at=? WHERE id=?",
            (now, current["id"]),
        )
        tx.execute(
            "INSERT INTO source_adapter_binding_revisions(id, binding_id, revision, adapter_id,"
            " adapter_version, strategy, priority, fallback_group, fallback_rank, config_json,"
            " auth_requirement, auth_scope_id, execution_class, permission_profile_id,"
            " permission_profile_revision, listing_identity_sufficient, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rev_id, binding_id, next_rev,
             current["adapter_id"], adapter_version or current["adapter_version"],
             strategy or current["strategy"], current["priority"], current["fallback_group"],
             current["fallback_rank"], new_config, auth_requirement or current["auth_requirement"],
             current["auth_scope_id"], execution_class or current["execution_class"],
             current["permission_profile_id"], current["permission_profile_revision"],
             current["listing_identity_sufficient"], now),
        )
        tx.execute("UPDATE source_adapter_bindings SET current_revision_id=?, updated_at=? WHERE id=?",
                   (rev_id, now, binding_id))
    return rev_id


def quarantine_binding(db: Database, binding_id: str, reason: str) -> None:
    now = utc_now_s()
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "UPDATE source_adapter_bindings SET administrative_state='QUARANTINED',"
            " quarantined_at=?, quarantine_reason=?, updated_at=? WHERE id=?",
            (now, reason, now, binding_id),
        )


def release_binding_quarantine(db: Database, binding_id: str) -> None:
    """QUARANTINED -> NORMAL only by explicit administrative release."""
    now = utc_now_s()
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "UPDATE source_adapter_bindings SET administrative_state='NORMAL', quarantined_at=NULL,"
            " quarantine_reason=NULL, updated_at=? WHERE id=?",
            (now, binding_id),
        )


def quarantine_source(db: Database, source_id: str, reason: str) -> None:
    now = utc_now_s()
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "UPDATE sources SET administrative_state='QUARANTINED', quarantined_at=?,"
            " quarantine_reason=?, updated_at=? WHERE id=?",
            (now, reason, now, source_id),
        )


def get_source(db: Database, source_id: str):
    return db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))


def list_sources(db: Database):
    return db.query("SELECT * FROM sources ORDER BY created_at")


def viable_bindings(db: Database, source_id: str):
    """Bindings eligible for execution: ENABLED + NORMAL (source and binding)."""
    return db.query(
        "SELECT b.id, b.display_name, b.desired_state, b.administrative_state,"
        " r.adapter_id, r.adapter_version, r.strategy, r.execution_class, r.priority,"
        " r.fallback_group, r.fallback_rank, r.config_json, r.auth_requirement, r.auth_scope_id,"
        " r.permission_profile_id, r.permission_profile_revision, r.listing_identity_sufficient,"
        " r.id AS binding_revision_id, r.revision AS binding_revision"
        " FROM source_adapter_bindings b"
        " JOIN source_adapter_binding_revisions r ON r.id = b.current_revision_id"
        " JOIN sources s ON s.id = b.source_id"
        " WHERE b.source_id=? AND b.desired_state='ENABLED' AND b.administrative_state='NORMAL'"
        " AND s.desired_state='ENABLED' AND s.administrative_state='NORMAL'"
        " AND b.retired_at IS NULL"
        " ORDER BY r.priority ASC, r.fallback_rank ASC",
        (source_id,),
    )
