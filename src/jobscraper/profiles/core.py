"""Profile creation and immutable revisions (01 §35, RUN-03)."""

from __future__ import annotations

import json
import sqlite3

from jobscraper.ids import new_id


def _hash(snapshot: dict) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, default=str).encode()
    ).hexdigest()


def create_profile(conn: sqlite3.Connection, *, snapshot: dict, now: str) -> tuple[str, str]:
    """Create a profile with its first immutable revision."""
    profile_id = new_id("prof")
    revision_id = new_id("profrev")
    conn.execute(
        "INSERT INTO search_profiles (id, name, is_default, current_revision_id,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (profile_id, str(snapshot.get("name") or "Profile"), 0, revision_id, now, now),
    )
    conn.execute(
        "INSERT INTO profile_revisions (id, profile_id, revision, profile_snapshot_json,"
        " content_hash, created_at) VALUES (?, ?, 1, ?, ?, ?)",
        (revision_id, profile_id, json.dumps(snapshot, sort_keys=True, default=str),
         _hash(snapshot), now),
    )
    conn.commit()
    return profile_id, revision_id


def edit_profile(conn: sqlite3.Connection, profile_id: str, *, snapshot: dict, now: str) -> str:
    """Append one immutable profile revision and materialize its evaluations.

    RUN-21 makes the profile pointer and all newly-current eligibility/score
    rows one local all-or-nothing operation. When called inside an existing
    transaction, ownership stays with the caller; otherwise this function owns
    a short BEGIN IMMEDIATE boundary.
    """
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT MAX(revision) FROM profile_revisions WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        next_revision = (row[0] or 0) + 1
        revision_id = new_id("profrev")
        conn.execute(
            "INSERT INTO profile_revisions (id, profile_id, revision, profile_snapshot_json,"
            " content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                profile_id,
                next_revision,
                json.dumps(snapshot, sort_keys=True, default=str),
                _hash(snapshot),
                now,
            ),
        )
        updated = conn.execute(
            "UPDATE search_profiles SET name = ?, current_revision_id = ?, updated_at = ?"
            " WHERE id = ?",
            (str(snapshot.get("name") or "Profile"), revision_id, now, profile_id),
        )
        if updated.rowcount != 1:
            raise KeyError(profile_id)

        # Lazy import avoids a module cycle: evaluation uses profile readers,
        # while profile mutation owns the revision-advance trigger.
        from jobscraper.pipeline.evaluation import materialize_profile_revision

        materialize_profile_revision(conn, profile_id, revision_id, now=now)
        if owns_transaction:
            conn.execute("COMMIT")
        return revision_id
    except BaseException:
        if owns_transaction and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def current_snapshot(conn: sqlite3.Connection, profile_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT r.profile_snapshot_json FROM search_profiles p
        JOIN profile_revisions r ON r.id = p.current_revision_id
        WHERE p.id = ?
        """,
        (profile_id,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["profile_snapshot_json"])


def list_profiles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM search_profiles ORDER BY created_at").fetchall()


__all__ = ["create_profile", "current_snapshot", "edit_profile", "list_profiles"]
