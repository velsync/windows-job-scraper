"""Child-process environment and lifecycle helpers.

When running from source (not a frozen build), child processes spawned by
the launcher/service/supervisor must be able to import ``jobscraper``. The
parent's ``sys.path`` is not inherited automatically (only
``PYTHONPATH`` is), so the package root is exported explicitly.

On Windows, CPython virtual environments use a small redirector executable
for ``Scripts\\python.exe``. If that redirector is passed to ``Popen``, the
PID owned by the parent can be the redirector rather than the interpreter
that actually runs the child. For lifecycle/fencing purposes we instead
spawn ``sys._base_executable`` and pass ``__PYVENV_LAUNCHER__`` so CPython
retains the virtual-environment identity and site-packages. This is the same
strategy used by CPython's Windows multiprocessing implementation.

Windows ``Popen.terminate()`` is a forced TerminateProcess operation, not a
graceful SIGTERM. Controlled service/launcher subprocesses therefore use a
new process group and receive ``CTRL_BREAK_EVENT`` for graceful shutdown.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def package_root() -> Path:
    """Directory containing the ``jobscraper`` package (src/ in a source
    checkout)."""
    return Path(__file__).resolve().parent.parent


def child_python_executable() -> str:
    """Executable for a directly-owned Python child process.

    Frozen builds re-invoke the packaged executable. Source-mode Windows venv
    runs bypass the redirector so ``Popen.pid`` is the actual interpreter PID.
    """
    if getattr(sys, "frozen", False):  # pragma: no cover - packaged build
        return sys.executable
    if sys.platform == "win32" and sys.prefix != sys.base_prefix:
        base = getattr(sys, "_base_executable", None)
        if base:
            return str(base)
    return sys.executable


def child_process_env(base: dict | None = None) -> dict:
    """Environment for child processes of this application."""
    env = dict(base if base is not None else os.environ)
    if getattr(sys, "frozen", False):  # pragma: no cover - packaged build
        return env

    root = str(package_root())
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if root not in parts:
        parts.insert(0, root)
    env["PYTHONPATH"] = os.pathsep.join(parts)

    # When spawning the base interpreter directly from a Windows venv, tell
    # CPython which venv launcher path should define sys.executable/prefix.
    if child_python_executable() != sys.executable:
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return env


def graceful_process_group_kwargs() -> dict[str, int]:
    """Popen kwargs required for a later graceful process-directed stop."""
    if sys.platform == "win32":  # pragma: no cover - Windows native
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def request_graceful_stop(proc: subprocess.Popen) -> None:
    """Request cooperative shutdown of an owned child process.

    Windows has no POSIX SIGTERM semantics in subprocess: ``terminate()`` is
    TerminateProcess. A child created in a new process group can instead be
    sent CTRL_BREAK_EVENT, which Python exposes as SIGBREAK to the child.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":  # pragma: no cover - Windows native
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()
