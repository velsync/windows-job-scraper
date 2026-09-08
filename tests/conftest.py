"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make src/ importable when the package is not installed (dev hosts where the
# >=3.12 product contract prevents editable install on an older interpreter).
REPO_ROOT = Path(__file__).resolve().parent.parent
src_entry = str(REPO_ROOT / "src")
if src_entry not in sys.path:
    sys.path.insert(0, src_entry)

from jobscraper.config import AppConfig  # noqa: E402
from jobscraper.db.connection import Database  # noqa: E402
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema  # noqa: E402
from jobscraper.paths import build_app_paths, ensure_app_directories  # noqa: E402


@pytest.fixture()
def data_root(tmp_path) -> Path:
    root = tmp_path / "data-root"
    paths = build_app_paths(root)
    ensure_app_directories(paths)
    return root


@pytest.fixture()
def app_config(data_root) -> AppConfig:
    return AppConfig(data_root=data_root)


@pytest.fixture()
def db(data_root) -> Database:
    database = Database(build_app_paths(data_root).database_file)
    migrate_schema(database.conn, LATEST_SCHEMA_VERSION)
    yield database
    database.close()


# ------------------------------------------------- CI failure annotations
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Emit failing tests as GitHub workflow annotations under Actions.

    Raw run-log downloads are unavailable from some review environments;
    check-run annotations stay readable through the standard GitHub API.
    This only *reports* failures — it never changes test outcomes or
    runs outside CI (no-op unless GITHUB_ACTIONS=true).
    """
    import os

    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    failed = terminalreporter.stats.get("failed", []) or []
    for report in failed[:10]:  # GitHub caps annotations per step
        title = report.nodeid.replace("::", " / ")[:250] or "failed test"
        message = getattr(report, "longreprtext", "") or str(
            getattr(report, "longrepr", "")
        )
        message = (
            message.replace("\r", " ").replace("\n", " ").replace("::", "; ").strip()
        )
        # the decisive assertion lines sit at the end of the repr
        print(f"::error title={title}::{message[-380:]}", flush=True)
