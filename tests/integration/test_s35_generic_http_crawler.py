from __future__ import annotations

import json

import pytest

from jobscraper.acquisition.envelope import RequestPlan, validate_envelope
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.adapters.contract import (
    AdapterTaskKind, CrawlCursor, ParseOutcome, ParseOutcomeKind, StopPolicy,
)
from jobscraper.db.connection import Database
from jobscraper.db.migrations import migrate_schema
from jobscraper.pipeline.driver import execute_run
from jobscraper.runtime.clock import begin_service_epoch, db_utc_now
from jobscraper.runtime.requests import enqueue_request
from jobscraper.runtime.runs import create_run
from jobscraper.version import SCHEMA_VERSION


class _CrawlerAdapter:
    listing_identity_sufficient = True
    stop_policy = StopPolicy(
        max_pages=4,
        max_consecutive_empty_pages=2,
        max_consecutive_no_new_jobs_pages=3,
        max_duplicate_pages=2,
        max_runtime_s=120.0,
        max_requests=8,
    )

    def plan(self, task, cursor, ctx=None):
        assert task.kind is AdapterTaskKind.CRAWL
        if cursor is None:
            url = task.payload['target_reference']
        else:
            url = json.loads(cursor.state_json)['url']
        return RequestPlan(method='GET', url=url, expected_content_types=('application/json',), timeout_s=5.0, max_bytes=10000, purpose='SOURCE_CRAWL')

    def parse(self, task, result, ctx=None):
        payload = json.loads(result.envelope.body.decode('utf-8'))
        next_url = payload.get('next')
        if next_url:
            cursor = CrawlCursor(
                source_id='src', binding_id='bnd', adapter_id='fixture_crawler',
                adapter_version='1.0.0', cursor_schema_version=1,
                state_json=json.dumps({'url': next_url}, sort_keys=True, separators=(',', ':')),
                checkpoint_at=result.envelope.fetched_at or 'unknown',
            )
            return ParseOutcome(kind=ParseOutcomeKind.SUCCESS_WITH_JOBS, cursor_proposal=cursor)
        return ParseOutcome(kind=ParseOutcomeKind.SUCCESS_EMPTY)

    def next_cursor(self, task, outcome, current_cursor, ctx=None):
        return outcome.cursor_proposal


def _setup(conn):
    now=db_utc_now(conn)
    conn.execute("INSERT INTO sources(id,display_name,source_family,entry_url,desired_state,administrative_state,robots_mode,created_at,updated_at) VALUES ('src','Fixture','CAREERS','http://127.0.0.1:8765/p1','ENABLED','NORMAL','RESPECT',?,?)", (now,now))
    conn.execute("INSERT INTO adapter_definitions(adapter_id,adapter_version,adapter_api_version,manifest_json,created_at) VALUES ('fixture_crawler','1.0.0','1','{}',?)", (now,))
    conn.execute("INSERT INTO adapter_permission_profiles(id,display_name,created_at) VALUES ('perm','fixture',?)", (now,))
    conn.execute("INSERT INTO adapter_permission_profile_revisions(id,permission_profile_id,revision,policy_json,created_at) VALUES ('permrev','perm',1,'{}',?)", (now,))
    conn.execute("INSERT INTO source_adapter_bindings(id,source_id,display_name,desired_state,administrative_state,created_at) VALUES ('bnd','src','crawler','ENABLED','NORMAL',?)", (now,))
    conn.execute("INSERT INTO source_adapter_binding_revisions(id,binding_id,revision,adapter_id,adapter_version,strategy,config_json,auth_requirement,execution_class,permission_profile_id,permission_profile_revision,created_at) VALUES ('bndrev','bnd',1,'fixture_crawler','1.0.0','HTTP_HTML','{}','NONE','HTTP','perm',1,?)", (now,))
    conn.commit()
    run_id, plan_ids=create_run(conn,profile_id=None,plans=[{
        'source_id':'src','source_plan_group_id':'grp','fallback_rank':0,
        'binding_id':'bnd','binding_revision_id':'bndrev','adapter_id':'fixture_crawler',
        'adapter_version':'1.0.0','adapter_api_version':'1','strategy':'HTTP_HTML',
        'execution_class':'HTTP','permission_profile_id':'perm','permission_profile_revision':1,
        'cursor_schema_version':1,
        'crawl_policy_snapshot_json':{'max_pages':4,'max_requests':8,'max_bytes':100000,'max_runtime_s':120,'max_detail_requests':4,'max_depth':2},
    }],now=now)
    plan_id=plan_ids[0]
    enqueue_request(conn,run_id=run_id,run_source_plan_id=plan_id,source_id='src',binding_id='bnd',request_type='SOURCE_CRAWL',target_identity='http://127.0.0.1:8765/p1',payload={'role':'PAGE','target_reference':'http://127.0.0.1:8765/p1'},strategy='HTTP_HTML',execution_class='HTTP',now=now)
    begin_service_epoch(conn, now=now)
    return run_id, plan_id


def _fake_transport(envelope, policy):
    validate_envelope(envelope, policy)
    url=envelope.payload.url
    if url.endswith('/robots.txt'):
        body=b'User-agent: *\nAllow: /\n'; ctype='text/plain'
    elif url.endswith('/p1'):
        body=json.dumps({'jobs':[{'id':'1'}], 'next':'http://127.0.0.1:8765/p2'}).encode(); ctype='application/json'
    elif url.endswith('/p2'):
        body=json.dumps({'jobs':[{'id':'2'}], 'next':'http://127.0.0.1:8765/p3'}).encode(); ctype='application/json'
    elif url.endswith('/p3'):
        body=json.dumps({'jobs':[], 'next':None}).encode(); ctype='application/json'
    else:
        raise AssertionError(url)
    return ResultEnvelope(
        execution_plan_id=envelope.plan_id, request_id=envelope.request_id,
        attempt_id=envelope.attempt_id, run_source_plan_id=envelope.run_source_plan_id,
        source_id=envelope.source_id, binding_id=envelope.binding_id,
        binding_revision_id=envelope.binding_revision_id, adapter_id=envelope.adapter_id,
        adapter_version=envelope.adapter_version, strategy=envelope.strategy,
        execution_class=envelope.execution_class, requested_url=url, final_url=url,
        status_code=200, content_type=ctype, body=body, fetched_at='2026-09-11T12:00:00.000000Z',
        bytes_downloaded=len(body), duration_ms=1,
    ).finalize()


def test_s35_crawl_robots_cursor_and_restart_state_share_one_durable_spine(tmp_path, monkeypatch):
    db=Database(tmp_path/'s35.db')
    try:
        migrate_schema(db.conn, SCHEMA_VERSION)
        run_id, plan_id=_setup(db.conn)
        monkeypatch.setattr('jobscraper.pipeline.driver.build_adapter', lambda _id,_cfg: _CrawlerAdapter())
        monkeypatch.setattr('jobscraper.runtime.dispatch.execute_request', _fake_transport)
        assert execute_run(db.conn, run_id) == 'SUCCEEDED'
        rows=db.conn.execute("SELECT request_type,payload_json,status FROM scrape_requests WHERE run_id=? ORDER BY created_at,id", (run_id,)).fetchall()
        roles=[json.loads(r['payload_json'] or '{}').get('role') for r in rows if r['request_type']=='SOURCE_CRAWL']
        assert roles.count('ROBOTS') == 1
        assert roles.count('PAGE') == 3
        assert all(r['status']=='SUCCEEDED' for r in rows)
        cursor=db.conn.execute("SELECT * FROM crawl_cursors WHERE checkpoint_run_source_plan_id=?", (plan_id,)).fetchone()
        assert cursor is not None and cursor['binding_revision_id']=='bndrev'
        assert json.loads(cursor['state_json'])['url'].endswith('/p3')
        assert json.loads(cursor['guard_state_json'])['version'] == 1
        coverage=db.conn.execute("SELECT completion_state,terminal_enumeration_proven FROM enumeration_coverage WHERE run_source_plan_id=?", (plan_id,)).fetchone()
        assert tuple(coverage) == ('COMPLETE', 1)
    finally:
        db.close()


def _setup_second_run(conn):
    # A new run under the same pinned binding revision: identical plan shape
    # (same binding_id/binding_revision_id/adapter pins), a fresh seed PAGE
    # request, and no re-provisioning of source/binding identity.
    now = db_utc_now(conn)
    run_id, plan_ids = create_run(conn, profile_id=None, plans=[{
        'source_id': 'src', 'source_plan_group_id': 'grp', 'fallback_rank': 0,
        'binding_id': 'bnd', 'binding_revision_id': 'bndrev', 'adapter_id': 'fixture_crawler',
        'adapter_version': '1.0.0', 'adapter_api_version': '1', 'strategy': 'HTTP_HTML',
        'execution_class': 'HTTP', 'permission_profile_id': 'perm', 'permission_profile_revision': 1,
        'cursor_schema_version': 1,
        'crawl_policy_snapshot_json': {'max_pages': 4, 'max_requests': 8, 'max_bytes': 100000, 'max_runtime_s': 120, 'max_detail_requests': 4, 'max_depth': 2},
    }], now=now)
    plan_id = plan_ids[0]
    enqueue_request(conn, run_id=run_id, run_source_plan_id=plan_id, source_id='src', binding_id='bnd', request_type='SOURCE_CRAWL', target_identity='http://127.0.0.1:8765/p1', payload={'role': 'PAGE', 'target_reference': 'http://127.0.0.1:8765/p1'}, strategy='HTTP_HTML', execution_class='HTTP', now=now)
    return run_id, plan_id


def _recording_transport(monkeypatch, fetched):
    def _recording(envelope, policy):
        fetched.append(envelope.payload.url)
        return _fake_transport(envelope, policy)
    monkeypatch.setattr('jobscraper.pipeline.driver.build_adapter', lambda _id, _cfg: _CrawlerAdapter())
    monkeypatch.setattr('jobscraper.runtime.dispatch.execute_request', _recording)


def _crash_after_page_one(monkeypatch, fetched):
    # Simulated process death after page 1's fence committed but before the
    # page 2 continuation is claimed: cursor + guard + continuation are
    # durable, the generation stays open, and no outcome is recorded.
    import jobscraper.pipeline.driver as driver
    real_claim = driver.claim_next_request

    def dying_claim(*args, **kwargs):
        if len(fetched) >= 2:
            raise RuntimeError('simulated crash after page 1 commit')
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(driver, 'claim_next_request', dying_claim)
    return real_claim


def test_cross_run_resume_reuses_compatible_cursor_with_fresh_guard(tmp_path, monkeypatch):
    from jobscraper.acquisition.crawler.canonicalize import crawl_identity

    db = Database(tmp_path / 's35-crossrun.db')
    try:
        migrate_schema(db.conn, SCHEMA_VERSION)
        run1, plan1 = _setup(db.conn)
        fetched = []
        _recording_transport(monkeypatch, fetched)
        real_claim = _crash_after_page_one(monkeypatch, fetched)
        with pytest.raises(RuntimeError, match='simulated crash'):
            execute_run(db.conn, run1)
        import jobscraper.pipeline.driver as driver
        monkeypatch.setattr(driver, 'claim_next_request', real_claim)
        # Page 1 committed; policy (robots) precedes page work by design.
        assert fetched == ['http://127.0.0.1:8765/robots.txt', 'http://127.0.0.1:8765/p1']
        g1 = json.loads(db.conn.execute("SELECT guard_state_json FROM crawl_cursors").fetchone()[0])
        assert g1["seen_url_identities"] == [crawl_identity('http://127.0.0.1:8765/p1')]

        # A new run under compatible pins resumes the same cursor row without
        # refetching page 1, but restarts pagination-guard accounting.
        run2, plan2 = _setup_second_run(db.conn)
        assert execute_run(db.conn, run2) == 'SUCCEEDED'
        assert fetched == [
            'http://127.0.0.1:8765/robots.txt', 'http://127.0.0.1:8765/p1',
            'http://127.0.0.1:8765/robots.txt', 'http://127.0.0.1:8765/p2',
            'http://127.0.0.1:8765/p3',
        ]
        rows = db.conn.execute("SELECT * FROM crawl_cursors").fetchall()
        assert len(rows) == 1  # one compatible cursor shared across runs
        assert rows[0]['checkpoint_run_source_plan_id'] == plan2
        assert json.loads(rows[0]['state_json'])['url'].endswith('/p3')
        g2 = json.loads(rows[0]['guard_state_json'])
        assert g2["seen_url_identities"] == [crawl_identity('http://127.0.0.1:8765/p2')]
    finally:
        db.close()


def test_same_plan_restart_resumes_cursor_and_guard(tmp_path, monkeypatch):
    from jobscraper.acquisition.crawler.canonicalize import crawl_identity

    db = Database(tmp_path / 's35-restart.db')
    try:
        migrate_schema(db.conn, SCHEMA_VERSION)
        run_id, plan_id = _setup(db.conn)
        fetched = []
        _recording_transport(monkeypatch, fetched)
        real_claim = _crash_after_page_one(monkeypatch, fetched)
        with pytest.raises(RuntimeError, match='simulated crash'):
            execute_run(db.conn, run_id)
        import jobscraper.pipeline.driver as driver
        monkeypatch.setattr(driver, 'claim_next_request', real_claim)
        pending = db.conn.execute(
            "SELECT request_type, status, payload_json FROM scrape_requests WHERE run_source_plan_id=? ORDER BY created_at, id",
            (plan_id,)).fetchall()
        assert [(r["request_type"], r["status"]) for r in pending if r["request_type"] == "SOURCE_CRAWL" and json.loads(r["payload_json"] or '{}').get("role") == "PAGE"] == [("SOURCE_CRAWL", "SUCCEEDED"), ("SOURCE_CRAWL", "PENDING")]
        generations = db.conn.execute("SELECT finalized_at FROM enumeration_coverage WHERE run_source_plan_id=?", (plan_id,)).fetchall()
        assert len(generations) == 1 and generations[0]["finalized_at"] is None

        # The resumed pass plans page 2 from the durable cursor, never
        # refetches page 1, preserves the guard history across the restart,
        # and finishes the same coverage generation.
        assert execute_run(db.conn, run_id) == 'SUCCEEDED'
        assert fetched == [
            'http://127.0.0.1:8765/robots.txt', 'http://127.0.0.1:8765/p1',
            'http://127.0.0.1:8765/p2', 'http://127.0.0.1:8765/p3',
        ]
        rows = db.conn.execute("SELECT * FROM crawl_cursors").fetchall()
        assert len(rows) == 1
        assert rows[0]['checkpoint_run_source_plan_id'] == plan_id
        g2 = json.loads(rows[0]['guard_state_json'])
        assert g2["seen_url_identities"] == [
            crawl_identity('http://127.0.0.1:8765/p1'),
            crawl_identity('http://127.0.0.1:8765/p2'),
        ]
        generations = db.conn.execute(
            "SELECT completion_state, terminal_enumeration_proven FROM enumeration_coverage WHERE run_source_plan_id=?",
            (plan_id,)).fetchall()
        assert len(generations) == 1
        assert tuple(generations[0]) == ('COMPLETE', 1)
    finally:
        db.close()


def test_cursor_and_continuation_roll_back_together_when_cursor_commit_crashes(tmp_path, monkeypatch):
    db=Database(tmp_path/'s35-crash.db')
    try:
        migrate_schema(db.conn, SCHEMA_VERSION)
        run_id, plan_id=_setup(db.conn)
        monkeypatch.setattr('jobscraper.pipeline.driver.build_adapter', lambda _id,_cfg: _CrawlerAdapter())
        monkeypatch.setattr('jobscraper.runtime.dispatch.execute_request', _fake_transport)
        # Let robots policy commit, then fail at the first PAGE cursor checkpoint.
        import jobscraper.pipeline.driver as driver
        real_save=driver.save_crawl_cursor
        def crash_on_page(*args, **kwargs):
            raise RuntimeError('fault after cursor proposal before commit')
        monkeypatch.setattr(driver, 'save_crawl_cursor', crash_on_page)
        with pytest.raises(RuntimeError, match='fault after cursor proposal'):
            execute_run(db.conn, run_id)
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_cursors WHERE checkpoint_run_source_plan_id=?", (plan_id,)).fetchone()[0] == 0
        pages=db.conn.execute("SELECT COUNT(*) FROM scrape_requests WHERE run_source_plan_id=? AND request_type='SOURCE_CRAWL' AND payload_json LIKE '%\"role\":\"PAGE\"%'", (plan_id,)).fetchone()[0]
        assert pages == 1  # no continuation escaped the rolled-back fence
        # The in-flight page remains RUNNING for normal epoch/restart recovery;
        # S3.5 must not invent a terminal transition outside the failed fence.
        assert db.conn.execute("SELECT status FROM scrape_requests WHERE run_source_plan_id=? AND payload_json LIKE '%\"role\":\"PAGE\"%'", (plan_id,)).fetchone()[0] == 'RUNNING'
    finally:
        db.close()
