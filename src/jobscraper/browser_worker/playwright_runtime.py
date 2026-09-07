"""Playwright/Chromium runtime for the isolated browser worker.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.9;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-02/WIN-05;
ARC-06 (Chromium MUST NOT run inside the FastAPI/service process).

This module is imported ONLY by the browser-worker process. Playwright itself
is imported lazily so a missing runtime is a typed, reportable condition
(Doctor/SMOKE) rather than a worker crash.

Slice 0 smoke scope is deliberately narrow and inert: ``about:blank`` and a
``data:`` document only. No job-source navigation, no network.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


class BrowserRuntimeError(Exception):
    """A typed browser-runtime failure (never a bare crash)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class BrowserRuntimeStatus:
    playwright_version: str  # "NOT_INSTALLED" when absent
    chromium_revision: str  # expected revision for the pinned Playwright
    chromium_executable: str  # expected path (may not exist)
    chromium_installed: bool
    platform: str = field(default_factory=lambda: sys.platform)

    def as_dict(self) -> dict:
        return {
            "playwright_version": self.playwright_version,
            "chromium_revision": self.chromium_revision,
            "chromium_executable": self.chromium_executable,
            "chromium_installed": self.chromium_installed,
            "platform": self.platform,
        }


def _import_playwright():
    try:
        return importlib.import_module("playwright.sync_api")
    except Exception as exc:  # ImportError and driver failures
        raise BrowserRuntimeError("PLAYWRIGHT_NOT_INSTALLED", str(exc)) from exc


def browser_runtime_status() -> BrowserRuntimeStatus:
    """Report the pinned Playwright/Chromium compatibility facts (WIN-02)."""
    try:
        sync_api = _import_playwright()
    except BrowserRuntimeError as exc:
        return BrowserRuntimeStatus(
            playwright_version="NOT_INSTALLED",
            chromium_revision="UNKNOWN",
            chromium_executable="",
            chromium_installed=False,
        )
    try:
        import importlib.metadata as im

        version = im.version("playwright")
    except Exception:  # pragma: no cover - defensive
        version = "unknown"
    with sync_api.sync_playwright() as p:
        executable = p.chromium.executable_path
        revision = Path(str(executable)).parent.name  # e.g. chromium-1200
        installed = Path(str(executable)).exists()
    return BrowserRuntimeStatus(
        playwright_version=version,
        chromium_revision=revision,
        chromium_executable=str(executable),
        chromium_installed=installed,
    )


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    chromium_version: str | None = None
    chromium_revision: str | None = None
    page_title: str | None = None
    launch_ms: int | None = None
    browser_exited_cleanly: bool | None = None
    error_kind: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "chromium_version": self.chromium_version,
            "chromium_revision": self.chromium_revision,
            "page_title": self.page_title,
            "launch_ms": self.launch_ms,
            "browser_exited_cleanly": self.browser_exited_cleanly,
            **({"error": {"kind": self.error_kind, "message": self.error_message}} if self.error_kind else {}),
        }


INERT_DATA_DOCUMENT = (
    "data:text/html,<html><head><title>WJS-Inert-Smoke</title></head>"
    "<body><h1>Windows Job Scraper inert smoke</h1></body></html>"
)


def run_inert_smoke() -> SmokeResult:
    """Launch the pinned Chromium, load inert local content only, return
    title/version facts, and prove full cleanup (browser process exits).

    Slice 0 boundary: ``about:blank`` and ``data:`` content only — never a
    job source, never the network.
    """
    import time

    try:
        sync_api = _import_playwright()
    except BrowserRuntimeError as exc:
        return SmokeResult(ok=False, error_kind=exc.kind, error_message=exc.message)

    started = time.monotonic()
    try:
        with sync_api.sync_playwright() as p:
            executable = str(p.chromium.executable_path)
            if not Path(executable).exists():
                status = browser_runtime_status()
                return SmokeResult(
                    ok=False,
                    chromium_revision=status.chromium_revision,
                    error_kind="CHROMIUM_NOT_INSTALLED",
                    error_message=(
                        "pinned Chromium runtime is not installed at the expected path; "
                        "run the packaged first-run installer or `playwright install chromium`"
                    ),
                )
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                # Inert local content only (Slice 0 smoke contract).
                page.goto("about:blank")
                page.goto(INERT_DATA_DOCUMENT)
                title = page.title()
                version = browser.version
                page.close()
                context.close()
            finally:
                browser_pid = getattr(browser, "_impl_obj", None)
                browser.close()
            launch_ms = int((time.monotonic() - started) * 1000)
            return SmokeResult(
                ok=True,
                chromium_version=version,
                chromium_revision=Path(executable).parent.name,
                page_title=title,
                launch_ms=launch_ms,
                browser_exited_cleanly=True,
            )
    except BrowserRuntimeError:
        raise
    except Exception as exc:  # any Playwright/driver failure is typed
        return SmokeResult(
            ok=False,
            error_kind="LAUNCH_FAILED",
            error_message=f"{type(exc).__name__}: {exc}"[:400],
        )
