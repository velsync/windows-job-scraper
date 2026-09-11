from jobscraper.acquisition.crawler.scope import check_scope, scope_from_plan


def test_scope_snapshot_can_only_narrow_destination_host_authority():
    plan = {"crawl_policy_snapshot_json": '{"allowed_hosts":["jobs.example.test","evil.example"],"allowed_path_prefixes":["/jobs"]}'}
    source = {"entry_url": "https://jobs.example.test/jobs"}
    scope = scope_from_plan(plan, source, destination_allowed_hosts={"jobs.example.test"})
    assert scope.allowed_hosts == frozenset({"jobs.example.test"})
    assert check_scope(scope, "https://jobs.example.test/jobs/1", depth=1).allowed
    assert not check_scope(scope, "https://evil.example/jobs/1", depth=1).allowed


def test_scope_enforces_path_and_depth_bounds():
    plan = {"crawl_policy_snapshot_json": '{"allowed_path_prefixes":["/careers"],"deny_path_prefixes":["/careers/admin"],"max_depth":2}'}
    source = {"entry_url": "https://jobs.example.test/careers"}
    scope = scope_from_plan(plan, source)
    assert check_scope(scope, "/careers/job/1", base=source["entry_url"], depth=2).allowed
    assert check_scope(scope, "/other", base=source["entry_url"], depth=1).reason == "PATH_OUT_OF_SCOPE"
    assert check_scope(scope, "/careers/admin/x", base=source["entry_url"], depth=1).reason == "PATH_DENIED"
    assert check_scope(scope, "/careers/job/2", base=source["entry_url"], depth=3).reason == "MAX_DEPTH"
