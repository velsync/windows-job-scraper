"""Build metadata collection.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-03;
docs/plans/slice-0-worker-implementation-plan-v0313.md (S0.0).

Reports explicit package/version lookups only — never the environment or
secrets wholesale.
"""

from __future__ import annotations

import importlib.metadata as im
import platform
import sys
from typing import Mapping


def _package_version(name: str) -> str:
    try:
        return im.version(name)
    except im.PackageNotFoundError:
        return "NOT_INSTALLED"
    except Exception:  # pragma: no cover - defensive
        return "UNKNOWN"


def collect_build_metadata() -> dict[str, str]:
    """Collect deterministic build/dependency identity."""
    from jobscraper.version import APP_VERSION, SCHEMA_VERSION, SPEC_VERSION

    metadata: dict[str, str] = {
        "app_version": APP_VERSION,
        "spec_version": SPEC_VERSION,
        "schema_version": str(SCHEMA_VERSION),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "fastapi_version": _package_version("fastapi"),
        "uvicorn_version": _package_version("uvicorn"),
        "jinja2_version": _package_version("jinja2"),
        "playwright_version": _package_version("playwright"),
        "pyinstaller_version": _package_version("pyinstaller"),
        "tzdata_version": _package_version("tzdata"),
        "platform": sys.platform,
        "machine": platform.machine(),
    }
    browser = _chromium_revision()
    metadata["expected_browser_revision"] = browser[0]
    metadata["installed_browser_path"] = browser[1]
    return metadata


def _chromium_revision() -> tuple[str, str]:
    """Expected pinned Chromium revision and installed browser path.

    The exact Playwright/Chromium pair is pinned by the production lock; the
    installed browser revision/path is discovered from the Playwright package
    without launching anything. On hosts without the browser installed the
    path reports NOT_INSTALLED.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
            installed = "INSTALLED" if path else "NOT_INSTALLED"
            return _playwright_expected_chromium(), f"{installed}:{path}"
    except Exception:
        return _playwright_expected_chromium(), "NOT_INSTALLED"


def _playwright_expected_chromium() -> str:
    try:
        from playwright._repo_version import version as _pw_version  # type: ignore[attr-defined]
    except Exception:
        _pw_version = _package_version("playwright")
    return f"chromium-for-playwright-{_pw_version}"


def format_metadata_line(metadata: Mapping[str, str]) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(metadata.items()))


def build_id(metadata: Mapping[str, str] | None = None) -> str:
    """Deterministic short build identifier from collected metadata."""
    import hashlib
    import json

    md = dict(metadata or collect_build_metadata())
    payload = json.dumps(md, sort_keys=True).encode("utf-8")
    return "b-" + hashlib.sha256(payload).hexdigest()[:16]
