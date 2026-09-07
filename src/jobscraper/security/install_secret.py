"""Per-install application secret.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md SEC-01;
plan S0.7 (choice 5).

The 32-byte random secret is protected with Windows user-scoped DPAPI and
stored under the ACL-hardened auth directory. No plaintext secret is
persisted. Rotation invalidates derived authority (sessions/bootstrap).
"""

from __future__ import annotations

import os
from pathlib import Path

from jobscraper.paths import AppPaths
from jobscraper.security.dpapi import (
    DPAPIError,
    protect_for_current_user,
    protector_kind,
    set_dev_auth_dir,
    unprotect_for_current_user,
)
from jobscraper.security.windows_acl import harden_auth_directory
from jobscraper.timeutil import utc_now_s

SECRET_FILE = "install-secret.bin"
ENTROPY = b"windows-job-scraper/install-secret/v1"


def _secret_path(paths: AppPaths) -> Path:
    return paths.auth / SECRET_FILE


def _load(paths: AppPaths) -> bytes | None:
    file = _secret_path(paths)
    if not file.is_file():
        return None
    blob = file.read_bytes()
    try:
        return unprotect_for_current_user(blob, entropy=ENTROPY)
    except DPAPIError:
        return None


def _store(paths: AppPaths, secret: bytes) -> None:
    harden_auth_directory(paths.auth)
    if protector_kind() != "DPAPI":
        set_dev_auth_dir(paths.auth)
    blob = protect_for_current_user(secret, entropy=ENTROPY)
    file = _secret_path(paths)
    file.write_bytes(blob)
    os.chmod(file, 0o600)


def load_or_create_install_secret(paths: AppPaths) -> bytes:
    """Load the install secret, creating it on first use."""
    import secrets

    paths.auth.mkdir(parents=True, exist_ok=True)
    existing = _load(paths)
    if existing is not None and len(existing) == 32:
        return existing
    secret = secrets.token_bytes(32)
    _store(paths, secret)
    return secret


def rotate_install_secret(paths: AppPaths) -> bytes:
    """Rotate the install secret (invalidates sessions/bootstrap authority)."""
    import secrets

    secret = secrets.token_bytes(32)
    _store(paths, secret)
    return secret


def secret_protected_at_rest(paths: AppPaths) -> bool:
    """True when the stored blob is not the plaintext secret."""
    file = _secret_path(paths)
    if not file.is_file():
        return False
    blob = file.read_bytes()
    loaded = _load(paths)
    return loaded is not None and blob != loaded and len(loaded) == 32
