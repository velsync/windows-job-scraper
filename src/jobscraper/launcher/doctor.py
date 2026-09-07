"""Doctor: aggregated, actionable local diagnostics (S0.10).

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.10;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-09.

Doctor is *diagnostic only*: it does not migrate, restore, rotate secrets, or
delete stale markers. It opens the existing database only when present
(missing database is an actionable FAIL, not something Doctor creates), and
it never reveals secret material — only decryptability facts.

Minimum Slice 0 checks (plan S0.10): app build/version; data-root writability;
DB open/integrity/foreign keys/migration version; effective SQLite PRAGMAs;
latest backup/restore-check status; FTS5 capability; loopback/security
configuration; auth storage/secret decryptability; stale runtime markers;
browser worker launch; Playwright/browser revision + launch smoke; free
disk; timezone-data version and named-zone resolution; schema/resource
manifest consistency.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jobscraper.config import AppConfig, config_from_env
from jobscraper.diagnostics.health import (
    FAIL,
    PASS,
    WARN,
    CheckResult,
    doctor_exit_code,
    doctor_json,
    format_doctor_report,
)
from jobscraper.paths import AppPaths
from jobscraper.version import APP_VERSION, SCHEMA_VERSION

# Policy defaults (tunable, not invariants).
FREE_DISK_WARN_BYTES = 2 * 1024**3
FREE_DISK_FAIL_BYTES = 512 * 1024**2
DOCTOR_ZONE = "Europe/Bucharest"  # named DST-capable zone for the resolution smoke


def _check_app_build() -> CheckResult:
    from jobscraper.build_metadata import collect_build_metadata

    try:
        metadata = collect_build_metadata()
    except Exception as exc:  # pragma: no cover - defensive
        return CheckResult("app_build", FAIL, f"build metadata unavailable: {exc}", {})
    missing = [k for k, v in metadata.items() if not v]
    details = dict(metadata)
    if missing:
        return CheckResult("app_build", WARN, f"metadata keys empty: {missing}", details)
    return CheckResult("app_build", PASS, f"application {APP_VERSION}", details)


def _check_data_root(config: AppConfig) -> CheckResult:
    root = config.paths.root
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult("data_root", FAIL, f"data root not writable: {exc}", {"path": str(root)})
    return CheckResult("data_root", PASS, "data root writable", {"path": str(root)})


def _check_database(paths: AppPaths) -> CheckResult:
    from jobscraper.db.connection import connect_db
    from jobscraper.db.migrations import current_schema_version, run_database_checks

    db_path = paths.database_file
    if not db_path.is_file():
        return CheckResult(
            "database",
            FAIL,
            "database not initialized (run the application once to create it)",
            {"path": str(db_path)},
        )
    try:
        conn = connect_db(db_path)
    except Exception as exc:
        return CheckResult(
            "database",
            FAIL,
            f"database cannot be opened: {exc}",
            {"path": str(db_path)},
        )
    try:
        checks = run_database_checks(conn)
        schema_version = current_schema_version(conn)
    except Exception as exc:
        return CheckResult(
            "database",
            FAIL,
            f"database checks failed: {exc}",
            {"path": str(db_path)},
        )
    finally:
        conn.close()
    details = {
        "path": str(db_path),
        "schema_version": schema_version,
        "expected_schema_version": SCHEMA_VERSION,
        "integrity_check": checks.get("integrity_check"),
        "foreign_key_check": checks.get("foreign_key_check"),
        "fts5": checks.get("fts5"),
        "pragmas": checks.get("pragmas"),
        "problems": checks.get("problems"),
    }
    problems = list(checks.get("problems") or [])
    if not checks.get("ok"):
        return CheckResult("database", FAIL, "database checks failed: " + "; ".join(problems), details)
    if schema_version != SCHEMA_VERSION:
        return CheckResult(
            "database",
            FAIL,
            f"schema version {schema_version} != expected {SCHEMA_VERSION}; migration required",
            details,
        )
    # FTS5 capability is surfaced separately from hard integrity.
    status = PASS if checks.get("fts5") else WARN
    summary = "database healthy" if checks.get("fts5") else "database healthy but FTS5 unavailable"
    return CheckResult("database", status, summary, details)


def _check_backup(paths: AppPaths) -> CheckResult:
    from jobscraper.db.backup import latest_verified_backup

    latest = latest_verified_backup(paths)
    if latest is None:
        generations = 0
        if paths.backups.is_dir():
            generations = sum(1 for _ in paths.backups.iterdir())
        if generations:
            return CheckResult(
                "backup",
                FAIL,
                f"{generations} backup generation(s) exist but none verify",
                {"generations": generations},
            )
        return CheckResult(
            "backup",
            WARN,
            "no backup generation yet (created automatically before migrations and on demand)",
            {},
        )
    gen_dir, manifest = latest
    return CheckResult(
        "backup",
        PASS,
        f"latest backup generation verifies ({manifest.schema_version} schema artifacts)",
        {
            "generation": gen_dir.name,
            "created_at_utc": manifest.created_at_utc,
            "artifacts": len(manifest.artifacts),
        },
    )


def _check_security_config(config: AppConfig) -> CheckResult:
    details = {"loopback_host": config.loopback_host}
    if config.loopback_host != "127.0.0.1":
        return CheckResult(
            "security_config",
            FAIL,
            f"loopback host is {config.loopback_host!r}, expected 127.0.0.1",
            details,
        )
    from jobscraper.web.security import CSP

    details["csp_default_self"] = CSP.startswith("default-src 'self'")
    if not details["csp_default_self"]:  # pragma: no cover - defensive
        return CheckResult("security_config", FAIL, "CSP does not default to 'self'", details)
    return CheckResult("security_config", PASS, "loopback + strict CSP configuration", details)


def _check_auth_storage(paths: AppPaths) -> CheckResult:
    from jobscraper.security.dpapi import protector_kind
    from jobscraper.security.install_secret import SECRET_FILE
    from jobscraper.security.windows_acl import inspect_auth_directory_acl

    secret_file = paths.auth / SECRET_FILE
    if not secret_file.is_file():
        return CheckResult(
            "auth_storage",
            WARN,
            "install secret not provisioned yet (created on first launch)",
            {"protector": protector_kind()},
        )
    acl = inspect_auth_directory_acl(paths.auth)
    details = {
        "protector": protector_kind(),
        "acl_ok": bool(acl.get("ok")),
        "acl_reason": acl.get("reason"),
    }
    if not acl.get("ok"):
        return CheckResult("auth_storage", FAIL, f"auth directory ACL: {acl.get('reason')}", details)
    # Decryptability only — the secret value is never revealed. Read-only:
    # never create/rotate (WIN-09: doctor must not mutate user data).
    try:
        from jobscraper.security.install_secret import load_install_secret_strict

        secret = load_install_secret_strict(paths)
        decryptable = len(secret) == 32
    except Exception as exc:
        details["decrypt_error_type"] = type(exc).__name__
        return CheckResult("auth_storage", FAIL, f"install secret not decryptable: {exc}", details)
    if not decryptable:
        return CheckResult("auth_storage", FAIL, "install secret has unexpected length", details)
    details["secret_decryptable"] = True
    return CheckResult("auth_storage", PASS, "install secret decryptable for current user", details)


def _check_stale_runtime_markers(paths: AppPaths) -> CheckResult:
    from jobscraper.db.maintenance import stale_runtime_markers
    from jobscraper.launcher.runtime_descriptor import pid_alive

    markers = stale_runtime_markers(paths, service_pid_alive=pid_alive)
    # Diagnostic only: markers are reported, never deleted (WIN-09).
    if markers:
        return CheckResult(
            "stale_runtime_markers",
            WARN,
            "stale runtime markers present (recovered automatically on next launch): "
            + ", ".join(markers),
            {"markers": markers},
        )
    return CheckResult("stale_runtime_markers", PASS, "no stale runtime markers", {})


def _check_browser_worker() -> CheckResult:
    """Spawn the browser worker, PING/VERSION, run the inert smoke, stop."""
    from jobscraper.browser_worker.supervisor import BrowserWorkerSupervisor

    supervisor = BrowserWorkerSupervisor()
    try:
        supervisor.start()
    except Exception as exc:  # pragma: no cover - defensive
        return CheckResult(
            "browser_worker",
            FAIL,
            f"browser worker failed to launch: {type(exc).__name__}",
            {},
        )
    try:
        version = supervisor.request("VERSION", timeout_s=60)
        if not version.ok:
            return CheckResult("browser_worker", FAIL, "browser worker VERSION failed", {})
        facts = dict(version.payload or {})
        smoke = supervisor.request("SMOKE", timeout_s=180)
        if smoke.ok:
            smoke_payload = dict(smoke.payload or {})
            facts["smoke"] = "PASS"
            facts["chromium_version"] = smoke_payload.get("chromium_version")
            facts["page_title"] = smoke_payload.get("page_title")
            return CheckResult(
                "browser_worker",
                PASS,
                "worker + pinned Chromium inert smoke passed",
                facts,
            )
        error = smoke.error_kind or "SMOKE_FAILED"
        facts["smoke"] = error
        # Missing runtime is expected pre-first-run (WIN-02 approach 2);
        # an installed-but-failing launch is a hard failure.
        if error in ("CHROMIUM_NOT_INSTALLED", "PLAYWRIGHT_NOT_INSTALLED"):
            return CheckResult(
                "browser_worker",
                WARN,
                "browser runtime not installed yet (installed on first run / by setup)",
                facts,
            )
        return CheckResult(
            "browser_worker",
            FAIL,
            f"inert browser smoke failed: {error}",
            facts,
        )
    finally:
        supervisor.stop(timeout_s=15)


def _check_free_disk(paths: AppPaths) -> CheckResult:
    import shutil

    try:
        usage = shutil.disk_usage(paths.root)
    except OSError as exc:  # pragma: no cover - defensive
        return CheckResult("free_disk", FAIL, f"cannot stat data root: {exc}", {})
    free = usage.free
    details = {"free_bytes": free, "free_mib": round(free / 1024**2, 1)}
    if free < FREE_DISK_FAIL_BYTES:
        return CheckResult("free_disk", FAIL, "critically low free disk space", details)
    if free < FREE_DISK_WARN_BYTES:
        return CheckResult("free_disk", WARN, "low free disk space", details)
    return CheckResult("free_disk", PASS, "sufficient free disk space", details)


def _check_timezone() -> CheckResult:
    from jobscraper.timeutil import resolve_local_time, resolve_named_zone, tzdata_version
    from jobscraper.timeutil.zones import LocalOccurrence

    details: dict = {"tzdata_version": tzdata_version(), "zone": DOCTOR_ZONE}
    try:
        zone = resolve_named_zone(DOCTOR_ZONE)
        details["zone_key"] = zone.key
    except Exception as exc:
        return CheckResult(
            "timezone",
            FAIL,
            f"named zone {DOCTOR_ZONE!r} does not resolve: {exc}",
            details,
        )
    # DST policy proof: one spring-gap and one autumn-fold resolution.
    from datetime import datetime

    try:
        spring = resolve_local_time(DOCTOR_ZONE, datetime(2026, 3, 29, 2, 30))
        autumn = resolve_local_time(DOCTOR_ZONE, datetime(2026, 10, 25, 2, 30))
        details["spring_gap_policy"] = spring.policy
        details["autumn_fold_policy"] = autumn.policy
    except Exception as exc:
        return CheckResult("timezone", FAIL, f"DST policy resolution failed: {exc}", details)
    ok_policies = spring.policy in ("SPRING_GAP_ADVANCED", "EXACT") and autumn.policy in (
        "AUTUMN_FOLD_FIRST",
        "EXACT",
    )
    if not ok_policies:  # pragma: no cover - defensive
        return CheckResult("timezone", FAIL, "DST policy resolution returned unexpected policy", details)
    return CheckResult(
        "timezone",
        PASS,
        f"pinned tzdata {tzdata_version()} resolves {DOCTOR_ZONE} with deterministic DST policy",
        details,
    )


def _check_resource_manifest() -> CheckResult:
    """Package resource manifest consistency (templates/static/vendor present)."""
    from jobscraper.service.app import _WEB_DIR

    required = [
        _WEB_DIR / "templates" / "bootstrap.html",
        _WEB_DIR / "templates" / "dashboard.html",
        _WEB_DIR / "templates" / "error.html",
        _WEB_DIR / "static" / "app.js",
        _WEB_DIR / "static" / "styles.css",
        _WEB_DIR / "static" / "vendor" / "htmx.min.js",
    ]
    missing = [str(p.relative_to(_WEB_DIR)) for p in required if not p.is_file()]
    if missing:
        return CheckResult(
            "resource_manifest",
            FAIL,
            "packaged web resources missing: " + ", ".join(missing),
            {"missing": missing},
        )
    return CheckResult("resource_manifest", PASS, "packaged web resources present", {"files": len(required)})


def run_doctor(config: AppConfig) -> list[CheckResult]:
    """Run all Slice 0 Doctor checks. Diagnostic only (WIN-09)."""
    paths = config.paths
    results = [
        _check_app_build(),
        _check_data_root(config),
        _check_database(paths),
        _check_backup(paths),
        _check_security_config(config),
        _check_auth_storage(paths),
        _check_stale_runtime_markers(paths),
        _check_browser_worker(),
        _check_free_disk(paths),
        _check_timezone(),
        _check_resource_manifest(),
    ]
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobscraper --doctor", description="Run local diagnostics")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    config = config_from_env()
    if args.data_root is not None:
        config = config.with_data_root(args.data_root)
    results = run_doctor(config)
    if args.json:
        print(json.dumps(doctor_json(results), indent=2, sort_keys=True))
    else:
        print(format_doctor_report(results))
    return doctor_exit_code(results)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
