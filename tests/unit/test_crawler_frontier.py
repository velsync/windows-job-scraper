from jobscraper.acquisition.crawler.budget import CrawlBudget, CrawlUsage
from jobscraper.acquisition.crawler.frontier import enqueue_discovered_task
from jobscraper.acquisition.crawler.scope import CrawlScope
from jobscraper.adapters.contract import DiscoveredTask


def _plan():
    return {"id":"rsp","source_id":"src","binding_id":"bnd","strategy":"HTTP_HTML","execution_class":"HTTP"}


def _scope():
    return CrawlScope(frozenset({"jobs.example.test"}), ("/",), (), 3)


def _budget():
    return CrawlBudget(10, 20, 100000, 60, 10, 3, 20)


def _usage():
    return CrawlUsage(0, 0, 0, 0, 0, 0.0, 0)


def test_opaque_detail_reference_remains_valid_until_adapter_plans_reviewed_url(monkeypatch):
    captured = {}
    def fake_enqueue(_conn, **kwargs):
        captured.update(kwargs)
        return "req1", True
    monkeypatch.setattr("jobscraper.acquisition.crawler.frontier.enqueue_request", fake_enqueue)
    task = DiscoveredTask(kind="DETAIL", logical_key="job-42", target_reference="42", depth=1)
    decision = enqueue_discovered_task(None, run_id="run", plan_row=_plan(), parent_request_id="parent", discovered=task, scope=_scope(), budget=_budget(), usage=_usage(), base_url="https://jobs.example.test/list", now="now")
    assert decision.accepted and captured["request_type"] == "DETAIL_FETCH"
    assert captured["target_identity"] == "42"


def test_crawl_reference_must_be_in_scope_before_durable_enqueue(monkeypatch):
    monkeypatch.setattr("jobscraper.acquisition.crawler.frontier.enqueue_request", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not enqueue")))
    task = DiscoveredTask(kind="CRAWL", logical_key="x", target_reference="https://evil.example/jobs", depth=1)
    decision = enqueue_discovered_task(None, run_id="run", plan_row=_plan(), parent_request_id="parent", discovered=task, scope=_scope(), budget=_budget(), usage=_usage(), base_url=None, now="now")
    assert not decision.accepted and decision.reason == "SCOPE_DENIED"


def test_frontier_budget_refusal_is_visible_and_does_not_enqueue(monkeypatch):
    monkeypatch.setattr("jobscraper.acquisition.crawler.frontier.enqueue_request", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not enqueue")))
    task = DiscoveredTask(kind="CRAWL", logical_key="x", target_reference="https://jobs.example.test/jobs/2", depth=1)
    usage = CrawlUsage(0, 20, 0, 0, 0, 1.0, 20)
    decision = enqueue_discovered_task(None, run_id="run", plan_row=_plan(), parent_request_id="parent", discovered=task, scope=_scope(), budget=_budget(), usage=usage, base_url=None, now="now")
    assert not decision.accepted and decision.reason == "BUDGET_MAX_REQUESTS"
