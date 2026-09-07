"""Windows DPAPI protection for app-owned secret material.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md section 6;
docs/plans/slice-0-worker-implementation-plan-v0313.md S0.7 (choice 5).

On Windows, ``CryptProtectData``/``CryptUnprotectData`` (user scope) protect
the per-install secret; no plaintext secret is persisted. On non-Windows
development/test hosts a clearly-labelled development protector (key file with
owner-only permissions under the auth directory) keeps the same interface so
cross-platform tests can exercise the full auth flow. The packaged Windows
product path is DPAPI and is verified by the native Windows acceptance
harness (WINDOWS_ACCEPTANCE_REQUIRED).
"""

from __future__ import annotations

import ctypes
import os
import secrets as _secrets
import sys
from ctypes import wintypes
from pathlib import Path

W32 = sys.platform == "win32"


class DPAPIError(Exception):
    pass


if W32:  # pragma: no cover - exercised on Windows only
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    def _blob_from_bytes(data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))

    def _bytes_from_blob(blob: "_DATA_BLOB") -> bytes:
        out = ctypes.string_at(blob.pbData, blob.cbData)
        return out

    _crypt32 = ctypes.WinDLL("Crypt32.dll")  # type: ignore[attr-defined]
    _kernel32 = ctypes.WinDLL("Kernel32.dll")  # type: ignore[attr-defined]

    def protect_for_current_user(plaintext: bytes, *, entropy: bytes | None = None) -> bytes:
        """DPAPI CryptProtectData with user scope."""
        in_blob = _blob_from_bytes(plaintext)
        ent_blob = _blob_from_bytes(entropy) if entropy else _DATA_BLOB(0, None)
        out_blob = _DATA_BLOB()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "jobscraper",
            ctypes.byref(ent_blob) if entropy else None,
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(out_blob),
        )
        if not ok:
            raise DPAPIError(f"CryptProtectData failed: {ctypes.GetLastError()}")
        try:
            return _bytes_from_blob(out_blob)
        finally:
            _kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))

    def unprotect_for_current_user(ciphertext: bytes, *, entropy: bytes | None = None) -> bytes:
        """DPAPI CryptUnprotectData with user scope."""
        in_blob = _blob_from_bytes(ciphertext)
        ent_blob = _blob_from_bytes(entropy) if entropy else _DATA_BLOB(0, None)
        out_blob = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob) if entropy else None,
            None,
            None,
            0x01,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise DPAPIError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
        try:
            return _bytes_from_blob(out_blob)
        finally:
            _kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))

else:

    def protect_for_current_user(plaintext: bytes, *, entropy: bytes | None = None) -> bytes:
        """Development protector (non-Windows) — replaced below."""
        raise NotImplementedError  # replaced below


# Non-Windows development fallback implementation.
if not W32:
    import base64
    import hashlib
    import hmac

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

    _DEV_AUTH_DIR: list[Path | None] = [None]

    def set_dev_auth_dir(path: Path) -> None:
        """Configure the directory backing the development protector."""
        _DEV_AUTH_DIR[0] = Path(path)


def protector_kind() -> str:
    return "DPAPI" if W32 else "DEV_FALLBACK"
