"""Search profiles and immutable profile/query revisions.

Authority: module 01 section 35 (profiles are data; edits create immutable
revisions); RUN-03 (query and profile reproducibility).
"""

from __future__ import annotations

import hashlib
import json
import secrets

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import utc_now_s

DEFAULT_PROFILE_SNAPSHOT = {
    "keywords": [],
    "negative_terms": [],
    "locations": [],
    "home_country": None,
    "eligible_countries": [],
    "acceptable_regions": [],
    "remote_rules": {"allow_remote": True, "remote_worldwide": False},
    "timezone_window": None,
    "salary_floor": None,
    "salary_currency": "USD",
    "seniority_allow": [],
    "employment_type_allow": [],
    "languages": [],
    "must_keywords": [],
    "should_keywords": [],
    "must_not_keywords": [],
    "weights": {
        "title_fit": 15,
        "keyword": 10,
        "eligible_location": 20,
        "salary_known": 8,
        "salary_above_floor": 8,
        "unknown_salary": -8,
        "seniority_above_preference": -12,
        "remote_fit": 6,
    },
    "min_score_inbox": 0.0,
    "schedule": None,
}


def create_profile(
    db: Database,
    *,
    name: str,
    timezone_name: str = "UTC",
    snapshot_overrides: dict | None = None,
    is_default: bool = False,
) -> str:
    profile_id = "prof-" + secrets.token_hex(8)
    now = utc_now_s()
    snapshot = {**DEFAULT_PROFILE_SNAPSHOT, **(snapshot_overrides or {})}
    content = json.dumps(snapshot, sort_keys=True)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO search_profiles(id, name, is_default, timezone, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (profile_id, name, 1 if is_default else 0, timezone_name, now, now),
        )
        rev_id = "prev-" + secrets.token_hex(8)
        tx.execute(
            "INSERT INTO profile_revisions(id, profile_id, revision, profile_snapshot_json,"
            " rules_revision_id, content_hash, created_at) VALUES (?,?,1,?,?,?,?)",
            (rev_id, profile_id, content, "rules-v1", content_hash, now),
        )
        tx.execute("UPDATE search_profiles SET current_revision_id=? WHERE id=?", (rev_id, profile_id))
    return profile_id


def revise_profile(db: Database, profile_id: str, *, overrides: dict | None = None,
                   timezone_name: str | None = None) -> str:
    """Edit a profile by creating a new immutable revision."""
    now = utc_now_s()
    profile = db.query_one("SELECT * FROM search_profiles WHERE id=?", (profile_id,))
    if profile is None:
        raise KeyError(profile_id)
    current = db.query_one(
        "SELECT * FROM profile_revisions WHERE id=?", (profile["current_revision_id"],)
    )
    snapshot = json.loads(current["profile_snapshot_json"])
    if overrides:
        snapshot.update(overrides)
    if timezone_name and timezone_name != profile["timezone"]:
        snapshot["timezone"] = timezone_name
    content = json.dumps(snapshot, sort_keys=True)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    next_rev = int(db.query_one(
        "SELECT MAX(revision) m FROM profile_revisions WHERE profile_id=?", (profile_id,)
    )["m"] or 0) + 1
    rev_id = "prev-" + secrets.token_hex(8)
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO profile_revisions(id, profile_id, revision, profile_snapshot_json,"
            " rules_revision_id, content_hash, created_at) VALUES (?,?,?,?,?,?,?)",
            (rev_id, profile_id, next_rev, content, "rules-v1", content_hash, now),
        )
        tx.execute(
            "UPDATE search_profiles SET current_revision_id=?, timezone=?, updated_at=? WHERE id=?",
            (rev_id, timezone_name or profile["timezone"], now, profile_id),
        )
    return rev_id


def current_profile_snapshot(db: Database, profile_id: str) -> dict | None:
    row = db.query_one(
        "SELECT r.profile_snapshot_json, r.id FROM search_profiles p"
        " JOIN profile_revisions r ON r.id = p.current_revision_id WHERE p.id=?",
        (profile_id,),
    )
    if row is None:
        return None
    data = json.loads(row["profile_snapshot_json"])
    data["_revision_id"] = row["id"]
    return data


def profile_snapshot_at(db: Database, revision_id: str) -> dict | None:
    row = db.query_one("SELECT profile_snapshot_json FROM profile_revisions WHERE id=?", (revision_id,))
    if row is None:
        return None
    data = json.loads(row["profile_snapshot_json"])
    data["_revision_id"] = revision_id
    return data


def list_profiles(db: Database):
    return db.query("SELECT * FROM search_profiles ORDER BY created_at")


def get_profile(db: Database, profile_id: str):
    return db.query_one("SELECT * FROM search_profiles WHERE id=?", (profile_id,))
