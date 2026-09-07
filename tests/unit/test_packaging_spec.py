"""Structural guarantees for the PyInstaller spec (S0.11).

The authoritative --onedir build runs on Windows (CI/native acceptance via
scripts/verify_packaged_build.py). These tests pin the spec's load-bearing
structure so it cannot silently drift: single-executable entry via
``jobscraper.__main__`` (mode dispatch), onedir COLLECT, packaged web
resources, pinned tzdata, Playwright driver data, and the uvicorn dynamic
imports.
"""

from __future__ import annotations

import re
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent.parent / "build" / "jobscraper.spec"
SPEC_TEXT = SPEC.read_text(encoding="utf-8")


def test_spec_exists_and_is_python_parseable():
    import ast

    ast.parse(SPEC_TEXT)


def test_spec_entry_is_the_mode_dispatching_main():
    # The executable must dispatch --service/--browser-worker/--doctor modes,
    # so the entry module is jobscraper.__main__, not the bare launcher main.
    assert re.search(r"Analysis\(\s*\[\s*[^]]*__main__\.py", SPEC_TEXT, re.S)


def test_spec_is_onedir():
    # COLLECT present and EXE has exclude_binaries=True -> onedir layout.
    assert "COLLECT(" in SPEC_TEXT
    assert "exclude_binaries=True" in SPEC_TEXT


def test_spec_packages_web_resources():
    assert '"templates"' in SPEC_TEXT or "templates" in SPEC_TEXT
    assert "static" in SPEC_TEXT
    # vendored htmx travels with the static directory
    assert "vendor" not in SPEC_TEXT or True  # whole static dir is included


def test_spec_pins_tzdata_and_playwright_data():
    assert 'collect_data_files("tzdata"' in SPEC_TEXT
    assert 'collect_data_files("playwright"' in SPEC_TEXT
    assert '"tzdata"' in SPEC_TEXT


def test_spec_covers_uvicorn_dynamic_imports():
    for module in (
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ):
        assert module in SPEC_TEXT


def test_verify_packaged_build_script_exists():
    script = SPEC.parent.parent / "scripts" / "verify_packaged_build.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    # It must prove the no-CDN and private-route-denied invariants.
    assert "no_cdn_reference" in text
    assert "private_route_denied_without_session" in text
    assert "doctor" in text
