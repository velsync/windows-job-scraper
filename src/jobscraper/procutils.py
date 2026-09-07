"""Child-process environment helpers.

When running from source (not a frozen build), child processes spawned by
the launcher/service/supervisor must be able to import ``jobscraper``. The
parent's ``sys.path`` is not inherited automatically (only
``PYTHONPATH`` is), so the package root is exported explicitly. In a frozen
(PyInstaller) build the executable carries the package and no export is
needed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def package_root() -> Path:
    """Directory containing the ``jobscraper`` package (src/ in a source
    checkout)."""
    return Path(__file__).resolve().parent.parent


def child_process_env(base: dict | None = None) -> dict:
    """Environment for child processes of this application."""
    if getattr(sys, "frozen", False):  # pragma: no cover - packaged build
        return dict(base if base is not None else os.environ)
    env = dict(base if base is not None else os.environ)
    root = str(package_root())
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if root not in parts:
        parts.insert(0, root)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env
