from __future__ import annotations

import sqlite3

import pytest
from dataclasses import dataclass

from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.acquisition.pagevalidity import PageClass, classify_page
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.acquisition.crawler.revalidation import (
    acquire_backup_hold,
    RevalidationCompatibilityError,
    prepare_revalidation,
    prune_representation,
    release_hold,
    request_variant_key,
    resolve_304,
    restore_membership,
    store_representation,
)


NOW = "2026-09-11T15:00:00.000000Z"


@dataclass(frozen=True)
class Obs:
    source_job_id: str | None
    raw_url: str | None = None


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE request_attempts(
            attempt_id TEXT PRIMARY KEY,
            outcome TEXT
        );
        CREATE TABLE cache_representation(
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            binding_revision_id TEXT NOT NULL,
            auth_scope_ref TEXT NOT NULL,
            request_variant_key TEXT NOT NULL,
            validated_page_class TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            normalized_content_hash TEXT NOT NULL,
            content_type TEXT,
            body_blob BLOB,
            parser_recipe_compatibility_key TEXT NOT NULL,
            membership_ref TEXT,
            membership_complete INTEGER NOT NULL DEFAULT 0,
            etag TEXT,
            last_modified TEXT,
            stored_at TEXT NOT NULL,
            last_verified_at TEXT NOT NULL,
            retention_policy TEXT NOT NULL DEFAULT 'BOUNDED_LOCAL',
            expires_at TEXT,
            superseded_at TEXT,
            pruned_at TEXT
        );
        CREATE UNIQUE INDEX uq_cache_rep_exact ON cache_representation(
            source_id,binding_revision_id,auth_scope_ref,request_variant_key,
            validated_page_class,normalized_content_hash,
            parser_recipe_compatibility_key
        );
        CREATE TABLE cache_representation_membership(
            representation_id TEXT NOT NULL,
            stable_source_identity TEXT NOT NULL,
            source_identity_generation INTEGER NOT NULL DEFAULT 1,
            evidence_ref TEXT,
            PRIMARY KEY(representation_id,stable_source_identity,source_identity_generation)
        );
        CREATE TABLE cache_representation_hold(
            id TEXT PRIMARY KEY,
            representation_id TEXT NOT NULL,
            owner_kind TEXT NOT NULL,
            owner_ref TEXT NOT NULL,
            owner_attempt_id TEXT,
            created_at TEXT NOT NULL,
            released_at TEXT
        );
        CREATE UNIQUE INDEX uq_cache_hold_active
            ON cache_representation_hold(representation_id,owner_kind,owner_ref)
            WHERE released_at IS NULL;
        CREATE TABLE enumeration_coverage(
            id TEXT PRIMARY KEY,
            run_source_plan_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            binding_revision_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            coverage_authority TEXT NOT NULL,
            finalized_at TEXT,
            started_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            generation_order_key TEXT NOT NULL
        );
        CREATE TABLE coverage_seen_identity(
            coverage_id TEXT NOT NULL,
            stable_source_identity TEXT NOT NULL,
            source_identity_generation INTEGER NOT NULL DEFAULT 1,
            observation_or_listing_evidence_ref TEXT,
            PRIMARY KEY(coverage_id,stable_source_identity,source_identity_generation)
        );
        CREATE TABLE source_presence_scope_membership(
            job_source_id TEXT NOT NULL,
            binding_revision_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            first_seen_coverage_id TEXT NOT NULL,
            last_seen_coverage_id TEXT NOT NULL,
            last_seen_order_key TEXT NOT NULL,
            last_absence_coverage_id TEXT,
            last_absence_order_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(job_source_id,binding_revision_id,scope_key)
        );
        CREATE TABLE sources(
            id TEXT PRIMARY KEY,
            source_family TEXT NOT NULL
        );
        CREATE TABLE job_observations(
            id TEXT PRIMARY KEY,
            strategy TEXT
        );
        CREATE TABLE jobs(
            id TEXT PRIMARY KEY,
            listing_status TEXT NOT NULL DEFAULT 'ACTIVE',
            last_changed_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE job_sources(
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_job_id TEXT,
            source_identity_generation INTEGER NOT NULL DEFAULT 1,
            last_seen_at TEXT,
            last_verified_at TEXT,
            last_changed_at TEXT,
            presence_state TEXT NOT NULL DEFAULT 'ACTIVE',
            last_absence_coverage_id TEXT,
            last_authoritative_scope_key TEXT,
            last_observation_id TEXT,
            availability_effective_at TEXT NOT NULL DEFAULT '',
            availability_received_at TEXT NOT NULL DEFAULT '',
            availability_evidence_kind TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN',
            availability_evidence_ref TEXT,
            availability_revision INTEGER NOT NULL DEFAULT 0,
            content_revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    return conn


def _plan(**changes):
    value = {
        "id": "rsp1",
        "source_id": "src",
        "binding_id": "bnd",
        "binding_revision_id": "rev1",
        "adapter_id": "fixture",
        "adapter_version": "1",
        "recipe_version_id": None,
        "cursor_schema_version": 1,
        "auth_scope_id": None,
    }
    value.update(changes)
    return value


def _request(headers=None):
    return RequestPlan(
        method="GET",
        url="https://jobs.example.test/list?utm_source=x",
        headers=headers or {"Accept": "application/json"},
        expected_content_types=("application/json",),
        purpose="LIST_FETCH",
    )


def test_request_variant_ignores_tracking_and_conditional_validator_headers():
    base = _request()
    conditional = _request(
        {
            "Accept": "application/json",
            "If-None-Match": '"abc"',
            "If-Modified-Since": "Wed, 09 Sep 2026 10:00:00 GMT",
        }
    )
    assert request_variant_key(base) == request_variant_key(conditional)


def test_compatible_representation_plans_host_owned_conditional_headers():
    conn = _conn()
    conn.execute("INSERT INTO request_attempts VALUES ('att1',NULL)")
    rep = store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b'{"jobs":[{"id":"A"}]}',
        body_hash="a" * 64,
        normalized_content_hash="b" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1"', "Last-Modified": "Wed, 09 Sep 2026 10:00:00 GMT"},
        observations=(Obs("A", "https://jobs.example.test/A"),),
        membership_complete=True,
        normalization_version=1,
        now=NOW,
    )
    prepared = prepare_revalidation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        attempt_id="att1",
        expected_page_classes=("VALID_LIST", "EMPTY"),
        require_membership=True,
        normalization_version=1,
        now=NOW,
    )
    assert prepared.conditional is True
    assert prepared.representation_id == rep
    assert prepared.request_plan.headers["If-None-Match"] == '"v1"'
    assert prepared.request_plan.revalidation_headers_allowed is True

    reuse = resolve_304(
        conn,
        preparation=prepared,
        plan_row=_plan(),
        request_plan=prepared.request_plan,
        expected_page_classes=("VALID_LIST", "EMPTY"),
        require_membership=True,
        normalization_version=1,
    )
    assert reuse.accepted is True
    assert reuse.body == b'{"jobs":[{"id":"A"}]}'
    assert [m.stable_source_identity for m in reuse.membership] == ["A"]


def test_binding_parser_and_auth_mismatch_never_reuses():
    conn = _conn()
    conn.execute("INSERT INTO request_attempts VALUES ('att1',NULL)")
    store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b"[]",
        body_hash="a" * 64,
        normalized_content_hash="a" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1"'},
        observations=(),
        membership_complete=True,
        normalization_version=1,
        now=NOW,
    )
    for changed in (
        _plan(binding_revision_id="rev2"),
        _plan(adapter_version="2"),
        _plan(auth_scope_id="auth2"),
    ):
        prep = prepare_revalidation(
            conn,
            plan_row=changed,
            request_plan=_request(),
            attempt_id="att1",
            expected_page_classes=("VALID_LIST", "EMPTY"),
            require_membership=True,
            normalization_version=1,
            now=NOW,
        )
        assert prep.conditional is False


def test_prune_is_blocked_while_revalidation_hold_is_live_then_allowed_after_release():
    conn = _conn()
    conn.execute("INSERT INTO request_attempts VALUES ('att1',NULL)")
    rep = store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b"[]",
        body_hash="a" * 64,
        normalized_content_hash="a" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1"'},
        observations=(),
        membership_complete=True,
        normalization_version=1,
        now=NOW,
    )
    prep = prepare_revalidation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        attempt_id="att1",
        expected_page_classes=("VALID_LIST",),
        require_membership=True,
        normalization_version=1,
        now=NOW,
    )
    assert prune_representation(conn, rep, now=NOW) is False
    release_hold(conn, prep.hold_id, now=NOW)
    assert prune_representation(conn, rep, now=NOW) is True
    row = conn.execute("SELECT body_blob,membership_complete FROM cache_representation WHERE id=?", (rep,)).fetchone()
    assert row["body_blob"] is None
    assert row["membership_complete"] == 0


def test_same_normalized_200_reuses_representation_changed_normalized_hash_creates_revision():
    conn = _conn()
    first = store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b"one",
        body_hash="1" * 64,
        normalized_content_hash="a" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1"'},
        observations=(Obs("A"),),
        membership_complete=True,
        normalization_version=1,
        now="2026-09-11T15:00:00Z",
    )
    same = store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b"one with harmless formatting",
        body_hash="9" * 64,
        normalized_content_hash="a" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1b"'},
        observations=(Obs("A"),),
        membership_complete=True,
        normalization_version=1,
        now="2026-09-11T15:01:00Z",
    )
    changed = store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b"two",
        body_hash="2" * 64,
        normalized_content_hash="b" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v2"'},
        observations=(Obs("A"), Obs("B")),
        membership_complete=True,
        normalization_version=1,
        now="2026-09-11T15:02:00Z",
    )
    assert same == first
    assert changed != first
    assert conn.execute("SELECT COUNT(*) FROM cache_representation").fetchone()[0] == 2
    old = conn.execute("SELECT superseded_at FROM cache_representation WHERE id=?", (first,)).fetchone()
    assert old["superseded_at"] is not None


def test_304_membership_advances_verification_without_content_revision():
    conn = _conn()
    rep = store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b'{"jobs":[{"id":"A"}]}',
        body_hash="a" * 64,
        normalized_content_hash="b" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1"'},
        observations=(Obs("A", "https://jobs.example.test/A"),),
        membership_complete=True,
        normalization_version=1,
        now="2026-09-11T15:00:00Z",
    )
    conn.execute(
        """
        INSERT INTO enumeration_coverage(
            id,run_source_plan_id,source_id,binding_id,binding_revision_id,
            scope_key,coverage_authority,finalized_at,started_at,created_at,
            generation_order_key)
        VALUES ('cov','rsp1','src','bnd','rev1','full-source',
                'AUTHORITATIVE_FULL_SOURCE',NULL,?,?,?)
        """,
        (NOW, NOW, f"{NOW}|cov"),
    )
    conn.execute("INSERT INTO sources VALUES ('src','ATS_PROVIDER_API')")
    conn.execute(
        "INSERT INTO jobs(id,listing_status,updated_at) VALUES ('job-js','ACTIVE',?)",
        ("2026-09-10T00:00:00Z",),
    )
    conn.execute(
        """
        INSERT INTO job_sources(
            id,job_id,source_id,source_job_id,source_identity_generation,
            last_seen_at,last_verified_at,presence_state,
            availability_effective_at,availability_received_at,
            availability_evidence_kind,content_revision,created_at,updated_at)
        VALUES ('js','job-js','src','A',1,?,?, 'ACTIVE',?,?, 'LEGACY_ACTIVE',7,?,?)
        """,
        (
            "2026-09-10T00:00:00Z",
            "2026-09-10T00:00:00Z",
            "2026-09-10T00:00:00Z",
            "2026-09-10T00:00:00Z",
            "2026-09-10T00:00:00Z",
            "2026-09-10T00:00:00Z",
        ),
    )
    restore_membership(
        conn,
        representation_id=rep,
        coverage_id="cov",
        plan_row=_plan(),
        now="2026-09-11T16:00:00Z",
    )
    row = conn.execute(
        "SELECT last_verified_at,content_revision FROM job_sources WHERE id='js'"
    ).fetchone()
    assert row["last_verified_at"] == "2026-09-11T16:00:00Z"
    assert row["content_revision"] == 7
    seen = conn.execute(
        "SELECT stable_source_identity FROM coverage_seen_identity WHERE coverage_id='cov'"
    ).fetchall()
    assert [r[0] for r in seen] == ["A"]


def test_bare_304_is_unknown_never_empty_before_cache_resolution():
    result = ResultEnvelope(
        execution_plan_id="plan",
        request_id="req",
        attempt_id="att",
        run_source_plan_id="rsp1",
        source_id="src",
        binding_id="bnd",
        binding_revision_id="rev1",
        adapter_id="fixture",
        adapter_version="1",
        strategy="HTTP_HTML",
        execution_class="HTTP",
        requested_url="https://jobs.example.test/list",
        final_url="https://jobs.example.test/list",
        status_code=304,
        was_304=True,
    )
    classified = classify_page(result, expect="LIST")
    assert classified.state is PageClass.UNKNOWN
    assert classified.evidence["revalidation_required"] is True


def test_adapter_supplied_conditional_validator_is_refused_even_on_forced_unconditional():
    conn = _conn()
    conn.execute("INSERT INTO request_attempts VALUES ('att1',NULL)")
    malicious = _request({"Accept": "application/json", "If-None-Match": '"adapter-owned"'})
    with pytest.raises(RevalidationCompatibilityError, match="host-owned"):
        prepare_revalidation(
            conn,
            plan_row=_plan(),
            request_plan=malicious,
            attempt_id="att1",
            expected_page_classes=("VALID_LIST",),
            require_membership=True,
            normalization_version=1,
            now=NOW,
            force_unconditional=True,
        )


def test_pruned_representation_is_not_selected_for_conditional_revalidation():
    conn = _conn()
    conn.execute("INSERT INTO request_attempts VALUES ('att1',NULL)")
    rep = store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b"[]",
        body_hash="a" * 64,
        normalized_content_hash="a" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1"'},
        observations=(),
        membership_complete=True,
        normalization_version=1,
        now=NOW,
    )
    assert prune_representation(conn, rep, now=NOW) is True
    prep = prepare_revalidation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        attempt_id="att1",
        expected_page_classes=("VALID_LIST",),
        require_membership=True,
        normalization_version=1,
        now=NOW,
    )
    assert prep.conditional is False


def test_backup_hold_blocks_pruning_until_explicit_release():
    conn = _conn()
    rep = store_representation(
        conn,
        plan_row=_plan(),
        request_plan=_request(),
        validated_page_class="VALID_LIST",
        body=b"[]",
        body_hash="a" * 64,
        normalized_content_hash="a" * 64,
        content_type="application/json",
        response_headers={"ETag": '"v1"'},
        observations=(),
        membership_complete=True,
        normalization_version=1,
        now=NOW,
    )
    hold = acquire_backup_hold(
        conn, rep, backup_ref="backup://fixture", now=NOW
    )
    assert prune_representation(conn, rep, now=NOW) is False
    release_hold(conn, hold, now=NOW)
    assert prune_representation(conn, rep, now=NOW) is True
