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
    unprotect_for_current_user,
)
from jobscraper.security.dpapi import W32 as _IS_WINDOWS

if not _IS_WINDOWS:
    from jobscraper.security.dpapi import set_dev_auth_dir

from jobscraper.security.windows_acl import harden_auth_directory

SECRET_FILE = "install-secret.bin"
ENTROPY = b"windows-job-scraper/install-secret/v1"


def _secret_path(paths: AppPaths) -> Path:
    return paths.auth / SECRET_FILE


def _prepare_protector(paths: AppPaths) -> None:
    """Bind the development protector to this auth directory (non-Windows).

    This must happen before *loading*, not only when storing: the service,
    launcher and tests are separate processes, and each must resolve the same
    development key file. On Windows the protector is DPAPI and needs no
    directory binding.
    """
    if not _IS_WINDOWS:
        set_dev_auth_dir(paths.auth)


def _load(paths: AppPaths) -> bytes | None:
    _prepare_protector(paths)
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
    _prepare_protector(paths)
    blob = protect_for_current_user(secret, entropy=ENTROPY)
    file = _secret_path(paths)
    file.write_bytes(blob)
    os.chmod(file, 0o600)


class InstallSecretError(RuntimeError):
    """Raised when the stored install secret exists but cannot be loaded."""


def load_install_secret_strict(paths: AppPaths) -> bytes:
    """Load the install secret without ever creating or rotating it.

    For read-only diagnostics (Doctor, WIN-09): unlike
    `load_or_create_install_secret`, a missing file or a blob that fails to
    decrypt is reported as an error instead of being silently replaced.
    """
    _prepare_protector(paths)
    file = _secret_path(paths)
    if not file.is_file():
        raise InstallSecretError("install secret file is missing")
    blob = file.read_bytes()
    try:
        secret = unprotect_for_current_user(blob, entropy=ENTROPY)
    except DPAPIError as exc:
        raise InstallSecretError(f"install secret not decryptable: {exc}") from exc
    if secret is None or len(secret) != 32:
        raise InstallSecretError("install secret has unexpected length")
    return secret


def load_or_create_install_secret(paths: AppPaths) -> bytes:
    """Load the install secret, creating it on first use.

    NOTE: a corrupt/undecryptable existing secret is replaced (rotated). This
    is correct for the owning service/launcher path but must NOT be used from
    read-only diagnostics; use `load_install_secret_strict` there.
    """
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
