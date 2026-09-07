"""Application data-root ownership.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-06;
docs/plans/slice-0-worker-implementation-plan-v0313.md (S0.1).

Mutable application state lives under the Windows application-data root
(``%LOCALAPPDATA%\\WindowsJobScraper``), never beside the executable and never
inside the package/source tree.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_APP_DIR_NAME = "WindowsJobScraper"

# Directories that hold durable user data (participate in backups).
DURABLE_DIRS = ("db", "backups", "auth", "fixtures", "snapshots", "diagnostics")

# Directories that hold ephemeral runtime state (never restored from backup).
RUNTIME_DIRS = ("logs", "runtime")

ALL_DIRS = DURABLE_DIRS + RUNTIME_DIRS


@dataclass(frozen=True)
class AppPaths:
    """Resolved application data paths."""

    root: Path
    db: Path
    backups: Path
    auth: Path
    fixtures: Path
    snapshots: Path
    diagnostics: Path
    logs: Path
    runtime: Path

    @property
    def durable_dirs(self) -> tuple[Path, ...]:
        return tuple(getattr(self, name) for name in DURABLE_DIRS)

    @property
    def runtime_dirs(self) -> tuple[Path, ...]:
        return tuple(getattr(self, name) for name in RUNTIME_DIRS)

    @property
    def database_file(self) -> Path:
        return self.db / "jobscraper.sqlite3"


def default_data_root() -> Path:
    """Return the default application data root for this platform.

    On Windows this is ``%LOCALAPPDATA%\\WindowsJobScraper``. On non-Windows
    development/test hosts an equivalent user-local directory is used so the
    product never writes beside the executable or into the source tree.
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            # Fall back to the user profile rather than writing beside the exe.
            home = os.environ.get("USERPROFILE") or str(Path.home())
            local_app_data = str(Path(home) / "AppData" / "Local")
        return Path(local_app_data) / _APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / _APP_DIR_NAME
    return Path.home() / ".local" / "share" / _APP_DIR_NAME


def build_app_paths(root: Path) -> AppPaths:
    """Derive the deterministic directory layout under ``root``."""
    root = Path(root)
    return AppPaths(
        root=root,
        db=root / "db",
        backups=root / "backups",
        auth=root / "auth",
        fixtures=root / "fixtures",
        snapshots=root / "snapshots",
        diagnostics=root / "diagnostics",
        logs=root / "logs",
        runtime=root / "runtime",
    )


def ensure_app_directories(paths: AppPaths) -> None:
    """Idempotently create all application directories.

    The ``auth`` directory is created with owner-only permissions where the
    platform supports it (see jobscraper.security.windows_acl for the hardened
    Windows path).
    """
    for d in paths.durable_dirs + paths.runtime_dirs:
        d.mkdir(parents=True, exist_ok=True)
