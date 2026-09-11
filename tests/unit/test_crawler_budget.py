import sqlite3

from jobscraper.acquisition.crawler.budget import (
    CrawlBudget,
    CrawlUsage,
    budget_from_plan,
    check_budget,
    close_unstarted_over_budget,
    load_usage,
)
from jobscraper.adapters.contract import StopPolicy


def test_adapter_stop_policy_only_tightens_host_budget():
    plan = {"crawl_policy_snapshot_json": '{"max_pages":40,"max_requests":150,"max_runtime_s":500,"max_bytes":90000000,"max_detail_requests":50,"max_depth":7,"execution_class_budgets":{"HTTP":{"max_requests":120}}}'}
    stop = StopPolicy(max_pages=3, max_requests=8, max_runtime_s=100.0)
    budget = budget_from_plan(plan, stop_policy=stop)
    assert (budget.max_pages, budget.max_requests, budget.max_runtime_s) == (3, 8, 100.0)
    assert budget.max_http_requests == 8
    assert budget.max_detail_requests == 50
    assert budget.max_depth == 7


def test_budget_enforces_http_and_page_dimensions_independently():
    budget = CrawlBudget(2, 10, 1000, 60.0, 3, 2, max_http_requests=4)
    assert check_budget(budget, CrawlUsage(2, 2, 0, 10, 1, 1.0, 2), proposed_request_type="SOURCE_CRAWL").reason == "MAX_PAGES"
    assert check_budget(budget, CrawlUsage(1, 4, 0, 10, 1, 1.0, 4), proposed_request_type="DETAIL_FETCH", proposed_execution_class="HTTP").reason == "MAX_HTTP_REQUESTS"


def test_close_unstarted_over_budget_never_touches_running_work():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE scrape_requests(id TEXT PRIMARY KEY, run_source_plan_id TEXT, request_type TEXT, status TEXT, finished_at TEXT, updated_at TEXT, last_failure_kind TEXT, last_failure_json TEXT)")
    conn.executemany("INSERT INTO scrape_requests(id,run_source_plan_id,request_type,status) VALUES (?,?,?,?)", [
        ("p","rsp","SOURCE_CRAWL","PENDING"),
        ("w","rsp","SOURCE_CRAWL","RETRY_WAIT"),
        ("r","rsp","SOURCE_CRAWL","RUNNING"),
    ])
    changed = close_unstarted_over_budget(conn, "rsp", request_types=frozenset({"SOURCE_CRAWL"}), reason="MAX_BYTES", now="2026-09-11T12:00:00Z")
    assert changed == 2
    rows = dict(conn.execute("SELECT id,status FROM scrape_requests"))
    assert rows == {"p":"FAILED", "w":"FAILED", "r":"RUNNING"}


def test_load_usage_counts_robots_request_but_not_as_enumeration_page():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE scrape_runs(id TEXT PRIMARY KEY, started_at TEXT);
    CREATE TABLE run_source_plans(id TEXT PRIMARY KEY, run_id TEXT, created_at TEXT);
    CREATE TABLE scrape_requests(id TEXT PRIMARY KEY, run_source_plan_id TEXT, request_type TEXT, execution_class TEXT, payload_json TEXT, depth INTEGER);
    CREATE TABLE fetch_attempts(id TEXT PRIMARY KEY, request_id TEXT, bytes_downloaded INTEGER);
    INSERT INTO scrape_runs VALUES('run','2026-09-11T12:00:00Z');
    INSERT INTO run_source_plans VALUES('rsp','run','2026-09-11T12:00:00Z');
    INSERT INTO scrape_requests VALUES('robots','rsp','SOURCE_CRAWL','HTTP','{"role":"ROBOTS"}',0);
    INSERT INTO scrape_requests VALUES('page','rsp','SOURCE_CRAWL','HTTP','{"role":"PAGE"}',0);
    INSERT INTO fetch_attempts VALUES('f1','robots',10);
    INSERT INTO fetch_attempts VALUES('f2','page',20);
    """)
    usage = load_usage(conn, "rsp", now="2026-09-11T12:00:10Z")
    assert usage.requests_created == 2
    assert usage.http_requests_created == 2
    assert usage.pages_completed == 1
    assert usage.bytes_downloaded == 30
