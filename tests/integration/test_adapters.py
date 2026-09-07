"""Adapter integration: lever, ashby (incl. pagination) and generic
(JSON-LD + recipe) through the full engine pipeline (Slice 2 breadth)."""

import json

import pytest

from jobscraper.acquisition.adapters import ashby as ashby_mod
from jobscraper.acquisition.registry import AdapterRegistry
from jobscraper.config import AppConfig
from jobscraper.domain.profiles import create_profile
from jobscraper.domain.sources import (
    create_binding,
    create_source,
    seed_builtin_permission_profile,
)
from jobscraper.runtime.engine import RunEngine
from jobscraper.security.netpolicy import make_test_fixture_policy
from tests.fixtures.fixture_server import FixtureServer

STRATEGY = "FEED_OR_PUBLIC_STRUCTURED_ENDPOINT"


def _setup(db, server, *, path, payload, adapter, config, family, html=False):
    seed_builtin_permission_profile(db)
    if html:
        server.add_html(path, payload)
    else:
        server.add_json(path, payload)
    source_id = create_source(
        db, display_name=f"{family} src", entry_url=server.base_url + path,
        canonical_host="127.0.0.1", source_family=family,
    )
    create_binding(
        db, source_id=source_id, display_name=f"{family} binding", adapter_id=adapter,
        adapter_version="1.0.0", strategy=STRATEGY, config=config,
    )
    profile_id = create_profile(
        db, name="P", snapshot_overrides={"home_country": "DE", "keywords": ["python"], "min_score_inbox": 0},
    )
    engine = RunEngine(
        AppConfig(data_root=db.path.parent), db, AdapterRegistry(),
        policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
    )
    return source_id, profile_id, engine


# ------------------------------------------------------------------- lever
LEVER_POSTINGS = [
    {
        "id": "lv-1",
        "text": "Backend Engineer (Python)",
        "hostedUrl": "https://jobs.example/acme/lv-1",
        "applyUrl": "https://jobs.example/acme/lv-1/apply",
        "descriptionPlain": "Python FastAPI backend role. Visa sponsorship available. Remote (Germany).",
        "categories": {"location": "Remote (Germany)", "commitment": "Full-time", "workplaceType": "remote"},
    },
    {
        "id": "lv-2",
        "text": "Sales Lead",
        "hostedUrl": "https://jobs.example/acme/lv-2",
        "descriptionPlain": "Lead the sales team in New York, NY, USA.",
        "categories": {"location": "New York, NY, USA", "commitment": "Full-time"},
    },
]


def test_lever_end_to_end(db, data_root):
    with FixtureServer() as server:
        server.add_json("/v0/postings/acme", LEVER_POSTINGS)
        seed_builtin_permission_profile(db)
        src = create_source(
            db, display_name="Lever src", entry_url=server.base_url + "/v0/postings/acme",
            canonical_host="127.0.0.1", source_family="lever",
        )
        create_binding(
            db, source_id=src, display_name="Lever", adapter_id="lever", adapter_version="1.0.0",
            strategy=STRATEGY, config={"company": "acme", "base_url": server.base_url},
        )
        profile = create_profile(db, name="P", snapshot_overrides={"keywords": ["python"], "min_score_inbox": 0})
        engine = RunEngine(
            AppConfig(data_root=data_root), db, AdapterRegistry(),
            policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
        )
        report = engine.execute_run(engine.create_run(profile_id=profile, source_ids=[src]))
        assert report.status == "SUCCEEDED"

        obs = {o["source_job_id"]: o for o in db.query("SELECT * FROM job_observations")}
        assert set(obs) == {"lv-1", "lv-2"}
        assert obs["lv-1"]["strategy"] == STRATEGY
        content = json.loads(obs["lv-1"]["content_json"])
        assert content["title"] == "Backend Engineer (Python)"
        assert content["location_raw"] == "Remote (Germany)"

        js = db.query_one("SELECT * FROM job_sources WHERE source_job_id='lv-1'")
        assert js["application_url"].endswith("/apply")
        assert js["last_authoritative_scope_key"] == "lever:acme"

        # Lever is absence-authoritative: full board enumerated.
        cov = db.query_one("SELECT * FROM enumeration_coverage")
        assert cov["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
        assert cov["completion_state"] == "COMPLETE"


def test_lever_absence_after_job_disappears(db, data_root):
    with FixtureServer() as server:
        server.add_json("/v0/postings/acme", LEVER_POSTINGS)
        seed_builtin_permission_profile(db)
        src = create_source(
            db, display_name="Lever src", entry_url=server.base_url + "/v0/postings/acme",
            canonical_host="127.0.0.1", source_family="lever",
        )
        create_binding(
            db, source_id=src, display_name="Lever", adapter_id="lever", adapter_version="1.0.0",
            strategy=STRATEGY, config={"company": "acme", "base_url": server.base_url},
        )
        profile = create_profile(db, name="P", snapshot_overrides={"keywords": ["python"], "min_score_inbox": 0})
        engine = RunEngine(
            AppConfig(data_root=data_root), db, AdapterRegistry(),
            policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
        )
        engine.execute_run(engine.create_run(profile_id=profile, source_ids=[src]))
        # Board drops lv-2.
        server.add_json("/v0/postings/acme", [LEVER_POSTINGS[0]])
        engine.execute_run(engine.create_run(profile_id=profile, source_ids=[src]))
        js = db.query_one("SELECT * FROM job_sources WHERE source_job_id='lv-2'")
        assert js["presence_state"] == "UNCERTAIN"  # absence -> uncertainty, not closure
        js1 = db.query_one("SELECT * FROM job_sources WHERE source_job_id='lv-1'")
        assert js1["presence_state"] == "ACTIVE"


# ------------------------------------------------------------------- ashby
_ASHBY_TITLES = [
    ("Platform Engineer", "python kubernetes platform"),
    ("Data Engineer", "python dbt pipelines"),
    ("Product Designer", "design systems"),
    ("Finance Analyst", "forecasting"),
]


def _ashby_jobs(count, offset=0):
    return [
        {
            "id": f"ash-{offset + i}",
            "title": _ASHBY_TITLES[offset + i][0],
            "jobUrl": f"https://jobs.example/acme/ash-{offset + i}",
            "applyUrl": f"https://jobs.example/acme/ash-{offset + i}/apply",
            "description": f"<p>{_ASHBY_TITLES[offset + i][1]} role. Full-time. Berlin, Germany.</p>",
            "location": "Berlin, Germany",
            "isRemote": False,
            "employmentType": "Full-time",
            "publishedAt": "2026-08-01T00:00:00Z",
            "salaryRangeDescription": "€80k - €95k",
        }
        for i in range(count)
    ]


def test_ashby_end_to_end_with_pagination(db, data_root, monkeypatch):
    # Small page limit so pagination actually happens: page0=3 jobs, page1=1.
    monkeypatch.setattr(ashby_mod, "PAGE_LIMIT", 3)
    with FixtureServer() as server:

        def ashby_handler(req, body):
            import json as _json
            from urllib.parse import urlsplit, parse_qs

            query = parse_qs(urlsplit(req.path).query)
            page = int((query.get("pageNo") or ["0"])[0])
            jobs = _ashby_jobs(3) if page == 0 else _ashby_jobs(1, offset=3)
            return 200, "application/json", _json.dumps({"jobs": jobs}), {}

        server.add("GET", "/posting-api/job-board/acme", ashby_handler)
        seed_builtin_permission_profile(db)
        src = create_source(
            db, display_name="Ashby src", entry_url=server.base_url + "/posting-api/job-board/acme",
            canonical_host="127.0.0.1", source_family="ashby",
        )
        create_binding(
            db, source_id=src, display_name="Ashby", adapter_id="ashby", adapter_version="1.0.0",
            strategy=STRATEGY, config={"org": "acme", "base_url": server.base_url},
        )
        profile = create_profile(db, name="P", snapshot_overrides={"keywords": ["python"], "min_score_inbox": 0})
        engine = RunEngine(
            AppConfig(data_root=data_root), db, AdapterRegistry(),
            policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
        )
        report = engine.execute_run(engine.create_run(profile_id=profile, source_ids=[src]))
        assert report.status == "SUCCEEDED"

        # All 4 identities across two pages.
        ids = {r["source_job_id"] for r in db.query("SELECT source_job_id FROM job_observations")}
        assert ids == {"ash-0", "ash-1", "ash-2", "ash-3"}
        # Both pages contributed to coverage; cursor reached the final page.
        assert db.query_one("SELECT COUNT(*) c FROM coverage_contributing_request")["c"] == 2
        cov = db.query_one("SELECT * FROM enumeration_coverage")
        assert cov["completion_state"] == "COMPLETE"
        assert cov["coverage_authority"] == "AUTHORITATIVE_FULL_SOURCE"
        cursor = db.query_one("SELECT * FROM crawl_cursors")
        assert json.loads(cursor["state_json"])["page"] == 0  # reset for next enumeration

        # Salary text made it into the canonical projection.
        job = db.query_one("SELECT * FROM jobs WHERE title='Platform Engineer'")
        assert job is not None
        assert job["salary_original_text"] == "€80k - €95k"
        assert job["salary_currency"] == "EUR"


# ------------------------------------------------------------------ generic
JSONLD_HTML = """<!doctype html>
<html><head><title>Acme Careers</title>
<script type="application/ld+json">
[
 {"@type": "JobPosting", "title": "Data Engineer (Python)",
  "url": "https://careers.example/jobs/44001",
  "hiringOrganization": {"name": "Acme Analytics"},
  "jobLocation": {"address": {"addressLocality": "Berlin", "addressCountry": "DE"}},
  "description": "Build data pipelines with Python and dbt."},
 {"@type": "JobPosting", "title": "Recruiter",
  "url": "https://careers.example/jobs/44002",
  "hiringOrganization": {"name": "Acme Analytics"},
  "jobLocation": {"address": {"addressLocality": "New York", "addressCountry": "US"}},
  "description": "Grow the team."}
]
</script></head>
<body><h1>Open roles</h1></body></html>
"""


def test_generic_jsonld_end_to_end(db, data_root):
    with FixtureServer() as server:
        server.add_html("/careers", JSONLD_HTML)
        seed_builtin_permission_profile(db)
        src = create_source(
            db, display_name="Careers page", entry_url=server.base_url + "/careers",
            canonical_host="127.0.0.1", source_family="generic",
        )
        create_binding(
            db, source_id=src, display_name="JSON-LD", adapter_id="generic", adapter_version="1.0.0",
            strategy="FALLBACK_PARSED_HTML", config={"entry_url": server.base_url + "/careers"},
        )
        profile = create_profile(db, name="P", snapshot_overrides={"keywords": ["python"], "min_score_inbox": 0})
        engine = RunEngine(
            AppConfig(data_root=data_root), db, AdapterRegistry(),
            policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
        )
        report = engine.execute_run(engine.create_run(profile_id=profile, source_ids=[src]))
        assert report.status == "SUCCEEDED"

        jobs = {j["title"]: j for j in db.query("SELECT * FROM jobs")}
        assert set(jobs) == {"Data Engineer (Python)", "Recruiter"}
        js = db.query_one(
            "SELECT * FROM job_sources WHERE source_job_id='44001'"
        )
        assert js["canonical_job_url"] == "https://careers.example/jobs/44001"
        # Generic pages prove page scope only: no absence inference.
        cov = db.query_one("SELECT * FROM enumeration_coverage")
        assert cov["completion_state"] == "COMPLETE"
        assert cov["coverage_authority"] == "NO_ABSENCE_INFERENCE"
        assert cov["absence_inference_allowed"] == 0


RECIPE_HTML = """<!doctype html>
<html><body>
<div class="board">
  <div data-testid="job-card">
    <h3>Site Reliability Engineer</h3>
    <a href="/careers/jobs/88001">View role</a>
    <p data-testid="job-location">Amsterdam, Netherlands</p>
  </div>
  <div data-testid="job-card">
    <h3>Finance Analyst</h3>
    <a href="/careers/jobs/88002">View role</a>
    <p data-testid="job-location">London, UK</p>
  </div>
</div>
</body></html>
"""

RECIPE = {
    "schema_version": 1,
    "mode": "HTML",
    "card_locators": [{"kind": "data_attr", "value": "data-testid=job-card"}],
    "fields": {
        "title": {"required": True, "locators": [{"kind": "relative_css", "value": "h3"}]},
        "job_url": {"locators": [{"kind": "href_pattern", "value": "/careers/jobs/"}]},
        "location_raw": {"locators": [{"kind": "data_attr", "value": "data-testid=job-location"}]},
    },
}


def test_generic_recipe_end_to_end(db, data_root):
    with FixtureServer() as server:
        server.add_html("/careers", RECIPE_HTML)
        seed_builtin_permission_profile(db)
        src = create_source(
            db, display_name="Recipe board", entry_url=server.base_url + "/careers",
            canonical_host="127.0.0.1", source_family="generic",
        )
        create_binding(
            db, source_id=src, display_name="Recipe", adapter_id="generic", adapter_version="1.0.0",
            strategy="FALLBACK_PARSED_HTML",
            config={"entry_url": server.base_url + "/careers", "recipe": RECIPE},
        )
        profile = create_profile(db, name="P", snapshot_overrides={"keywords": ["python"], "min_score_inbox": 0})
        engine = RunEngine(
            AppConfig(data_root=data_root), db, AdapterRegistry(),
            policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
        )
        report = engine.execute_run(engine.create_run(profile_id=profile, source_ids=[src]))
        assert report.status == "SUCCEEDED"

        jobs = {j["title"]: j for j in db.query("SELECT * FROM jobs")}
        assert set(jobs) == {"Site Reliability Engineer", "Finance Analyst"}
        # href_pattern resolved against the page base URL.
        js = db.query_one("SELECT * FROM job_sources WHERE canonical_job_url LIKE '%88001'")
        assert js["canonical_job_url"].startswith(server.base_url)
        # Recipe locations land in the canonical projection.
        sre = jobs["Site Reliability Engineer"]
        locs = db.query("SELECT * FROM job_locations WHERE job_id=?", (sre["id"],))
        assert any(loc["city"] == "Amsterdam" for loc in locs)


def test_generic_recipe_missing_required_field_is_typed_failure(db, data_root):
    """Required-field failure produces a typed outcome, never invented data."""
    from jobscraper.acquisition.adapters import generic as generic_mod
    from jobscraper.acquisition.contracts import ParseOutcomeKind
    from jobscraper.recipes.models import ExtractionRecipe

    recipe = ExtractionRecipe.from_json(
        {
            "schema_version": 1,
            "mode": "HTML",
            "card_locators": [{"kind": "data_attr", "value": "data-testid=job-card"}],
            "fields": {"title": {"required": True, "locators": [{"kind": "relative_css", "value": "h3"}]}},
        }
    )
    html = '<html><body><div data-testid="job-card"><p>no heading here</p></div></body></html>'
    adapter = generic_mod.GenericAdapter("https://x.example/careers", recipe=recipe)
    body = html.encode()

    class _R:  # minimal envelope for parse
        pass

    class _Env:
        pass

    env = _Env()
    inner = _R()
    inner.body = body
    inner.final_url = "https://x.example/careers"
    inner.requested_url = "https://x.example/careers"
    env.result = inner
    from jobscraper.acquisition.contracts import ValidatedResultEnvelope

    validated = ValidatedResultEnvelope(
        result_envelope_ref="x", result=inner, validated_page_class="VALID_LIST",
        validation_evidence_ref="t", security_policy_result="ALLOWED",
    )
    outcome = adapter.parse(
        __import__("jobscraper.acquisition.contracts", fromlist=["AdapterTask"]).AdapterTask(
            kind=__import__("jobscraper.acquisition.contracts", fromlist=["AdapterTaskKind"]).AdapterTaskKind.ENUMERATE,
            payload={},
        ),
        validated,
        __import__("jobscraper.acquisition.contracts", fromlist=["ParseContext"]).ParseContext(
            request_id="r", attempt_id="a", run_source_plan_id="p", parser_version="t",
            recipe_version="1",
        ),
    )
    assert outcome.kind == ParseOutcomeKind.FAILURE
    assert outcome.failure_kind == "PARSE_EMPTY"
    assert outcome.observations == ()
