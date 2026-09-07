"""Service + UI integration tests: security model, bootstrap, and the
Slice-1 ship-condition middle (launch -> profile -> collect -> Inbox ->
shortlist/dismiss -> apply -> tracking) over HTTP.
"""

import json
import time

import pytest
from starlette.testclient import TestClient

from jobscraper.acquisition.registry import AdapterRegistry  # noqa: F401 (wiring check)
from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.domain.sources import (
    create_binding,
    create_source,
    seed_builtin_permission_profile,
)
from jobscraper.security.netpolicy import make_test_fixture_policy
from jobscraper.service.app import create_service_app
from jobscraper.web.bootstrap import make_launcher_proof
from tests.fixtures.fixture_server import FixtureServer

PORT = 8437
BASE = f"http://127.0.0.1:{PORT}"
SECRET = b"test-install-secret-0123456789"

GREENHOUSE_JOBS = {
    "jobs": [
        {
            "id": 3001,
            "title": "Lead Python Engineer",
            "absolute_url": "https://job-boards.example/greenhouse/acme/jobs/3001",
            "updated_at": "2026-08-01T00:00:00Z",
            "first_published": "2026-07-15T00:00:00Z",
            "content": (
                "<div><p>Lead python engineer with fastapi and postgresql."
                " Full-time. Visa sponsorship available."
                " Berlin, Germany or Remote (Germany).</p>"
                "<script>alert('xss-attempt')</script>"
                "<a href=\"javascript:alert('proto-attempt')\">click</a></div>"
            ),
            "location": {"name": "Berlin, Germany"},
            "offices": [], "departments": [], "metadata": [],
        },
    ],
    "meta": {"total": 1},
}


@pytest.fixture()
def service(db, data_root):
    """Service app + client with a fixture-backed source configured."""
    with FixtureServer() as server:
        server.add_json("/v1/boards/acme/jobs", GREENHOUSE_JOBS)
        seed_builtin_permission_profile(db)
        source_id = create_source(
            db, display_name="Acme GH", entry_url=server.base_url + "/v1/boards/acme/jobs",
            canonical_host="127.0.0.1", source_family="greenhouse",
        )
        create_binding(
            db, source_id=source_id, display_name="GH", adapter_id="greenhouse",
            adapter_version="1.0.0", strategy="FEED_OR_PUBLIC_STRUCTURED_ENDPOINT",
            config={"board": "acme", "base_url": server.base_url},
        )
        app, state = create_service_app(
            AppConfig(data_root=data_root), db, port=PORT, secret=SECRET,
            policy_factory=lambda ctx: make_test_fixture_policy("127.0.0.1"),
        )
        with TestClient(app, base_url=BASE) as client:
            yield client, state, db
        state.shutdown()


def _launcher_proof_headers(state) -> dict:
    nonce = "nonce-test-1"
    issued_at = int(time.time())
    proof = make_launcher_proof(SECRET, state.instance_id, nonce, issued_at)
    return {"json": {
        "instance_id": state.instance_id, "nonce": nonce,
        "issued_at": issued_at, "proof": proof,
    }}


def _login(client, state) -> str:
    """Launcher proof -> ticket -> session exchange. Returns CSRF token."""
    origin = f"http://127.0.0.1:{state.port}"
    resp = client.post("/api/launcher/proof", **_launcher_proof_headers(state))
    assert resp.status_code == 200, resp.text
    ticket = resp.json()["ticket"]
    assert resp.json()["dashboard_url"].startswith(f"{origin}/#bootstrap=")
    resp = client.post(
        "/api/bootstrap/exchange",
        json={"ticket": ticket},
        headers={"Origin": origin},
    )
    assert resp.status_code == 200, resp.text
    csrf = resp.json()["csrf_token"]
    assert "wjs_session" in resp.cookies
    return csrf


def _mutation_headers(csrf: str, state) -> dict:
    return {
        "Origin": f"http://127.0.0.1:{state.port}",
        "X-CSRF-Token": csrf,
        "content-type": "application/json",
    }


# --------------------------------------------------------------- security
class TestSecurityModel:
    def test_liveness_public_and_minimal(self, service):
        client, state, db = service
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_security_headers_on_every_response(self, service):
        client, state, db = service
        resp = client.get("/healthz")
        assert resp.headers["content-security-policy"].startswith("default-src 'self'")
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["cache-control"] == "no-store"
        assert not any(k.lower().startswith("access-control-") for k in resp.headers)

    def test_wrong_host_rejected(self, service):
        client, state, db = service
        assert client.get("/healthz", headers={"Host": "localhost:8437"}).status_code == 400
        assert client.get("/healthz", headers={"Host": "evil.example:8437"}).status_code == 400
        assert client.get("/healthz", headers={"Host": "127.0.0.1:9999"}).status_code == 400

    def test_wrong_origin_rejected_on_reads(self, service):
        client, state, db = service
        resp = client.get("/healthz", headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403

    def test_private_reads_require_session(self, service):
        client, state, db = service
        assert client.get("/").status_code == 401
        assert client.get("/applications").status_code == 401
        assert client.get("/api/inbox?profile_id=x").status_code == 401

    def test_mutations_rejected_without_session(self, service):
        client, state, db = service
        resp = client.post(
            "/api/profiles", json={"name": "X"},
            headers={"Origin": BASE, "content-type": "application/json"},  # BASE matches the fixture service port
        )
        assert resp.status_code == 401

    def test_mutation_csrf_and_content_type_required(self, service):
        client, state, db = service
        csrf = _login(client, state)
        # No CSRF header.
        resp = client.post(
            "/api/profiles", json={"name": "X"},
            headers={"Origin": BASE, "content-type": "application/json"},  # BASE matches the fixture service port
        )
        assert resp.status_code == 403
        # Wrong CSRF token.
        resp = client.post(
            "/api/profiles", json={"name": "X"},
            headers={"Origin": BASE, "content-type": "application/json", "X-CSRF-Token": "bogus"},
        )
        assert resp.status_code == 403
        # Missing Origin.
        resp = client.post(
            "/api/profiles", json={"name": "X"},
            headers={"content-type": "application/json", "X-CSRF-Token": csrf},
        )
        assert resp.status_code == 403
        # Wrong content type.
        resp = client.post(
            "/api/profiles", content="name=X",
            headers={
                "Origin": BASE, "X-CSRF-Token": csrf,
                "content-type": "application/x-www-form-urlencoded",
            },
        )
        assert resp.status_code == 403
        # Correct: 200.
        resp = client.post(
            "/api/profiles", json={"name": "OK"},
            headers=_mutation_headers(csrf, state),
        )
        assert resp.status_code == 200, resp.text

    def test_oversized_body_rejected(self, service):
        client, state, db = service
        big = "x" * (2 * 1024 * 1024)
        resp = client.post(
            "/api/profiles", content=json.dumps({"name": big}).encode(),
            headers={"Origin": BASE, "content-type": "application/json"},  # BASE matches the fixture service port
        )
        assert resp.status_code == 413

    def test_launcher_proof_requires_valid_hmac(self, service):
        client, state, db = service
        issued_at = int(time.time())
        resp = client.post("/api/launcher/proof", json={
            "instance_id": state.instance_id, "nonce": "n1",
            "issued_at": issued_at, "proof": "0" * 64,
        })
        assert resp.status_code == 403
        # Replay of a used nonce is rejected.
        headers = _launcher_proof_headers(state)
        assert client.post("/api/launcher/proof", **headers).status_code == 200
        assert client.post("/api/launcher/proof", **headers).status_code == 403

    def test_bootstrap_ticket_single_use(self, service):
        client, state, db = service
        resp = client.post("/api/launcher/proof", **_launcher_proof_headers(state))
        ticket = resp.json()["ticket"]
        # Exchange requires exact Origin.
        bad = client.post("/api/bootstrap/exchange", json={"ticket": ticket})
        assert bad.status_code == 403
        ok = client.post(
            "/api/bootstrap/exchange", json={"ticket": ticket}, headers={"Origin": BASE}
        )
        assert ok.status_code == 200
        # Second exchange of the same ticket fails.
        again = client.post(
            "/api/bootstrap/exchange", json={"ticket": ticket}, headers={"Origin": BASE}
        )
        assert again.status_code == 403


# ------------------------------------------------------- full UI workflow
class TestShipConditionWorkflow:
    def test_launch_profile_collect_triage_apply_track(self, service):
        client, state, db = service
        csrf = _login(client, state)

        # -- Create profile over the API.
        resp = client.post(
            "/api/profiles",
            json={
                "name": "EU Engineering",
                "home_country": "de",
                "eligible_countries": "de, nl",
                "keywords": "python, fastapi",
                "must_keywords": "python",
                "min_score_inbox": 10,
            },
            headers=_mutation_headers(csrf, state),
        )
        assert resp.status_code == 200, resp.text
        profile_id = resp.json()["profile_id"]

        # -- Collect now (background run on the single executor).
        resp = client.post(
            "/api/runs", json={"profile_id": profile_id}, headers=_mutation_headers(csrf, state)
        )
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]
        for _ in range(120):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"):
                break
            time.sleep(0.25)
        assert run["status"] == "SUCCEEDED", run

        # -- Dashboard shows the job in the Inbox.
        resp = client.get("/", params={"profile_id": profile_id})
        assert resp.status_code == 200
        assert "Lead Python Engineer" in resp.text

        # -- JSON inbox read model.
        resp = client.get("/api/inbox", params={"profile_id": profile_id})
        items = resp.json()["items"]
        assert len(items) == 1
        job_id = items[0]["id"]
        assert items[0]["eligibility"] == "ELIGIBLE"
        assert items[0]["score"] is not None and items[0]["score"] >= 10

        # -- Job detail page: escaped description, no XSS, no javascript: links.
        resp = client.get(f"/jobs/{job_id}", params={"profile_id": profile_id})
        assert resp.status_code == 200
        assert "Lead Python Engineer" in resp.text
        # Layer 1: normalization strips script/anchor payloads entirely — the
        # malicious content never reaches the page in any form.
        assert "xss-attempt" not in resp.text
        assert "proto-attempt" not in resp.text
        assert "javascript:" not in resp.text
        # The only script element on the page is the local app.js.
        import re as _re

        scripts = _re.findall(r"<script[^>]*>", resp.text)
        assert scripts == ['<script src="/static/app.js" defer>']
        # Provenance links are http(s) with rel=noopener.
        assert 'rel="noopener noreferrer"' in resp.text
        assert "/jobs/3001" in resp.text

        # -- Shortlist (disposition).
        resp = client.post(
            f"/api/jobs/{job_id}/disposition",
            json={"profile_id": profile_id, "disposition": "SHORTLISTED",
                  "expected_row_revision": 1},
            headers=_mutation_headers(csrf, state),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["disposition"] == "SHORTLISTED"
        # Dashboard still shows it (SHORTLISTED stays in the queue).
        assert "Lead Python Engineer" in client.get("/", params={"profile_id": profile_id}).text

        # -- Track an application with the job's apply URL.
        resp = client.post(
            f"/api/jobs/{job_id}/applications",
            json={"profile_id": profile_id,
                  "applied_via_url": "https://job-boards.example/greenhouse/acme/jobs/3001"},
            headers=_mutation_headers(csrf, state),
        )
        assert resp.status_code == 200, resp.text
        application_id = resp.json()["application_id"]

        # Non-http(s) application URL rejected.
        resp = client.post(
            f"/api/jobs/{job_id}/applications",
            json={"profile_id": profile_id, "applied_via_url": "javascript:alert(1)"},
            headers=_mutation_headers(csrf, state),
        )
        assert resp.status_code == 400

        # -- Advance the application lifecycle.
        resp = client.post(
            f"/api/applications/{application_id}/status",
            json={"status": "APPLIED", "expected_row_revision": 1},
            headers=_mutation_headers(csrf, state),
        )
        assert resp.status_code == 200, resp.text
        # Invalid transition rejected with a typed 400.
        resp = client.post(
            f"/api/applications/{application_id}/status",
            json={"status": "ACCEPTED", "expected_row_revision": 2},
            headers=_mutation_headers(csrf, state),
        )
        assert resp.status_code == 400

        # -- Applications page shows the tracked application.
        resp = client.get("/applications", params={"profile_id": profile_id})
        assert resp.status_code == 200
        assert "Lead Python Engineer" in resp.text
        assert "APPLIED" in resp.text

        # -- Dismiss removes it from the Inbox (disposition filter).
        resp = client.post(
            f"/api/jobs/{job_id}/disposition",
            json={"profile_id": profile_id, "disposition": "DISMISSED",
                  "expected_row_revision": 2},
            headers=_mutation_headers(csrf, state),
        )
        assert resp.status_code == 200
        assert "Lead Python Engineer" not in client.get(
            "/", params={"profile_id": profile_id}
        ).text

    def test_profiles_page_rendes_create_form(self, service):
        client, state, db = service
        _login(client, state)
        resp = client.get("/profiles")
        assert resp.status_code == 200
        assert "Create profile" in resp.text
        assert 'data-api-form' in resp.text

    def test_run_without_sources_is_typed_400(self, db, data_root):
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
        app, state = create_service_app(
            AppConfig(data_root=data_root), db, port=8438, secret=SECRET,
        )
        try:
            with TestClient(app, base_url="http://127.0.0.1:8438") as client:
                csrf = _login(client, state)
                resp = client.post(
                    "/api/runs", json={}, headers=_mutation_headers(csrf, state)
                )
                assert resp.status_code == 400
                assert "no sources" in resp.json()["detail"]
        finally:
            state.shutdown()
