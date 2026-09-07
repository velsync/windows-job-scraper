"""Integration tests for Doctor aggregation (S0.10).

Proves:
* a healthy initialized data root produces an actionable all-PASS report
  (browser-runtime WARN is environment-dependent, never an unexplained FAIL);
* a broken database produces an actionable FAIL;
* Doctor is diagnostic only — it never creates the database, never provisions
  the install secret, never deletes stale runtime markers (WIN-09);
* no secret material appears in any Doctor output;
* machine-readable JSON output and exit-code policy.
"""

from __future__ import annotations

import json

import pytest

from jobscraper.config import AppConfig
from jobscraper.diagnostics.health import CheckResult, doctor_exit_code, doctor_json
from jobscraper.launcher.doctor import run_doctor
from jobscraper.paths import build_app_paths, ensure_app_directories
from jobscraper.security.install_secret import load_or_create_install_secret


def _initialize_healthy_root(root) -> None:
    from jobscraper.db.backup import create_backup_generation
    from jobscraper.db.connection import Database
    from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema

    paths = build_app_paths(root)
    ensure_app_directories(paths)
    load_or_create_install_secret(paths)
    db = Database(paths.database_file)
    try:
        migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
        create_backup_generation(paths, db.conn)
    finally:
        db.close()


def test_doctor_healthy_root(tmp_path):
    root = tmp_path / "healthy"
    _initialize_healthy_root(root)
    results = run_doctor(AppConfig(data_root=root))
    by_name = {r.name: r for r in results}
    # Every mandatory Slice 0 check ran.
    expected = {
        "app_build",
        "data_root",
        "database",
        "backup",
        "security_config",
        "auth_storage",
        "stale_runtime_markers",
        "browser_worker",
        "free_disk",
        "timezone",
        "resource_manifest",
    }
    assert set(by_name) == expected
    # Healthy: no FAIL anywhere.
    failures = [r for r in results if r.status == "FAIL"]
    assert not failures, [(r.name, r.summary) for r in failures]
    # The initialized pieces are PASS.
    assert by_name["database"].status == "PASS"
    assert by_name["backup"].status == "PASS"
    assert by_name["auth_storage"].status == "PASS"
    assert by_name["timezone"].status == "PASS"
    assert by_name["timezone"].details["tzdata_version"] not in ("", "unknown")
    # Browser runtime is either PASS (runtime present) or an *explained* WARN
    # (runtime not installed yet) — never an unexplained FAIL (asserted above).
    assert by_name["browser_worker"].status in ("PASS", "WARN")


def test_doctor_broken_database_is_actionable(tmp_path):
    root = tmp_path / "broken"
    paths = build_app_paths(root)
    ensure_app_directories(paths)
    paths.db.mkdir(parents=True, exist_ok=True)
    paths.database_file.write_bytes(b"this is not a sqlite database" * 10)
    results = run_doctor(AppConfig(data_root=root))
    db_check = next(r for r in results if r.name == "database")
    assert db_check.status == "FAIL"
    assert db_check.summary  # actionable text


def test_doctor_does_not_mutate_fresh_root(tmp_path):
    """WIN-09: Doctor never creates the DB/secret and never deletes markers."""
    root = tmp_path / "fresh"
    paths = build_app_paths(root)
    ensure_app_directories(paths)
    # A stale runtime marker that must survive Doctor.
    (paths.runtime / "service_descriptor.json").write_text(
        '{"pid": 999999999, "service_instance_id": "svc-gone"}', encoding="utf-8"
    )
    results = run_doctor(AppConfig(data_root=root))
    # Nothing was created:
    assert not paths.database_file.exists()
    assert not (paths.auth / "install-secret.bin").exists()
    # The stale marker is still there (reported, not deleted):
    assert (paths.runtime / "service_descriptor.json").is_file()
    marker_check = next(r for r in results if r.name == "stale_runtime_markers")
    assert marker_check.status == "WARN"
    assert any("stale" in m or "corrupt" in m for m in marker_check.details.get("markers", []))


def test_doctor_output_contains_no_secret_material(tmp_path):
    root = tmp_path / "secret-scan"
    _initialize_healthy_root(root)
    secret = load_or_create_install_secret(build_app_paths(root))
    results = run_doctor(AppConfig(data_root=root))
    rendered = json.dumps(doctor_json(results), default=str)
    assert secret.hex() not in rendered
    assert repr(secret) not in rendered


def test_doctor_json_shape(tmp_path):
    root = tmp_path / "json"
    _initialize_healthy_root(root)
    results = run_doctor(AppConfig(data_root=root))
    payload = doctor_json(results)
    assert payload["schema_version"] == 1
    assert payload["exit_code"] == doctor_exit_code(results)
    assert all(c["status"] in ("PASS", "WARN", "FAIL") for c in payload["checks"])
    assert all("summary" in c and "name" in c for c in payload["checks"])


def test_doctor_exit_code_policy():
    ok = [CheckResult("a", "PASS", "s"), CheckResult("b", "WARN", "s")]
    assert doctor_exit_code(ok) == 0
    bad = ok + [CheckResult("c", "FAIL", "s")]
    assert doctor_exit_code(bad) == 1


def test_doctor_timezone_failure_is_fail(tmp_path, monkeypatch):
    import jobscraper.timeutil as timeutil

    def broken_zone(name):
        raise RuntimeError("no tzdata")

    monkeypatch.setattr(timeutil, "resolve_named_zone", broken_zone)
    root = tmp_path / "tz"
    _initialize_healthy_root(root)
    results = run_doctor(AppConfig(data_root=root))
    tz = next(r for r in results if r.name == "timezone")
    assert tz.status == "FAIL"
    assert tz.summary  # actionable
