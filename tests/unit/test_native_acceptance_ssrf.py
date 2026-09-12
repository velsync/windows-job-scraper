"""Regression coverage for the native harness's W1 SSRF assertions."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "native_acceptance.py"


def _load_native_acceptance_module():
    spec = importlib.util.spec_from_file_location("wjs_native_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE source_adapter_binding_revisions (
            id TEXT PRIMARY KEY,
            config_json TEXT NOT NULL
        );
        INSERT INTO source_adapter_binding_revisions (id, config_json)
        VALUES ('bndrev-2', '{}');

        CREATE TABLE scrape_requests (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            status TEXT NOT NULL,
            page_class TEXT,
            last_failure_kind TEXT,
            last_failure_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE request_attempts (
            attempt_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            outcome TEXT,
            failure_kind TEXT,
            started_at TEXT NOT NULL
        );
        CREATE TABLE fetch_attempts (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            failure_kind TEXT,
            failure_json TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE acquisition_evidence (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            attempt_id TEXT,
            kind TEXT NOT NULL,
            ref TEXT,
            detail_json TEXT NOT NULL,
            observed_at TEXT NOT NULL
        );
        CREATE TABLE job_observations (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL
        );
        """
    )
    return conn


def _harness(module, tmp_path: Path, monkeypatch):
    harness = module.Harness("dev", None, tmp_path / "evidence")
    harness._w1_state["profile_id"] = "profile-1"
    monkeypatch.setattr(harness, "_w1_seed_source", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(harness, "_w1_repoint_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        harness,
        "launch_and_get_url",
        lambda _data_root: (object(), "http://127.0.0.1:8765/#bootstrap=ticket"),
    )
    monkeypatch.setattr(harness, "stop_launcher", lambda _launcher: None)
    monkeypatch.setattr(harness, "_w1_bootstrap", lambda _url: {})
    monkeypatch.setattr(
        harness,
        "_w1_api",
        lambda *_args, **_kwargs: (200, {"status": "PARTIAL"}),
    )
    return harness


def test_w1_04_accepts_typed_pre_dispatch_rejection_without_fetch(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_native_acceptance_module()
    harness = _harness(module, tmp_path, monkeypatch)
    db = _database()
    try:
        db.execute(
            "INSERT INTO scrape_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "req-1",
                "src-2",
                "FAILED",
                None,
                "POLICY_REJECTED",
                json.dumps(
                    {
                        "reason": "CRAWL_SCOPE_HOST_NOT_ALLOWED",
                        "network_io_started": False,
                    }
                ),
                "2026-09-12T10:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO request_attempts VALUES (?, ?, ?, ?, ?)",
            (
                "attempt-1",
                "req-1",
                "FAILED",
                "POLICY_REJECTED",
                "2026-09-12T10:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO acquisition_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "evidence-1",
                "req-1",
                "attempt-1",
                "REVIEW",
                "crawler://SCOPE_DENIED",
                json.dumps(
                    {
                        "reason": "CRAWL_SCOPE_HOST_NOT_ALLOWED",
                        "network_io_started": False,
                    }
                ),
                "2026-09-12T10:00:00Z",
            ),
        )
        db.commit()

        harness._w1_04_private_destination_denied(tmp_path, 8765, db)

        result = harness.results["W1-04"]
        assert result["status"] == module.PASS
        assert result["details"]["request"]["status"] == "FAILED"
        assert result["details"]["fetch_count"] == 0
        assert result["details"]["observation_count"] == 0
    finally:
        db.close()


def test_w1_05_accepts_failed_typed_redirect_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_native_acceptance_module()
    harness = _harness(module, tmp_path, monkeypatch)
    db = _database()
    failure = json.dumps(
        {
            "kind": "POLICY_REJECTED",
            "details_redacted": {"reason_code": "HOST_NOT_ALLOWED"},
        }
    )
    try:
        db.execute(
            "INSERT INTO scrape_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "req-2",
                "src-2",
                "FAILED",
                "UNKNOWN",
                "POLICY_REJECTED",
                failure,
                "2026-09-12T10:01:00Z",
            ),
        )
        db.execute(
            "INSERT INTO request_attempts VALUES (?, ?, ?, ?, ?)",
            (
                "attempt-2",
                "req-2",
                "FAILED",
                "POLICY_REJECTED",
                "2026-09-12T10:01:00Z",
            ),
        )
        db.execute(
            "INSERT INTO fetch_attempts VALUES (?, ?, ?, ?, ?, ?)",
            (
                "fetch-2",
                "attempt-2",
                "req-2",
                "POLICY_REJECTED",
                failure,
                "2026-09-12T10:01:01Z",
            ),
        )
        db.commit()

        harness._w1_05_redirect_hop_denied(tmp_path, 8765, db)

        result = harness.results["W1-05"]
        assert result["status"] == module.PASS
        assert result["details"]["request_status"] == "FAILED"
        assert result["details"]["observation_count"] == 0

        # Evidence from different retries must never be combined into a pass.
        db.execute(
            "UPDATE fetch_attempts SET failure_kind = ?, failure_json = ?"
            " WHERE id = 'fetch-2'",
            ("HTTP_5XX", json.dumps({"kind": "HTTP_5XX"})),
        )
        db.execute(
            "INSERT INTO request_attempts VALUES (?, ?, ?, ?, ?)",
            (
                "attempt-1",
                "req-2",
                "FAILED",
                "HTTP_5XX",
                "2026-09-12T10:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO fetch_attempts VALUES (?, ?, ?, ?, ?, ?)",
            (
                "fetch-1",
                "attempt-1",
                "req-2",
                "POLICY_REJECTED",
                failure,
                "2026-09-12T10:02:00Z",
            ),
        )
        db.commit()

        harness._w1_05_redirect_hop_denied(tmp_path, 8765, db)

        assert harness.results["W1-05"]["status"] == module.FAIL
    finally:
        db.close()
