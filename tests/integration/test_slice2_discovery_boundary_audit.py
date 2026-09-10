"""S2.8/S2.9 audit: durable discovery boundary negatives and continuity.

The S2.8 acceptance suite proves the happy chain; this module pins the parts of
the same authority the audit found unpinned for the §12.1 first-probe path:

* **Hostile/invalid-content refusal** — a failed, challenge, login, rate-limited,
  shell, non-HTML, malformed or oversized probe is classified, refused and
  recorded, and MUST NOT yield a fingerprint or a route decision (02 §12.1
  "low-confidence ... MUST fall back to generic discovery rather than silently
  forcing a specialized adapter"; plan v0313 §S2.8 security negatives).
* **Security-policy evidence** — a budget denial is durable evidence
  (``DENIED:BODY_TOO_LARGE``) that neither parses nor trusts the body
  (04 §5.1, SEC-02/03).
* **Content cannot widen authority** — a hostile body naming a private address
  or a foreign ATS host still cannot construct a grant or widen an allowed
  host (04 §5.1, SEC-09).
* **Crash/restart continuity** — a probe orphaned mid-flight is reclaimed by
  the restart authority and *resumes from its durable identity*, never by
  carrying Python objects across the restart (02 §12.1; 03 §16/§18,
  RUN-07/RUN-09).
* **Probe history is not rewritten** — a later specialized binding cites the
  same Source and leaves the probe's request/attempt/evidence intact
  (02 §12.1 "later specialized bindings do not rewrite the probe history";
  plan §0 rule 3).

Authority: v0.3.1.3 02 §12.1/§12.2, 03 §16/§18/RUN-07/RUN-09,
04 §5.1 + SEC-02/03/09, 06 §72.7/§72.22; plan
``docs/plans/slice-2-worker-implementation-plan-v0313.md`` §S2.8 and §0
rules 3, 4, 6.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.pipeline.driver import source_policy
from jobscraper.runtime.claims import claim_next_request
from jobscraper.runtime.discovery import (
    DiscoveryError,
    execute_source_discovery,
    load_queued_discovery,
    queue_source_discovery,
)
from jobscraper.runtime.provisioning import (
    ProvisioningError,
    ensure_builtin_adapter_definition,
    provision_source_and_binding,
)
from jobscraper.runtime.recovery import recover_interrupted_requests

NOW = "2026-09-10T06:30:00.000000Z"
LATER = "2026-09-10T08:30:00.000000Z"
# RUN-07 retry backoff is 5s * attempt_count (cap 300s): a reclaimed probe is
# claimable again only after it elapses.
AFTER_BACKOFF = "2026-09-10T08:31:00.000000Z"

_GREENHOUSE_PAGE = (
    b"<!doctype html><html><head>"
    b"<script src='https://boards.greenhouse.io/embed/job_board?for=acme'></script>"
    b"<link rel='canonical' href='https://boards.greenhouse.io/acme'>"
    b"</head><body><div id='grnhse_app'></div></body></html>"
)
# A hostile careers page: it advertises a private-range address and a foreign
# ATS host, and carries an active payload.  None of that may become authority.
_HOSTILE_PAGE = (
    b"<!doctype html><html><head><title>Careers</title>"
    b"<script>fetch('http://10.255.255.5/admin');"
    b"location='http://169.254.169.254/latest/meta-data/';</script>"
    b"<link rel='canonical' href='http://10.255.255.5/careers'>"
    b"<script src='https://evil.example/steal.js'></script>"
    b"</head><body><h1>Careers</h1><p>Join our team today and grow.</p>"
    b"<a href='javascript:alert(1)'>apply</a></body></html>"
)
_JS_SHELL_PAGE = (
    b"<!doctype html><html><head><title>Careers</title></head>"
    b"<body><div id='root'></div><noscript>enable js</noscript></body></html>"
)
_LOGIN_PAGE = (
    b"<!doctype html><html><head><title>Sign in</title></head><body>"
    b"<form><input type='password' name='pw'></form></body></html>"
)

# path -> (status, content-type, body)
_ROUTES: dict[str, tuple[int, str, bytes]] = {
    "/careers/greenhouse": (200, "text/html; charset=utf-8", _GREENHOUSE_PAGE),
    "/careers/hostile": (200, "text/html; charset=utf-8", _HOSTILE_PAGE),
    "/careers/jsshell": (200, "text/html; charset=utf-8", _JS_SHELL_PAGE),
    "/careers/login": (200, "text/html; charset=utf-8", _LOGIN_PAGE),
    "/careers/binary": (200, "application/octet-stream", b"\x00\x01\x02\x03" * 64),
    "/careers/badjson": (200, "application/json", b"{not json at all"),
    "/careers/notfound": (404, "text/html; charset=utf-8", b"<p>gone</p>"),
    "/careers/challenge": (
        403,
        "text/html; charset=utf-8",
        b"<title>Just a moment</title><p>checking your browser</p>",
    ),
    "/careers/ratelimited": (429, "text/html; charset=utf-8", b"<p>slow down</p>"),
    "/careers/oversized": (200, "text/html; charset=utf-8", b"A" * 40_000),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    hits = 0
    paths: list[str] = []

    def do_GET(self):  # noqa: N802 - stdlib handler interface
        type(self).hits += 1
        path = self.path.split("?")[0]
        type(self).paths.append(path)
        entry = _ROUTES.get(path)
        if entry is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, ctype, body = entry
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    _Handler.hits = 0
    _Handler.paths = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _database(path):
    db = Database(path)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    db.conn.executescript(
        f"""
        INSERT INTO adapter_permission_profiles (id, display_name, created_at)
        VALUES ('perm-audit', 'audit', '{NOW}');
        INSERT INTO adapter_permission_profile_revisions
            (id, permission_profile_id, revision, policy_json, created_at)
        VALUES ('permrev-audit', 'perm-audit', 1, '{{}}', '{NOW}');
        """
    )
    db.conn.commit()
    return db


def _probe(db, server, path, *, max_bytes=None, now=NOW):
    kwargs = {"max_bytes": max_bytes} if max_bytes is not None else {}
    queued = queue_source_discovery(
        db.conn,
        display_name="Audit careers",
        entry_url=f"http://127.0.0.1:{server.server_address[1]}{path}",
        source_family="EMPLOYER_CAREERS",
        now=now,
        **kwargs,
    )
    outcome = execute_source_discovery(
        db.conn, queued, worker_id="audit", now=now
    )
    return queued, outcome


def _page_validity_kinds(db, request_id):
    return [
        row["ref"]
        for row in db.conn.execute(
            "SELECT ref FROM acquisition_evidence"
            " WHERE request_id = ? AND kind = 'PAGE_VALIDITY'",
            (request_id,),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Hostile / invalid content refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_class"),
    [
        ("/careers/notfound", "NOT_FOUND"),
        ("/careers/challenge", "CHALLENGE_PAGE"),
        ("/careers/ratelimited", "RATE_LIMITED"),
        ("/careers/login", "LOGIN_REQUIRED"),
        ("/careers/binary", "UNEXPECTED_CONTENT"),
        ("/careers/badjson", "UNEXPECTED_CONTENT"),
        ("/careers/jsshell", "JS_SHELL"),
    ],
)
def test_invalid_or_hostile_probes_are_refused_without_fingerprint_or_route(
    tmp_path, server, path, expected_class
):
    """Refusal is total: no fingerprint, no route, and no fabricated coverage.

    A page that is missing, challenged, gated, rate-limited, shell-only,
    non-HTML or malformed is *evidence about the attempt*, never a
    classification the router may act on.  The run finalizes PARTIAL rather
    than claiming a proven source (03 RUN-13 absence discipline).
    """
    db = _database(tmp_path / "refused.db")
    try:
        queued, outcome = _probe(db, server, path)

        assert outcome.page_class.value == expected_class
        assert outcome.fingerprint is None
        assert outcome.decision is None
        assert outcome.fingerprint_id is None
        assert outcome.route_decision_id is None
        assert outcome.run_status == "PARTIAL"

        # the refusal itself is durable, honest evidence about what was seen
        assert _page_validity_kinds(db, queued.request_id) == [
            f"validity://{expected_class}"
        ]
        assert db.conn.execute(
            "SELECT COUNT(*) FROM ats_fingerprints"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM source_route_decisions"
        ).fetchone()[0] == 0
        # classification is not enumeration: no coverage or observation exists
        assert db.conn.execute(
            "SELECT COUNT(*) FROM enumeration_coverage"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM job_observations"
        ).fetchone()[0] == 0
        request = db.conn.execute(
            "SELECT status, page_class FROM scrape_requests WHERE id = ?",
            (queued.request_id,),
        ).fetchone()
        assert request["page_class"] == expected_class
    finally:
        db.close()


def test_js_shell_is_refused_even_though_it_is_valid_html(tmp_path, server):
    """A JS-shell page is valid HTML but not a usable classification.

    ``JS_SHELL`` is outside the discovery-validity set, so an app shell cannot
    be fingerprinted as if it were the board — the honest answer is "not yet
    known", never a fabricated family (02 §12.1/§12.2).
    """
    db = _database(tmp_path / "shell.db")
    try:
        _, outcome = _probe(db, server, "/careers/jsshell")
        assert outcome.page_class.value == "JS_SHELL"
        assert outcome.fingerprint is None
        assert outcome.decision is None
    finally:
        db.close()


def test_oversized_probe_is_capped_and_denied_with_security_evidence(tmp_path, server):
    """A body over the budget is cut off and typed, never parsed or trusted.

    The denial is recorded as SECURITY_POLICY evidence under the request's own
    provenance (04 §5.1), and the run degrades instead of reporting a
    classification (plan §S2.8 "oversized body ... still denied").
    """
    db = _database(tmp_path / "oversized.db")
    try:
        queued, outcome = _probe(db, server, "/careers/oversized", max_bytes=1000)
        assert outcome.page_class.value == "UNKNOWN"
        assert outcome.fingerprint is None
        assert outcome.run_status == "PARTIAL"

        fetch = db.conn.execute(
            "SELECT * FROM fetch_attempts WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()
        assert fetch["failure_kind"] == "POLICY_REJECTED"
        failure = json.loads(fetch["failure_json"])
        assert failure["details_redacted"]["reason_code"] == "BODY_TOO_LARGE"
        assert failure["details_redacted"]["max_bytes"] == 1000

        security = db.conn.execute(
            "SELECT * FROM acquisition_evidence"
            " WHERE request_id = ? AND kind = 'SECURITY_POLICY'",
            (queued.request_id,),
        ).fetchall()
        assert [row["ref"] for row in security] == ["DENIED:BODY_TOO_LARGE"]
        assert json.loads(security[0]["detail_json"])["requested_url"].endswith(
            "/careers/oversized"
        )
        # the capped body was never classified as a usable page
        assert _page_validity_kinds(db, queued.request_id) == ["validity://UNKNOWN"]
    finally:
        db.close()


@pytest.mark.parametrize("bad_url", ["javascript:alert(1)", "file:///etc/passwd"])
def test_unusable_scheme_targets_never_become_a_probe(tmp_path, server, bad_url):
    """SEC-03 guard: an unusable target is refused before any durable work.

    A scheme with no host cannot name a Source, so nothing is committed — no
    Source, no binding, no request — and therefore no network authority is
    ever conferred on it (04 §5.1, plan §S2.8 "javascript: link ... denied").
    """
    db = _database(tmp_path / "scheme.db")
    try:
        with pytest.raises(ProvisioningError):
            queue_source_discovery(
                db.conn, display_name="bad target", entry_url=bad_url, now=NOW
            )
        for table in ("sources", "scrape_requests", "source_adapter_bindings"):
            assert db.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0
    finally:
        db.close()


def test_forbidden_scheme_is_denied_at_the_boundary_with_security_evidence(tmp_path):
    """A host-bearing non-HTTP scheme is denied by the acquisition boundary.

    The Source identity may be recorded (it is the operator's URL), but the
    HTTP execution class refuses the scheme before any connection: typed
    POLICY_REJECTED, durable ``SECURITY_POLICY`` evidence, no classification
    and no fingerprint (04 §5.1, SEC-02/03).
    """
    db = _database(tmp_path / "ftp.db")
    try:
        queued = queue_source_discovery(
            db.conn,
            display_name="ftp target",
            entry_url="ftp://example.com/careers",
            now=NOW,
        )
        outcome = execute_source_discovery(db.conn, queued, worker_id="audit", now=NOW)

        assert outcome.page_class.value == "UNKNOWN"
        assert outcome.fingerprint is None
        assert outcome.decision is None
        assert outcome.run_status == "PARTIAL"

        fetch = db.conn.execute(
            "SELECT * FROM fetch_attempts WHERE request_id = ?",
            (queued.request_id,),
        ).fetchone()
        assert fetch["failure_kind"] == "POLICY_REJECTED"
        failure = json.loads(fetch["failure_json"])
        assert failure["details_redacted"]["reason_code"] == "SCHEME_FORBIDDEN"
        security = db.conn.execute(
            "SELECT ref FROM acquisition_evidence"
            " WHERE request_id = ? AND kind = 'SECURITY_POLICY'",
            (queued.request_id,),
        ).fetchall()
        assert [row["ref"] for row in security] == ["DENIED:SCHEME_FORBIDDEN"]
        assert db.conn.execute(
            "SELECT COUNT(*) FROM ats_fingerprints"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_hostile_content_cannot_widen_network_authority(tmp_path, server):
    """A hostile body cannot create a grant or widen an allowed host.

    The page names a private address, a link-local metadata endpoint and a
    foreign script origin, and carries an active payload.  Authority is derived
    from the Source row alone (04 §5.1, SEC-09), so after the probe the host's
    own policy is unchanged, exactly one request went out, and nothing in the
    stored evidence executes (06 §72.50: no hostile content in the dashboard
    origin).
    """
    db = _database(tmp_path / "hostile.db")
    try:
        queued, outcome = _probe(db, server, "/careers/hostile")

        # the probe read what the page said, and nothing more
        assert outcome.page_class.value == "VALID_LIST"
        assert outcome.fingerprint is not None
        assert _Handler.paths == ["/careers/hostile"]

        source = db.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (queued.source_id,)
        ).fetchone()
        policy = source_policy(source, adapter_id="generic_discovery")
        # allowed hosts are host names; the loopback grant is the exact literal
        host = "127.0.0.1"
        assert policy.allowed_hosts == frozenset({host})
        # the hostile page's addresses and origins acquired no authority
        for forbidden in ("10.255.255.5", "169.254.169.254", "evil.example"):
            assert forbidden not in policy.allowed_hosts
        assert policy.internal_grant is not None
        assert policy.internal_grant.allowed_hosts == frozenset({host})
        assert "boards.greenhouse.io" not in policy.allowed_hosts

        # the hostile bytes are stored as inert evidence, not as markup to run
        evidence = db.conn.execute(
            "SELECT * FROM acquisition_evidence"
            " WHERE request_id = ? AND kind = 'RESULT_ENVELOPE'",
            (queued.request_id,),
        ).fetchone()
        assert evidence is not None
        detail = json.loads(evidence["detail_json"])
        assert detail["content_type"].startswith("text/html")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Crash / restart continuity
# ---------------------------------------------------------------------------


def test_crashed_probe_resumes_after_restart_reclamation(tmp_path, server):
    """A probe orphaned after its claim resumes from durable identity.

    The first "process" claims the request and dies before I/O.  The restart
    authority (03 §18, RUN-07) reclaims the orphaned ownership under the fresh
    epoch, and the new process rebuilds the continuation from the committed
    rows — no Python object is carried across the restart.  Exactly one network
    probe is performed in total.
    """
    path = tmp_path / "crashed.db"
    db = _database(path)
    url = f"http://127.0.0.1:{server.server_address[1]}/careers/greenhouse"
    queued = queue_source_discovery(
        db.conn, display_name="Crash careers", entry_url=url, now=NOW
    )
    request_id = queued.request_id
    run_id = queued.run_id

    # the doomed process starts the run and claims the request, then dies
    from jobscraper.runtime.runs import mark_run_started

    mark_run_started(db.conn, run_id, now=NOW)
    claim = claim_next_request(
        db.conn,
        "doomed-worker",
        now=NOW,
        types=frozenset({"SOURCE_DISCOVERY"}),
        run_source_plan_id=queued.run_source_plan_id,
    )
    assert claim is not None and claim.request_id == request_id
    assert _Handler.hits == 0
    assert db.conn.execute(
        "SELECT status FROM scrape_requests WHERE id = ?", (request_id,)
    ).fetchone()["status"] == "RUNNING"
    db.close()
    del queued

    reopened = Database(path)
    try:
        # a stale RUNNING request is not silently re-executed
        with pytest.raises(DiscoveryError):
            load_queued_discovery(reopened.conn, request_id=request_id)

        report = recover_interrupted_requests(reopened.conn, now=LATER)
        assert request_id in report["reclaimed"]

        resumed = load_queued_discovery(reopened.conn, run_id=run_id)
        assert resumed.request_id == request_id
        assert resumed.run_id == run_id
        # the reclaimed retry honours the RUN-07 backoff before it is claimable
        with pytest.raises(DiscoveryError):
            execute_source_discovery(
                reopened.conn, resumed, worker_id="eager", now=LATER
            )
        outcome = execute_source_discovery(
            reopened.conn, resumed, worker_id="restart-worker", now=AFTER_BACKOFF
        )
        assert _Handler.hits == 1
        assert outcome.fingerprint is not None
        assert outcome.fingerprint.family == "GREENHOUSE"
        assert outcome.run_status == "SUCCEEDED"

        attempts = reopened.conn.execute(
            "SELECT outcome FROM request_attempts WHERE request_id = ?"
            " ORDER BY started_at, attempt_id",
            (request_id,),
        ).fetchall()
        # the abandoned first attempt stays visible; the retry completed
        assert len(attempts) == 2
        assert attempts[0]["outcome"] == "ABANDONED"
        assert attempts[1]["outcome"] == "SUCCEEDED"
        assert reopened.conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts WHERE request_id = ?",
            (request_id,),
        ).fetchone()[0] == 1
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# Routing / provisioning consistency
# ---------------------------------------------------------------------------


def test_specialized_binding_never_rewrites_the_probe_history(tmp_path, server):
    """Specialization cites the probe's Source and appends no duplicate probe.

    02 §12.1: "later specialized bindings do not rewrite the probe history".
    The specialized binding lands on the *same* Source the probe used, the
    generic discovery binding/revision stays intact, and the probe's request,
    attempt and evidence rows are untouched (plan §0 rule 3, RUN-05).
    """
    db = _database(tmp_path / "specialize.db")
    try:
        queued, outcome = _probe(db, server, "/careers/greenhouse")
        assert outcome.decision is not None
        assert outcome.decision.outcome.value == "SPECIALIZED"

        def snapshot():
            return {
                "requests": db.conn.execute(
                    "SELECT COUNT(*) FROM scrape_requests"
                ).fetchone()[0],
                "attempts": db.conn.execute(
                    "SELECT COUNT(*) FROM request_attempts"
                ).fetchone()[0],
                "evidence": db.conn.execute(
                    "SELECT COUNT(*) FROM acquisition_evidence"
                ).fetchone()[0],
                "fetches": db.conn.execute(
                    "SELECT COUNT(*) FROM fetch_attempts"
                ).fetchone()[0],
                "fingerprints": db.conn.execute(
                    "SELECT COUNT(*) FROM ats_fingerprints"
                ).fetchone()[0],
                "routes": db.conn.execute(
                    "SELECT COUNT(*) FROM source_route_decisions"
                ).fetchone()[0],
            }

        before = snapshot()
        probe_request = db.conn.execute(
            "SELECT * FROM scrape_requests WHERE id = ?", (queued.request_id,)
        ).fetchone()
        probe_fingerprint = db.conn.execute(
            "SELECT * FROM ats_fingerprints WHERE id = ?", (outcome.fingerprint_id,)
        ).fetchone()
        probe_route = db.conn.execute(
            "SELECT * FROM source_route_decisions WHERE id = ?",
            (outcome.route_decision_id,),
        ).fetchone()

        definition = ensure_builtin_adapter_definition(db.conn, "greenhouse", now=LATER)
        provisioned = provision_source_and_binding(
            db.conn,
            display_name="Audit careers",
            source_family="EMPLOYER_CAREERS",
            entry_url=(
                f"http://127.0.0.1:{server.server_address[1]}/careers/greenhouse"
            ),
            canonical_host=None,
            adapter_id="greenhouse",
            adapter_version=definition.adapter_version,
            strategy="PROVIDER_NATIVE",
            execution_class="HTTP",
            config={
                "board": "acme",
                "api_base_url": f"http://127.0.0.1:{server.server_address[1]}",
                "company_name": "Acme Fixtures",
                "careers_url": "https://acme.example/careers",
            },
            now=LATER,
        )

        # same operator Source identity carries the specialization
        assert provisioned.source_id == queued.source_id
        # no new probe work or duplicated probe evidence was manufactured
        assert snapshot() == before
        assert db.conn.execute(
            "SELECT * FROM scrape_requests WHERE id = ?", (queued.request_id,)
        ).fetchone()["status"] == probe_request["status"]
        assert dict(
            db.conn.execute(
                "SELECT * FROM ats_fingerprints WHERE id = ?", (outcome.fingerprint_id,)
            ).fetchone()
        ) == dict(probe_fingerprint)
        assert dict(
            db.conn.execute(
                "SELECT * FROM source_route_decisions WHERE id = ?",
                (outcome.route_decision_id,),
            ).fetchone()
        ) == dict(probe_route)

        # both bindings coexist on the one Source: the probe binding is history,
        # the provider binding is the specialization the router chose
        bindings = {
            row["adapter_id"]: row["id"]
            for row in db.conn.execute(
                "SELECT b.id AS id, r.adapter_id AS adapter_id"
                " FROM source_adapter_bindings b"
                " JOIN source_adapter_binding_revisions r"
                "   ON r.id = b.current_revision_id"
                " WHERE b.source_id = ?",
                (queued.source_id,),
            ).fetchall()
        }
        assert bindings == {
            "generic_discovery": queued.binding_id,
            "greenhouse": provisioned.binding_id,
        }
        assert db.conn.execute(
            "SELECT COUNT(*) FROM source_adapter_bindings"
        ).fetchone()[0] == 2

        # the reviewed provider binding becomes the runnable authority, and the
        # probe's own binding/revision stays byte-intact as history
        current = {
            row["adapter_id"]: row["current_revision_id"]
            for row in db.conn.execute(
                "SELECT b.current_revision_id AS current_revision_id,"
                " r.adapter_id AS adapter_id"
                " FROM source_adapter_bindings b"
                " JOIN source_adapter_binding_revisions r"
                "   ON r.id = b.current_revision_id"
                " WHERE b.source_id = ?",
                (queued.source_id,),
            ).fetchall()
        }
        assert current == {
            "generic_discovery": queued.binding_revision_id,
            "greenhouse": provisioned.binding_revision_id,
        }
        probe_revision = db.conn.execute(
            "SELECT * FROM source_adapter_binding_revisions WHERE id = ?",
            (queued.binding_revision_id,),
        ).fetchone()
        assert probe_revision["adapter_id"] == "generic_discovery"
        assert probe_revision["strategy"] == "GENERIC_DISCOVERY"
        assert probe_revision["execution_class"] == "HTTP"
        # the probe's revision is current on two bindings at once only if the
        # specialization overwrote identity — it must not have
        assert db.conn.execute(
            "SELECT current_revision_id FROM source_adapter_bindings"
            " WHERE id = ?", (provisioned.binding_id,)
        ).fetchone()["current_revision_id"] == provisioned.binding_revision_id
    finally:
        db.close()


def test_requeueing_the_same_careers_url_reuses_the_source_identity(tmp_path, server):
    """Re-probing one careers URL never forks a second Source identity.

    The operator's source is the URL they added (06 §72.7); a second probe run
    for the same URL attaches to the existing Source and appends new request
    provenance instead of duplicating the source (plan §0 rule 4, RUN-05).
    """
    db = _database(tmp_path / "requeue.db")
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/careers/greenhouse"
        first = queue_source_discovery(
            db.conn, display_name="Audit careers", entry_url=url, now=NOW
        )
        second = queue_source_discovery(
            db.conn, display_name="Audit careers", entry_url=url, now=LATER
        )
        assert first.source_id == second.source_id
        assert first.request_id != second.request_id
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM source_adapter_bindings"
        ).fetchone()[0] == 1

        for queued, now in ((first, NOW), (second, LATER)):
            outcome = execute_source_discovery(
                db.conn, queued, worker_id="audit", now=now
            )
            assert outcome.fingerprint is not None
            assert outcome.fingerprint.family == "GREENHOUSE"

        assert _Handler.hits == 2
        assert db.conn.execute(
            "SELECT COUNT(*) FROM scrape_requests"
            " WHERE request_type = 'SOURCE_DISCOVERY'"
        ).fetchone()[0] == 2
        # each probe keeps its own attempt provenance on the shared source
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts"
        ).fetchone()[0] == 2
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    finally:
        db.close()
