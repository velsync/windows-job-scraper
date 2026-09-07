"""Unit tests: source/binding domain lifecycle and profile revisions (§15-19)."""

import pytest

from jobscraper.domain.profiles import (
    create_profile,
    current_profile_snapshot,
    list_profiles,
    revise_profile,
)
from jobscraper.domain.sources import (
    create_binding,
    create_source,
    quarantine_binding,
    release_binding_quarantine,
    revise_binding,
    seed_builtin_permission_profile,
    viable_bindings,
)


@pytest.fixture()
def seeded(db):
    seed_builtin_permission_profile(db)
    return db


def _make_source(db, name="S"):
    return create_source(
        db, display_name=name, entry_url="https://boards.example/x",
        canonical_host="boards.example", source_family="greenhouse",
    )


def test_create_source_writes_immutable_revision(seeded):
    db = seeded
    src = _make_source(db)
    row = db.query_one("SELECT * FROM sources WHERE id=?", (src,))
    assert row["current_revision_id"]
    rev = db.query_one("SELECT * FROM source_revisions WHERE id=?", (row["current_revision_id"],))
    assert rev is not None
    assert rev["content_hash"]


def test_binding_revisions_are_immutable(seeded):
    db = seeded
    src = _make_source(db)
    bnd = create_binding(
        db, source_id=src, display_name="A", adapter_id="greenhouse", adapter_version="1.0.0",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", config={"board": "acme"},
    )
    before = db.query_one("SELECT * FROM source_adapter_bindings WHERE id=?", (bnd,))
    create_binding_rev2 = revise_binding(
        db, binding_id=bnd, config={"board": "acme", "base_url": "https://gh.example"},
    )
    after = db.query_one("SELECT * FROM source_adapter_bindings WHERE id=?", (bnd,))
    assert after["current_revision_id"] != before["current_revision_id"]
    old = db.query_one("SELECT * FROM source_adapter_binding_revisions WHERE id=?", (before["current_revision_id"],))
    assert old["superseded_at"] is not None
    revs = db.query(
        "SELECT revision FROM source_adapter_binding_revisions WHERE binding_id=? ORDER BY revision", (bnd,)
    )
    assert [r["revision"] for r in revs] == [1, 2]
    assert create_binding_rev2 == after["current_revision_id"]


def test_viable_bindings_respects_states(seeded):
    db = seeded
    src = _make_source(db)
    bnd = create_binding(
        db, source_id=src, display_name="A", adapter_id="greenhouse", adapter_version="1.0.0",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", config={"board": "acme"},
    )
    assert [b["id"] for b in viable_bindings(db, src)] == [bnd]

    quarantine_binding(db, bnd, "challenged")
    assert list(viable_bindings(db, src)) == []
    release_binding_quarantine(db, bnd)
    assert [b["id"] for b in viable_bindings(db, src)] == [bnd]

    db.execute("UPDATE source_adapter_bindings SET desired_state='DISABLED' WHERE id=?", (bnd,))
    assert list(viable_bindings(db, src)) == []


def test_viable_bindings_fallback_order(seeded):
    db = seeded
    src = _make_source(db)
    b1 = create_binding(
        db, source_id=src, display_name="primary", adapter_id="greenhouse", adapter_version="1.0.0",
        strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT", config={"board": "acme"},
        priority=5, fallback_group="g1", fallback_rank=1,
    )
    b2 = create_binding(
        db, source_id=src, display_name="fallback", adapter_id="generic", adapter_version="1.0.0",
        strategy="FALLBACK_PARSED_HTML", config={"recipe": {}},
        priority=5, fallback_group="g1", fallback_rank=2,
    )
    ids = [b["id"] for b in viable_bindings(db, src)]
    assert ids.index(b1) < ids.index(b2)


# ------------------------------------------------------------------ profiles
def test_profile_revision_immutable_and_snapshot(seeded):
    db = seeded
    pid = create_profile(db, name="P1", snapshot_overrides={"keywords": ["python"]})
    snap1 = current_profile_snapshot(db, pid)
    assert snap1["keywords"] == ["python"]
    rev1_id = snap1["_revision_id"]

    revise_profile(db, pid, overrides={"keywords": ["rust"]})
    snap2 = current_profile_snapshot(db, pid)
    assert snap2["keywords"] == ["rust"]
    assert snap2["_revision_id"] != rev1_id

    # Old revision rows survive untouched.
    old = db.query_one("SELECT * FROM profile_revisions WHERE id=?", (rev1_id,))
    assert old is not None
    assert old["profile_snapshot_json"]


def test_list_profiles(db):
    create_profile(db, name="A")
    create_profile(db, name="B")
    names = [p["name"] for p in list_profiles(db)]
    assert names == ["A", "B"]


def test_default_profile_snapshot_exists(seeded):
    from jobscraper.domain.profiles import DEFAULT_PROFILE_SNAPSHOT

    assert "keywords" in DEFAULT_PROFILE_SNAPSHOT
    assert "min_score_inbox" in DEFAULT_PROFILE_SNAPSHOT
