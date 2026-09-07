"""Windows DPAPI protection for app-owned secret material.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md section 6;
docs/plans/slice-0-worker-implementation-plan-v0313.md S0.7 (choice 5) and
section 2 (choice 4: the pinned ``pywin32`` runtime dependency is the
authoritative Windows primitive for DPAPI — no custom security-sensitive
ctypes reimplementations).

On Windows, ``win32crypt.CryptProtectData``/``CryptUnprotectData`` (user
scope) protect the per-install secret; no plaintext secret is persisted. On
non-Windows development/test hosts a clearly-labelled development protector
(key file with owner-only permissions under the auth directory) keeps the same
interface so cross-platform tests can exercise the full auth flow. The
development protector is never used on Windows; the packaged Windows product
path is pywin32 DPAPI and is proven by the Windows CI suite and the native
Windows acceptance harness.
"""

from __future__ import annotations

import os
import secrets as _secrets
import sys
from pathlib import Path

W32 = sys.platform == "win32"


class DPAPIError(Exception):
    pass


if W32:  # pragma: no cover - exercised on Windows only (CI + native harness)
    import win32con
    import win32crypt

    def protect_for_current_user(plaintext: bytes, *, entropy: bytes | None = None) -> bytes:
        """DPAPI CryptProtectData with user scope (pywin32)."""
        try:
            blob = win32crypt.CryptProtectData(
                plaintext,
                "WindowsJobScraper/install-secret",
                entropy,
                None,
                None,
                win32con.CRYPTPROTECT_UI_FORBIDDEN,
            )
        except Exception as exc:  # pywintypes.error
            raise DPAPIError(f"CryptProtectData failed: {exc}") from exc
        return bytes(blob)

    def unprotect_for_current_user(ciphertext: bytes, *, entropy: bytes | None = None) -> bytes:
        """DPAPI CryptUnprotectData with user scope (pywin32)."""
        try:
            out = win32crypt.CryptUnprotectData(
                ciphertext, entropy, None, None, win32con.CRYPTPROTECT_UI_FORBIDDEN
            )
        except Exception as exc:  # pywintypes.error
            raise DPAPIError(f"CryptUnprotectData failed: {exc}") from exc
        # pywin32 returns the plaintext, or a (description, plaintext) tuple
        # depending on version; accept both shapes.
        if isinstance(out, tuple):
            out = out[-1]
        return bytes(out)

else:

    def protect_for_current_user(plaintext: bytes, *, entropy: bytes | None = None) -> bytes:
        """Development protector (non-Windows) — see module docstring."""
        raise NotImplementedError  # replaced below

    def unprotect_for_current_user(ciphertext: bytes, *, entropy: bytes | None = None) -> bytes:
        """Development protector (non-Windows) — see module docstring."""
        raise NotImplementedError  # replaced below


# Non-Windows development fallback implementation.
if not W32:
    import base64
    import hashlib
    import hmac

    _DEV_AUTH_DIR: list[Path | None] = [None]

    def _dev_key(auth_dir: Path) -> bytes:
        auth_dir.mkdir(parents=True, exist_ok=True)
        key_file = auth_dir / ".dev-protector-key"
        if not key_file.exists():
            key_file.write_bytes(_secrets.token_bytes(32))
            os.chmod(key_file, 0o600)
        return key_file.read_bytes()

    def _dev_cipher(key: bytes) -> tuple:
        # Simple XOR-stream with HMAC — adequate ONLY for development hosts.
        # It is explicitly not a substitute for DPAPI on the product build.
        return hashlib.sha256(key).digest(), hashlib.sha256(key + b"mac").digest()

    def protect_for_current_user(plaintext: bytes, *, entropy: bytes | None = None) -> bytes:  # type: ignore[no-redef]
        auth_dir = _DEV_AUTH_DIR[0]
        if auth_dir is None:
            raise DPAPIError("development protector requires an auth directory")
        enc_key, mac_key = _dev_cipher(_dev_key(Path(auth_dir)) + (entropy or b""))
        nonce = _secrets.token_bytes(16)
        stream = hashlib.sha256(enc_key + nonce).digest() * ((len(plaintext) // 32) + 1)
        ct = bytes(a ^ b for a, b in zip(plaintext, stream[: len(plaintext)]))
        mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        return base64.b64encode(b"DEV1" + nonce + mac + ct)

    def unprotect_for_current_user(ciphertext: bytes, *, entropy: bytes | None = None) -> bytes:  # type: ignore[no-redef]
        auth_dir = _DEV_AUTH_DIR[0]
        if auth_dir is None:
            raise DPAPIError("development protector requires an auth directory")
        try:
            raw = base64.b64decode(ciphertext)
        except Exception as exc:
            raise DPAPIError(f"corrupt protected blob: {exc}") from exc
        if not raw.startswith(b"DEV1"):
            raise DPAPIError("not a development-protected blob")
        nonce, mac, ct = raw[4:20], raw[20:52], raw[52:]
        enc_key, mac_key = _dev_cipher(_dev_key(Path(auth_dir)) + (entropy or b""))
        expected = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise DPAPIError("development protector MAC mismatch")
        stream = hashlib.sha256(enc_key + nonce).digest() * ((len(ct) // 32) + 1)
        return bytes(a ^ b for a, b in zip(ct, stream[: len(ct)]))

    def set_dev_auth_dir(path: Path) -> None:
        """Configure the directory backing the development protector."""
        _DEV_AUTH_DIR[0] = Path(path)


def protector_kind() -> str:
    """``DPAPI`` on Windows, ``DEV_FALLBACK`` on development hosts."""
    return "DPAPI" if W32 else "DEV_FALLBACK"
