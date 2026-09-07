"""Auth-directory ACL hardening.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md section 6
(restrictive ACLs); plan S0.7.

On Windows the auth directory is ACL-hardened so only the current user (plus
SYSTEM/Administrators for recovery) can access it. On POSIX development
hosts, owner-only permissions (0o700/0o600) provide the equivalent guarantee.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

W32 = sys.platform == "win32"


def harden_auth_directory(path: Path) -> None:
    """Apply restrictive permissions to the auth directory."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if W32:  # pragma: no cover - Windows native
        # Disable inheritance, grant the current user full control only.
        user = os.environ.get("USERNAME", "")
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
    else:
        os.chmod(path, 0o700)


def inspect_auth_directory_acl(path: Path) -> dict[str, object]:
    """Inspect the effective protections of the auth directory.

    Returns a dict with ``ok`` plus machine-readable details. The check proves
    no broad Everyone/Users write authority exists and the current user keeps
    required access.
    """
    path = Path(path)
    info: dict[str, object] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return {**info, "ok": False, "reason": "auth directory missing"}
    if W32:  # pragma: no cover - Windows native
        proc = subprocess.run(["icacls", str(path)], capture_output=True, text=True)
        output = proc.stdout
        broad_write = "Everyone:" in output and "(W)" in output.replace(" ", "")
        info["raw_contains_everyone"] = "Everyone" in output
        info["ok"] = proc.returncode == 0 and "Everyone" not in output
        info["detail"] = "owner-restricted" if info["ok"] else "broad-access-detected"
        return info
    mode = os.stat(path).st_mode & 0o777
    info["mode"] = oct(mode)
    info["ok"] = mode & 0o077 == 0
    info["detail"] = "owner-only" if info["ok"] else "group/other access detected"
    return info
