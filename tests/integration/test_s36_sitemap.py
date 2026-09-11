from __future__ import annotations

import json

from jobscraper.acquisition.crawler.budget import CrawlUsage, budget_from_plan
from jobscraper.acquisition.crawler.scope import scope_from_plan
from jobscraper.acquisition.crawler.sitemap import (
    SitemapDocumentKind,
    discover_sitemaps_from_robots,
    enqueue_robots_sitemaps,
    enqueue_sitemap_result,
    parse_sitemap,
)
from jobscraper.db.connection import Database
from jobscraper.db.migrations import migrate_schema
from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.runs import create_run
from jobscraper.version import SCHEMA_VERSION


def _setup(conn, *, max_requests: int = 20):
    now = db_utc_now(conn)
    conn.execute(
        "INSERT INTO sources(id,display_name,source_family,entry_url,desired_state,"
        "administrative_state,robots_mode,created_at,updated_at)"
        " VALUES ('src','Fixture','CAREERS','https://jobs.example.test/start',"
        "'ENABLED','NORMAL','RESPECT',?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO adapter_definitions(adapter_id,adapter_version,adapter_api_version,"
        "manifest_json,created_at) VALUES ('fixture','1','1','{}',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profiles(id,display_name,created_at)"
        " VALUES ('perm','fixture',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO adapter_permission_profile_revisions(id,permission_profile_id,"
        "revision,policy_json,created_at) VALUES ('permrev','perm',1,'{}',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO source_adapter_bindings(id,source_id,display_name,desired_state,"
        "administrative_state,created_at) VALUES ('bnd','src','crawler','ENABLED','NORMAL',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO source_adapter_binding_revisions(id,binding_id,revision,adapter_id,"
        "adapter_version,strategy,config_json,auth_requirement,execution_class,"
        "permission_profile_id,permission_profile_revision,created_at)"
        " VALUES ('bndrev','bnd',1,'fixture','1','HTTP_HTML','{}','NONE','HTTP','perm',1,?)",
        (now,),
    )
    conn.commit()
    run_id, plans = create_run(
        conn,
        profile_id=None,
        plans=[{
            "source_id": "src",
            "source_plan_group_id": "grp",
            "fallback_rank": 0,
            "binding_id": "bnd",
            "binding_revision_id": "bndrev",
            "adapter_id": "fixture",
            "adapter_version": "1",
            "adapter_api_version": "1",
            "strategy": "HTTP_HTML",
            "execution_class": "HTTP",
            "permission_profile_id": "perm",
            "permission_profile_revision": 1,
            "cursor_schema_version": 1,
            "crawl_policy_snapshot_json": {
                "max_pages": 10,
                "max_requests": max_requests,
                "max_bytes": 1000000,
                "max_runtime_s": 120,
                "max_detail_requests": 10,
                "max_depth": 3,
            },
        }],
        now=now,
    )
    plan_id = plans[0]
    plan = conn.execute("SELECT * FROM run_source_plans WHERE id=?", (plan_id,)).fetchone()
    source = conn.execute("SELECT * FROM sources WHERE id='src'").fetchone()
    scope = scope_from_plan(plan, source, destination_allowed_hosts={"jobs.example.test"})
    budget = budget_from_plan(plan)
    return now, run_id, plan, scope, budget


def test_offline_sitemap_fixture_drives_only_scoped_unique_durable_frontier(tmp_path):
    db = Database(tmp_path / "s36.db")
    try:
        migrate_schema(db.conn, SCHEMA_VERSION)
        now, run_id, plan, scope, budget = _setup(db.conn)

        discovery = discover_sitemaps_from_robots(
            ["Sitemap: https://jobs.example.test/sitemap.xml"]
        )
        first = enqueue_robots_sitemaps(
            db.conn,
            discovery=discovery,
            run_id=run_id,
            plan_row=plan,
            parent_request_id=None,
            scope=scope,
            budget=budget,
            base_url="https://jobs.example.test/robots.txt",
            now=now,
        )
        assert first.created == 1 and first.reused == 0

        parsed = parse_sitemap(
            b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://jobs.example.test/about</loc><lastmod>2026-09-01</lastmod></url>
              <url><loc>https://jobs.example.test/careers/new</loc><lastmod>2026-09-11</lastmod></url>
              <url><loc>https://outside.example/jobs/escape</loc><lastmod>2026-09-11</lastmod></url>
              <url><loc>https://jobs.example.test/careers/new?utm_source=duplicate</loc></url>
            </urlset>""",
            sitemap_url="https://jobs.example.test/sitemap.xml",
        )
        assert parsed.kind is SitemapDocumentKind.URLSET
        enqueued = enqueue_sitemap_result(
            db.conn,
            parsed=parsed,
            current_sitemap_depth=0,
            run_id=run_id,
            plan_row=plan,
            parent_request_id=None,
            scope=scope,
            budget=budget,
            base_url="https://jobs.example.test/sitemap.xml",
            now=now,
            page_depth=1,
        )
        assert enqueued.created == 2
        assert any(d.code == "SCOPE_DENIED" for d in enqueued.diagnostics)

        rows = db.conn.execute(
            "SELECT payload_json,priority,depth FROM scrape_requests"
            " WHERE run_id=? ORDER BY priority DESC,id",
            (run_id,),
        ).fetchall()
        payloads = [json.loads(r["payload_json"]) for r in rows]
        assert sum(p.get("role") == "SITEMAP" for p in payloads) == 1
        page_rows = [
            (p, row) for p, row in zip(payloads, rows) if p.get("role") == "PAGE"
        ]
        assert len(page_rows) == 2
        assert page_rows[0][0]["target_reference"].endswith("/careers/new")
        assert page_rows[0][1]["priority"] > page_rows[1][1]["priority"]
        assert all(row[1]["depth"] == 1 for row in page_rows)

        # Reprocessing the same accepted sitemap is restart/idempotent: no
        # second durable unit is created for the same normalized URL.
        again = enqueue_sitemap_result(
            db.conn,
            parsed=parsed,
            current_sitemap_depth=0,
            run_id=run_id,
            plan_row=plan,
            parent_request_id=None,
            scope=scope,
            budget=budget,
            base_url="https://jobs.example.test/sitemap.xml",
            now=now,
            page_depth=1,
        )
        assert again.created == 0
        assert again.reused == 2
        assert db.conn.execute(
            "SELECT COUNT(*) FROM scrape_requests WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 3
    finally:
        db.close()


def test_sitemap_frontier_respects_durable_request_budget(tmp_path):
    db = Database(tmp_path / "s36-budget.db")
    try:
        migrate_schema(db.conn, SCHEMA_VERSION)
        now, run_id, plan, scope, budget = _setup(db.conn, max_requests=1)
        parsed = parse_sitemap(
            b"""<urlset>
              <url><loc>https://jobs.example.test/jobs/1</loc></url>
              <url><loc>https://jobs.example.test/jobs/2</loc></url>
            </urlset>""",
            sitemap_url="https://jobs.example.test/sitemap.xml",
        )
        out = enqueue_sitemap_result(
            db.conn,
            parsed=parsed,
            current_sitemap_depth=0,
            run_id=run_id,
            plan_row=plan,
            parent_request_id=None,
            scope=scope,
            budget=budget,
            base_url="https://jobs.example.test/sitemap.xml",
            now=now,
            page_depth=1,
        )
        assert out.created == 1
        assert any(d.code == "BUDGET_MAX_REQUESTS" for d in out.diagnostics)
        assert db.conn.execute(
            "SELECT COUNT(*) FROM scrape_requests WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 1
    finally:
        db.close()
