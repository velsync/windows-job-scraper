"""Windows-native security tests (S0.7).

These run only on real Windows (windows-latest CI and the native acceptance
harness). They prove the authoritative Windows primitives:

* pywin32 DPAPI round-trip for the current user (no plaintext at rest);
* the auth directory receives a structurally verified app-owned ACL: current
  user + SYSTEM + Administrators only, protected from inheritance, no broad
  principal (Everyone/Authenticated Users/Users/Guests/Power Users) holds
  write authority — verified by reading the actual DACL, not by searching for
  the string "Everyone";
* the install secret survives reload and is DPAPI-protected.
"""

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-native security tests (windows-latest CI)"
)


def _windows():
    import win32security

    return win32security


def test_pywin32_primitives_importable():
    import win32con
    import win32crypt
    import win32event
    import win32file
    import win32process
    import win32security

    assert win32crypt is not None
    assert win32security is not None


def test_dpapi_roundtrip_current_user():
    from jobscraper.security.dpapi import (
        DPAPIError,
        protector_kind,
        protect_for_current_user,
        unprotect_for_current_user,
    )

    assert protector_kind() == "DPAPI"
    plaintext = b"windows-job-scraper-secret-probe"
    blob = protect_for_current_user(plaintext, entropy=b"probe-entropy")
    assert blob != plaintext
    assert plaintext not in blob
    assert unprotect_for_current_user(blob, entropy=b"probe-entropy") == plaintext
    with pytest.raises(DPAPIError):
        unprotect_for_current_user(blob, entropy=b"wrong-entropy")


def test_auth_directory_acl_structurally_app_owned(tmp_path):
    from jobscraper.security.windows_acl import (
        BROAD_PRINCIPALS,
        harden_auth_directory,
        inspect_auth_directory_acl,
    )

    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    harden_auth_directory(auth_dir)
    report = inspect_auth_directory_acl(auth_dir)
    assert report["ok"], report
    # The verification is structural: it read the actual DACL.
    assert report["protected_dacl"] is True
    assert isinstance(report["aces"], list) and report["aces"]
    allowed_sids = {a["sid"] for a in report["aces"] if a.get("type") == "ALLOW"}
    assert not allowed_sids & BROAD_PRINCIPALS
    # Current user retains required access on the hardened directory.
    probe = auth_dir / "probe.txt"
    probe.write_text("x", encoding="utf-8")
    assert probe.read_text() == "x"


def test_install_secret_dpapi_protected_at_rest(tmp_path):
    from jobscraper.paths import build_app_paths
    from jobscraper.security.install_secret import (
        load_or_create_install_secret,
        secret_protected_at_rest,
    )

    paths = build_app_paths(tmp_path / "root")
    paths.auth.mkdir(parents=True, exist_ok=True)
    secret = load_or_create_install_secret(paths)
    assert len(secret) == 32
    assert load_or_create_install_secret(paths) == secret
    assert secret_protected_at_rest(paths) is True
    blob = (paths.auth / "install-secret.bin").read_bytes()
    assert secret not in blob
