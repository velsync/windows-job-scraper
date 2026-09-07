"""Single-instance ownership via a Windows named mutex.

Authority: docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-04;
plan S0.8 (choices 3-4: the mutex primitive is provided by the pinned pywin32
runtime dependency — win32event — rather than a custom ctypes call).

On Windows the mutex lives in the per-login-session ``Local\\`` namespace,
matching the single-user product model (each Windows user may run their own
instance against their own data root). On non-Windows development hosts an
advisory lock file provides the same interface so lifecycle logic is testable
on Linux CI; the packaged Windows product uses the named mutex.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

MUTEX_NAME = r"Local\WindowsJobScraper.SingleInstance"

if sys.platform == "win32":  # pragma: no cover - Windows native
    import win32api
    import win32event
    import winerror


class SingleInstanceOwnership:
    """Result of a single-instance acquisition attempt.

    ``already_running`` is True when another process owns the product
    lifecycle; in that case this process did NOT acquire ownership.
    """

    def __init__(self, *, already_running: bool, handle=None, lock_file=None) -> None:
        self.already_running = already_running
        self._handle = handle
        self._lock_file = lock_file
        self._released = False

    def release(self) -> None:
        """Release ownership (if held) so a clean shutdown lets the next
        launcher become the owner."""
        if self._released or self.already_running:
            return
        self._released = True
        if self._handle is not None:  # pragma: no cover - Windows native
            try:
                win32event.ReleaseMutex(self._handle)
            finally:
                self._handle.Close()
                self._handle = None
        if self._lock_file is not None:
            try:
                import os

                os.close(self._lock_file)
            except OSError:  # pragma: no cover
                pass
            self._lock_file = None
            try:
                Path(self._lock_path).unlink()
            except Exception:  # pragma: no cover
                pass

    _lock_path: Optional[str] = None


def acquire_single_instance(runtime_dir: Path) -> SingleInstanceOwnership:
    """Attempt to become the single product-lifecycle owner.

    Returns an ownership object; check ``already_running`` before proceeding.
    The ownership must be released on shutdown when held.
    """
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":  # pragma: no cover - Windows native
        # Request initial ownership when creating the mutex. The previous
        # implementation passed False, so it held a kernel handle without
        # owning the mutex and ReleaseMutex necessarily failed on shutdown.
        handle = win32event.CreateMutex(None, True, MUTEX_NAME)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            handle.Close()
            return SingleInstanceOwnership(already_running=True)
        return SingleInstanceOwnership(already_running=False, handle=handle)

    # Non-Windows development fallback: advisory lock file under the runtime
    # directory (per data root, mirroring the per-session mutex semantics).
    import fcntl
    import os

    lock_path = runtime_dir / "single-instance.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return SingleInstanceOwnership(already_running=True)
    ownership = SingleInstanceOwnership(already_running=False, lock_file=fd)
    ownership._lock_path = str(lock_path)
    return ownership
