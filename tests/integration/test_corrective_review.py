"""Corrective-review regression tests.

Each test pins a bug found in the corrective review of the service/session
work: multi-source run isolation, evidence-time projection ordering (RUN-21),
extractor per-card required fields, no invented job URLs, inbox NULL-score
consistency, aggregator-vs-employer trust, and fingerprint/router units for
previously untested modules.
"""

import json
import secrets

import pytest

from jobscraper.acquisition import fingerprint as fp_mod
from jobscraper.acquisition import router as router_mod
from jobscraper.acquisition.contracts import ExecutionClass, Strategy
from jobscraper.acquisition.registry import AdapterRegistry
from jobscraper.config import AppConfig
from jobscraper.db.connection import immediate_transaction
from jobscraper.domain.profiles import create_profile
from jobscraper.domain.sources import (
    create_binding,
    create_source,
    seed_builtin_permission_profile,
)
from jobscraper.recipes.extractor import extract_with_telemetry
from jobscraper.recipes.models import ExtractionRecipe
from jobscraper.runtime.engine import RunEngine
from jobscraper.runtime.processing import ProcessingPipeline
from jobscraper.security.netpolicy import make_test_fixture_policy
from jobscraper.timeutil import utc_now_s
from jobscraper.workflow.inbox import emit_inbox_event, inbox_queue
from tests.fixtures.fixture_server import FixtureServer

STRATEGY = "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT"


def _board(job_id, title, extra=""):
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://job-boards.example/x/jobs/{job_id}",
        "updated_at": "2026-08-01T00:00:00Z",
        "first_published": "2026-07-15T00:00:00Z",
        "content": f"<p>{title}. python fastapi. Berlin, Germany.{extra}</p>",
        "location": {"name": "Berlin, Germany"},
        "offices": [], "departments": [], "metadata": [],
    }


# ------------------------------------------------- multi-source run isolation
def test_two_sources_both_collected_in_one_run(db, data_root):
    """Regression: bindings without an explicit fallback_group must not share
    a group (second source was SKIPPED), and one plan's drain loop must never
    claim another plan's requests (wrong pinned adapter config)."""
    with FixtureServer() as server:
        server.add_json("/v1/boards/acme/jobs", {"jobs": [_board(9001, "Acme Alpha Engineer")], "meta": {}})
        server.add_json("/v1/boards/beta/jobs", {"jobs": [_board(9002, "Beta Omega Engineer")], "meta": {}})
        seed_builtin_permission_profile(db)
        src_a = create_source(db, display_name="Acme", entry_url=server.base_url + "/v1/boards/acme/jobs",
                              canonical_host="127.0.0.1", source_family="greenhouse")
        src_b = create_source(db, display_name="Beta", entry_url=server.base_url + "/v1/boards/beta/jobs",
                              canonical_host="127.0.0.1", source_family="greenhouse")
        # Default bindings: NO explicit fallback_group (the regression case).
        create_binding(db, source_id=src_a, display_name="A", adapter_id="greenhouse",
                       adapter_version="1.0.0", strategy=STRATEGY,
                       config={"board": "acme", "base_url": server.base_url})
        create_binding(db, source_id=src_b, display_name="B", adapter_id="greenhouse",
                       adapter_version="1.0.0", strategy=STRATEGY,
                       config={"board": "beta", "base_url": server.base_url})
        profile = create_profile(db, name="P", snapshot_overrides={"keywords": ["python"], "min_score_inbox": 0})
        engine = RunEngine(AppConfig(data_root=data_root), db, AdapterRegistry(),
                           policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"))
        report = engine.execute_run(engine.create_run(profile_id=profile, source_ids=[src_a, src_b]))

        # Both groups satisfied; both sources produced their own jobs.
        assert report.status == "SUCCEEDED", report
        assert sorted(report.groups.values()) == ["SATISFIED", "SATISFIED"]
        titles = {j["title"] for j in db.query("SELECT title FROM jobs")}
        assert titles == {"Acme Alpha Engineer", "Beta Omega Engineer"}
        # Each plan executed exactly its own request.
        plans = {p["id"]: p for p in db.query("SELECT * FROM run_source_plans")}
        assert len(plans) == 2
        for plan_id, plan in plans.items():
            reqs = db.query("SELECT * FROM scrape_requests WHERE run_source_plan_id=?", (plan_id,))
            assert len(reqs) == 1
            assert reqs[0]["source_id"] == plan["source_id"]
        # Two coverages (one per plan), both COMPLETE.
        covs = db.query("SELECT * FROM enumeration_coverage")
        assert len(covs) == 2
        assert all(c["completion_state"] == "COMPLETE" for c in covs)


def test_failing_source_does_not_block_healthy_source(db, data_root):
    with FixtureServer() as server:
        server.add_json("/v1/boards/good/jobs", {"jobs": [_board(9101, "Good Corp Engineer")], "meta": {}})
        server.add_html("/v1/boards/dead/jobs",
                        "<html><title>Access Denied</title><body>Verify you are human</body></html>", status=403)
        seed_builtin_permission_profile(db)
        src_good = create_source(db, display_name="Good", entry_url=server.base_url + "/v1/boards/good/jobs",
                                 canonical_host="127.0.0.1", source_family="greenhouse")
        src_dead = create_source(db, display_name="Dead", entry_url=server.base_url + "/v1/boards/dead/jobs",
                                 canonical_host="127.0.0.1", source_family="greenhouse")
        for src, board in ((src_good, "good"), (src_dead, "dead")):
            create_binding(db, source_id=src, display_name=board, adapter_id="greenhouse",
                           adapter_version="1.0.0", strategy=STRATEGY,
                           config={"board": board, "base_url": server.base_url})
        profile = create_profile(db, name="P", snapshot_overrides={"keywords": ["python"], "min_score_inbox": 0})
        engine = RunEngine(AppConfig(data_root=data_root), db, AdapterRegistry(),
                           policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"))
        report = engine.execute_run(engine.create_run(profile_id=profile, source_ids=[src_good, src_dead]))
        # Mixed outcome: PARTIAL run, healthy source collected, dead source FAILED.
        assert report.status == "PARTIAL", report
        assert sorted(report.groups.values()) == ["FAILED", "SATISFIED"]
        titles = {j["title"] for j in db.query("SELECT title FROM jobs")}
        assert titles == {"Good Corp Engineer"}


# ---------------------------------------------- projection evidence ordering
def _mk_observation(db, *, source_id, observed_at, title, source_job_id, content_extra=""):
    """Insert a minimal run/plan/request/observation by hand (controlled times)."""
    now = utc_now_s()
    suffix = secrets.token_hex(4)
    with immediate_transaction(db.conn) as tx:
        tx.execute(
            "INSERT INTO scrape_runs(id, run_kind, status, collection_status, created_at)"
            " VALUES (?,?, 'SUCCEEDED','COMPLETED',?)",
            (f"run-{suffix}", "COLLECT", now),
        )
        tx.execute(
            "INSERT INTO run_source_plans(id, run_id, source_id, source_revision_id,"
            " source_config_snapshot_ref, source_plan_group_id, fallback_rank, binding_id,"
            " binding_revision_id, binding_revision, binding_config_snapshot_json, adapter_id,"
            " adapter_version, adapter_api_version, strategy, execution_class,"
            " crawl_policy_snapshot_json, permission_profile_id, permission_profile_revision,"
            " run_config_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"rsp-{suffix}", f"run-{suffix}", source_id, "srev-x", "{}", f"grp-{source_id}", 1,
             "bnd-x", "brv-x", 1, "{}", "greenhouse", "1.0.0", "1", STRATEGY, "HTTP",
             "{}", "pp-x", 1, "hash-x", now),
        )
        tx.execute(
            "INSERT INTO scrape_requests(id, run_id, run_source_plan_id, source_id, binding_id,"
            " request_type, request_unique_key, payload_json, strategy, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?, 'LIST_FETCH', ?, ?, ?, 'SUCCEEDED', ?, ?)",
            (f"req-{suffix}", f"run-{suffix}", f"rsp-{suffix}", source_id, "bnd-x",
             f"uk-{suffix}", "{}", STRATEGY, now, now),
        )
        obs_id = f"obs-{suffix}"
        tx.execute(
            "INSERT INTO job_observations(id, run_id, request_id, attempt_id, source_id, binding_id,"
            " adapter_id, adapter_version, strategy, execution_class, source_job_id, raw_url,"
            " canonical_url_candidate, observed_at, observation_unique_key, content_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obs_id, f"run-{suffix}", f"req-{suffix}", f"att-{suffix}", source_id, "bnd-x",
             "greenhouse", "1.0.0", STRATEGY, "HTTP", source_job_id,
             f"https://x.example/jobs/{source_job_id}", f"https://x.example/jobs/{source_job_id}",
             observed_at, f"ouk-{suffix}",
             json.dumps({"title": title, "company": "Orderly Corp",
                         "description_html": f"<p>{title}. python. Berlin.{content_extra}</p>",
                         "location_raw": "Berlin, Germany"})),
        )
    return {"observation_id": obs_id, "claim_token": "unused"}


def test_older_observation_late_cannot_regress_newer_projection(db, data_root):
    """RUN-21 at the canonical projection: evidence-time ordering, not
    content-hash ordering (regression: hash-int comparison randomly blocked
    legitimate updates / allowed regressions)."""
    seed_builtin_permission_profile(db)
    src = create_source(db, display_name="S", entry_url="https://boards.example/x",
                        canonical_host="boards.example", source_family="greenhouse")
    pipeline = ProcessingPipeline(AppConfig(data_root=data_root), db)

    # Newer observation first (10:00, new content).
    pipeline._process_one(
        _mk_observation(db, source_id=src, observed_at="2026-09-05T10:00:00Z",
                        title="New Title", source_job_id="ord-1"), run_id=None)
    job = db.query_one("SELECT * FROM jobs")
    assert job["title"] == "New Title"
    assert job["projection_evidence_at"] == "2026-09-05T10:00:00Z"

    # Older observation (09:00, DIFFERENT content) completes later: it must
    # NOT regress the projection regardless of hash ordering.
    pipeline._process_one(
        _mk_observation(db, source_id=src, observed_at="2026-09-05T09:00:00Z",
                        title="Old Title", source_job_id="ord-1"), run_id=None)
    job = db.query_one("SELECT * FROM jobs")
    assert job["title"] == "New Title"
    assert job["projection_evidence_at"] == "2026-09-05T10:00:00Z"
    # One canonical job only (same source-native identity).
    assert db.query_one("SELECT COUNT(*) c FROM jobs")["c"] == 1

    # A genuinely newer observation with different content DOES update.
    pipeline._process_one(
        _mk_observation(db, source_id=src, observed_at="2026-09-06T12:00:00Z",
                        title="Updated Title", source_job_id="ord-1",
                        content_extra=" Now with fastapi."), run_id=None)
    job = db.query_one("SELECT * FROM jobs")
    assert job["title"] == "Updated Title"
    assert job["projection_evidence_at"] == "2026-09-06T12:00:00Z"
    assert job["last_changed_at"] is not None


# ------------------------------------------------------- extractor fixes
RECIPE_TWO_CARDS = ExtractionRecipe.from_json({
    "schema_version": 1,
    "mode": "HTML",
    "card_locators": [{"kind": "data_attr", "value": "data-testid=job-card"}],
    "fields": {
        "title": {"required": True, "locators": [{"kind": "relative_css", "value": "h3"}]},
    },
})

TWO_CARDS_HTML = """<html><body>
<div data-testid="job-card"><h3>Good Role</h3></div>
<div data-testid="job-card"><p>no heading</p></div>
</body></html>"""


def test_extractor_bad_card_does_not_drop_good_card():
    """Regression: missing_required accumulated across cards, and the key
    membership check dropped the GOOD card because the BAD card missed a
    field the good card had."""
    result = extract_with_telemetry(RECIPE_TWO_CARDS, TWO_CARDS_HTML, "https://x.example/jobs")
    assert [r["title"] for r in result.records] == ["Good Role"]
    assert len(result.missing_required) == 1
    assert result.missing_required[0]["field"] == "title"


def test_extractor_never_invents_job_url():
    """Regression: records without a job_url locator got a junk `base#` URL,
    which would feed stage-3 canonical-URL dedup and merge unrelated jobs."""
    html = '<html><body><div data-testid="job-card"><h3>Role A</h3></div></body></html>'
    result = extract_with_telemetry(RECIPE_TWO_CARDS, html, "https://x.example/jobs")
    assert len(result.records) == 1
    assert "job_url" not in result.records[0]  # absent, not invented


# -------------------------------------------------- inbox queue consistency
def test_inbox_queue_excludes_null_score_jobs(db):
    """Regression: COALESCE(sc.score, 0) >= 0 admitted NULL-score jobs while
    the canonical inbox_eligible predicate requires a score."""
    from tests.unit.test_entity_presence_inbox import _mk_job, _mk_profile

    job_with = _mk_job(db, title="Scored Job")
    job_without = _mk_job(db, title="Unscored Job")
    profile_id = _mk_profile(db)
    for job_id in (job_with, job_without):
        emit_inbox_event(db, job_id=job_id, profile_id=profile_id,
                         event_kind="NEW_ELIGIBLE_APPEARANCE",
                         dedupe_key=f"first-eligible:{job_id}:{profile_id}")
    db.execute(
        "INSERT INTO job_scores(job_id, profile_id, rules_revision_id, job_content_revision,"
        " normalization_version, scorer_version, score, breakdown_json, rule_version, scored_at)"
        " VALUES (?,?, 'rules-v1',1,'normalize-v1','scorer-v1', 30.0, '[]', 'scoring-rules-v1', ?)",
        (job_with, profile_id, utc_now_s()),
    )
    queued = [row["id"] for row in inbox_queue(db, profile_id)]
    assert queued == [job_with]


# ------------------------------------------------ presence trust tiers
def test_aggregator_closed_cannot_close_employer_active(db):
    """Regression: _is_trusted ranked everything <= 100 as trusted, so an
    aggregator CLOSED closed jobs the employer still listed as ACTIVE."""
    from tests.unit.test_entity_presence_inbox import _mk_job, _mk_source
    from jobscraper.runtime.presence import derive_listing_status, upsert_presence

    job_id = _mk_job(db)
    agg = _mk_source(db, name="Aggregator")
    employer = _mk_source(db, name="Employer")
    upsert_presence(db, job_id=job_id, source_id=agg, binding_id=None, source_job_id="a1",
                    observed_at="2026-09-01T00:00:00Z", source_rank=50)
    upsert_presence(db, job_id=job_id, source_id=employer, binding_id=None, source_job_id="e1",
                    observed_at="2026-09-01T00:00:00Z", source_rank=10)
    db.execute("UPDATE job_sources SET presence_state='CLOSED' WHERE source_id=? AND job_id=?", (agg, job_id))
    # Aggregator says closed, employer says active: the job stays ACTIVE.
    assert derive_listing_status(db, job_id) == "ACTIVE"
    # A trusted employer CLOSED still wins over aggregator ACTIVE.
    db.execute("UPDATE job_sources SET presence_state='ACTIVE' WHERE source_id=? AND job_id=?", (agg, job_id))
    db.execute("UPDATE job_sources SET presence_state='CLOSED' WHERE source_id=? AND job_id=?", (employer, job_id))
    assert derive_listing_status(db, job_id) == "CLOSED"


# ------------------------------------------- fingerprint/router units (new)
class TestFingerprint:
    def test_greenhouse_board_detected(self):
        html = ("<html><head><title>Acme — Careers</title></head><body>"
                "<a href='https://job-boards.greenhouse.io/acme'>jobs</a></body></html>")
        fp = fp_mod.fingerprint_html(html, "https://acme.example/careers")
        assert fp.family == "greenhouse"
        assert fp.is_confident
        assert fp.recommended_adapter_id == "greenhouse"
        assert fp.board_token == "acme"
        assert fp.title == "Acme — Careers"

    def test_ashby_detected(self):
        html = "<html><body>Powered by <a href='https://jobs.ashbyhq.com/acme'>Ashby</a></body></html>"
        fp = fp_mod.fingerprint_html(html, "https://acme.example")
        assert fp.family == "ashby" and fp.is_confident

    def test_generic_page_is_low_confidence(self):
        html = "<html><head><title>Careers</title></head><body>Our open roles…</body></html>"
        fp = fp_mod.fingerprint_html(html, "https://acme.example/careers")
        assert not fp.is_confident  # falls back to generic discovery, never forced

    def test_unconfident_family_never_forces_specialized_adapter(self):
        # Families without a specialized adapter (workable et al.) score below
        # the 0.7 confidence bar; recommendation must stay the generic adapter.
        html = "<html><body><a href='https://careers.workable.com/acme'>jobs</a></body></html>"
        fp = fp_mod.fingerprint_html(html, "https://acme.example")
        assert fp.family == "workable"
        assert not fp.is_confident
        assert fp.recommended_adapter_id == "generic"


class TestRouter:
    def test_confident_family_routes_to_feed(self):
        routed = router_mod.route_for_fingerprint(
            family="greenhouse", confident=True, adapter_for_family="greenhouse")
        assert routed.strategy == Strategy.FEED_OR_PUBLIC_STRUCTURED_ENDPOINT
        assert routed.execution_class == ExecutionClass.HTTP

    def test_low_confidence_routes_to_generic_http(self):
        routed = router_mod.route_for_fingerprint(family=None, confident=False)
        assert routed.adapter_id == "generic"
        assert routed.strategy == Strategy.HTTP_HTML
        assert routed.execution_class == ExecutionClass.HTTP

    def test_needs_browser_routes_to_playwright(self):
        routed = router_mod.route_for_fingerprint(family=None, confident=False, needs_browser=True)
        assert routed.execution_class == ExecutionClass.BROWSER
        assert routed.strategy == Strategy.PLAYWRIGHT_PUBLIC

    def test_fallback_ladder_adds_browser_rung(self):
        routed = router_mod.route_for_fingerprint(family="greenhouse", confident=True,
                                                 adapter_for_family="greenhouse")
        ladder = router_mod.fallback_candidates(routed)
        assert [r.execution_class for r in ladder] == [ExecutionClass.BROWSER]
        assert ladder[0].fallback_rank > routed.fallback_rank

    def test_strategy_order_cheap_to_expensive(self):
        assert router_mod.strategy_rank(Strategy.FEED_OR_PUBLIC_STRUCTURED_ENDPOINT) < \
               router_mod.strategy_rank(Strategy.HTTP_HTML) < \
               router_mod.strategy_rank(Strategy.PLAYWRIGHT_PUBLIC) < \
               router_mod.strategy_rank(Strategy.MANUAL_UNSUPPORTED)


# ---------------------------------------------- obligation crash-safety
def test_obligation_crashed_running_is_reclaimed_and_bounded(db):
    """Regression: a crashed drain left obligations RUNNING forever (no
    reclamation); now expired leases are reclaimed and attempts are bounded."""
    from jobscraper.db.connection import immediate_transaction
    from jobscraper.runtime import obligations as obl
    from jobscraper.timeutil import add_seconds, utc_now_s

    with immediate_transaction(db.conn) as tx:
        obl_id = obl.create_obligation(tx, kind="PROCESS", observation_id="obs-none",
                                       run_id=None, request_id=None)
    first = obl.claim_pending(db, limit=10)
    assert [o["id"] for o in first] == [obl_id]
    assert first[0]["attempt_count"] == 0  # pre-claim snapshot

    # Simulate a crash: obligation stays RUNNING, lease expires.
    expired = add_seconds(utc_now_s(), -3600)
    db.execute("UPDATE processing_obligations SET lease_until=? WHERE id=?", (expired, obl_id))
    second = obl.claim_pending(db, limit=10)
    assert [o["id"] for o in second] == [obl_id]  # reclaimed and re-claimed
    row = db.query_one("SELECT * FROM processing_obligations WHERE id=?", (obl_id,))
    assert row["status"] == "RUNNING" and row["attempt_count"] == 2

    # Poison bound: after max_attempts the obligation is CANCELLED (terminal),
    # never re-claimed — evidence stays, processing stops.
    db.execute("UPDATE processing_obligations SET attempt_count=5, lease_until=? WHERE id=?", (expired, obl_id))
    assert obl.claim_pending(db, limit=10) == []
    row = db.query_one("SELECT * FROM processing_obligations WHERE id=?", (obl_id,))
    assert row["status"] == "CANCELLED"
    assert obl.pending_count(db) == 0
