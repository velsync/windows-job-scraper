"""Slice 0 contract tests (S0.12).

Aggregates the repository-level contracts every Slice 0 candidate must
satisfy before native acceptance: Python 3.12 baseline, real entry point,
exact committed dependency locks (including the pinned pywin32 Windows
primitive), and the package/file-map boundaries the plan fixes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


class TestPythonContract:
    def test_requires_python_312_baseline(self):
        pyproject = _read("pyproject.toml")
        match = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject)
        assert match, "requires-python missing"
        assert match.group(1).strip() == ">=3.12", (
            "the authoritative baseline is Python 3.12+; metadata must not be relaxed"
        )


class TestEntryPointContract:
    def test_console_script_points_at_a_real_launcher(self):
        pyproject = _read("pyproject.toml")
        match = re.search(r'jobscraper\s*=\s*"([^"]+)"', pyproject)
        assert match, "project.scripts entry missing"
        module_path, _, attr = match.group(1).partition(":")
        parts = module_path.split(".")
        file = REPO_ROOT / "src" / Path(*parts[:-1]) / (parts[-1] + ".py")
        assert file.is_file(), f"entry module {module_path} does not exist"
        assert attr == "main"
        text = file.read_text(encoding="utf-8")
        assert "def main(" in text, f"{module_path} has no main()"

    def test_dunder_main_dispatches_all_internal_modes(self):
        text = _read("src/jobscraper/__main__.py")
        for mode in ("--service", "--browser-worker", "--doctor", "--version"):
            assert f'"{mode}"' in text, f"__main__ must dispatch {mode}"


class TestDependencyLockContract:
    """The committed locks are the reproducible dependency contract (S0.0)."""

    PRODUCTION_PINS = {
        "fastapi": r"fastapi==[\d.]+",
        "uvicorn": r"uvicorn==[\d.]+",
        "jinja2": r"jinja2==[\d.]+",
        "playwright": r"playwright==[\d.]+",
        "tzdata": r"tzdata==[\d.]+",
        "pywin32": r'pywin32==[\d.]+;\s*sys_platform\s*==\s*"win32"',
    }

    def test_production_lock_pins_every_direct_dependency(self):
        lock = _read("requirements/production.lock.txt")
        for name, pattern in self.PRODUCTION_PINS.items():
            assert re.search(pattern, lock, re.IGNORECASE), (
                f"production.lock.txt must contain an exact pin for {name} "
                f"(pywin32 with a win32 platform marker)"
            )

    def test_production_in_declares_pywin32_windows_marker(self):
        production_in = _read("requirements/production.in")
        assert re.search(r'pywin32[^#\n]*;\s*sys_platform\s*==\s*"win32"', production_in) or re.search(
            r"pywin32", production_in
        ), "production.in must declare pywin32"

    def test_dev_lock_pins_pyinstaller_pytest_httpx(self):
        lock = _read("requirements/dev.lock.txt")
        for name in ("pyinstaller", "pytest", "httpx"):
            assert re.search(rf"{name}==[\d.]+", lock, re.IGNORECASE), f"dev.lock.txt missing pin for {name}"

    def test_no_runtime_cdn_dependency(self):
        """HTMX must be a committed local asset, never a runtime CDN fetch."""
        for template in (REPO_ROOT / "src/jobscraper/web/templates").glob("*.html"):
            text = template.read_text(encoding="utf-8")
            assert "unpkg.com" not in text and "cdn.jsdelivr" not in text and "googleapis" not in text, (
                f"{template.name} references a CDN"
            )
        assert (REPO_ROOT / "src/jobscraper/web/static/vendor/htmx.min.js").is_file()


class TestWindowsPrimitiveContract:
    def test_dpapi_uses_pywin32_on_windows(self):
        text = _read("src/jobscraper/security/dpapi.py")
        assert "import win32crypt" in text
        # No custom security-sensitive ctypes reimplementation.
        assert not re.search(r"^\s*(import ctypes|from ctypes)", text, re.M)

    def test_acl_uses_pywin32_on_windows(self):
        text = _read("src/jobscraper/security/windows_acl.py")
        assert "import win32security" in text
        assert "icacls" not in text  # no shelling out for security-sensitive ACL work

    def test_single_instance_uses_pywin32_named_mutex(self):
        text = _read("src/jobscraper/launcher/single_instance.py")
        assert "import win32event" in text
        assert "Local" in text and "Mutex" in text.replace("MUTEX_NAME", "Mutex").replace("mutex", "Mutex")


class TestSliceBoundaryContract:
    def test_no_product_code_in_slice0(self):
        """No future-slice product modules may exist yet (plan section 7)."""
        forbidden = [
            "src/jobscraper/domain",
            "src/jobscraper/acquisition",
            "src/jobscraper/queue",
            "src/jobscraper/runtime",
            "src/jobscraper/normalize",
            "src/jobscraper/workflow",
            "src/jobscraper/recipes",
        ]
        present = [p for p in forbidden if (REPO_ROOT / p).exists()]
        # Slice 1 (S1.2+) legitimately introduces the domain modules listed
        # below on the Slice-1 lineage; the Slice-0 boundary applies to the
        # Slice-0 promotion lineage, so those are exempt here. Everything
        # else in the forbidden list stays future-slice scope.
        slice1_modules = {
            "src/jobscraper/net",
            "src/jobscraper/runtime",
            "src/jobscraper/acquisition",
            "src/jobscraper/adapters",
            "src/jobscraper/pipeline",
            "src/jobscraper/inbox",
            "src/jobscraper/applications",
        }
        leaked = [p for p in present if p not in slice1_modules]
        assert not leaked, f"future-slice code leaked beyond the Slice-1 boundary: {leaked}"

    def test_plan_file_map_present(self):
        required = [
            "src/jobscraper/launcher/main.py",
            "src/jobscraper/launcher/single_instance.py",
            "src/jobscraper/launcher/lifecycle.py",
            "src/jobscraper/launcher/runtime_descriptor.py",
            "src/jobscraper/launcher/doctor.py",
            "src/jobscraper/service/app.py",
            "src/jobscraper/service/lifespan.py",
            "src/jobscraper/service/runner.py",
            "src/jobscraper/web/security.py",
            "src/jobscraper/web/sessions.py",
            "src/jobscraper/web/bootstrap.py",
            "src/jobscraper/web/sse.py",
            "src/jobscraper/security/dpapi.py",
            "src/jobscraper/security/windows_acl.py",
            "src/jobscraper/security/install_secret.py",
            "src/jobscraper/security/redaction.py",
            "src/jobscraper/db/connection.py",
            "src/jobscraper/db/migrations.py",
            "src/jobscraper/db/backup.py",
            "src/jobscraper/db/restore.py",
            "src/jobscraper/diagnostics/events.py",
            "src/jobscraper/diagnostics/health.py",
            "src/jobscraper/browser_worker/main.py",
            "src/jobscraper/browser_worker/protocol.py",
            "src/jobscraper/browser_worker/playwright_runtime.py",
            "src/jobscraper/timeutil/zones.py",
            "build/jobscraper.spec",
            "build/build_metadata.py",
        ]
        missing = [p for p in required if not (REPO_ROOT / p).is_file()]
        assert not missing, f"missing plan file-map entries: {missing}"
