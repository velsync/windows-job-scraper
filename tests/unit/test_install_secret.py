"""Tests for the per-install secret protection (S0.7).

On non-Windows hosts the development protector exercises the same interface;
DPAPI behavior on Windows is covered by the native acceptance harness
(WINDOWS_ACCEPTANCE_REQUIRED).
"""

import pytest

from jobscraper.paths import build_app_paths, ensure_app_directories
from jobscraper.security import dpapi
from jobscraper.security.install_secret import (
    load_or_create_install_secret,
    rotate_install_secret,
    secret_protected_at_rest,
)
from jobscraper.security.windows_acl import harden_auth_directory, inspect_auth_directory_acl


def test_secret_created_once_and_stable(data_root):
    paths = build_app_paths(data_root)
    ensure_app_directories(paths)
    s1 = load_or_create_install_secret(paths)
    s2 = load_or_create_install_secret(paths)
    assert s1 == s2
    assert len(s1) == 32


def test_secret_not_plaintext_at_rest(data_root):
    paths = build_app_paths(data_root)
    ensure_app_directories(paths)
    secret = load_or_create_install_secret(paths)
    assert secret_protected_at_rest(paths) is True
    blob = (paths.auth / "install-secret.bin").read_bytes()
    assert secret not in blob
    assert blob != secret


def test_secret_rotation_changes_value(data_root):
    paths = build_app_paths(data_root)
    ensure_app_directories(paths)
    s1 = load_or_create_install_secret(paths)
    s2 = rotate_install_secret(paths)
    assert s1 != s2
    assert load_or_create_install_secret(paths) == s2


def test_auth_directory_acl_hardened(data_root):
    paths = build_app_paths(data_root)
    harden_auth_directory(paths.auth)
    report = inspect_auth_directory_acl(paths.auth)
    assert report["ok"], report
    # Owner retains required access.
    (paths.auth / "probe.txt").write_text("x", encoding="utf-8")
    assert (paths.auth / "probe.txt").read_text() == "x"


def test_protector_roundtrip_with_entropy(data_root):
    paths = build_app_paths(data_root)
    ensure_app_directories(paths)
    if dpapi.protector_kind() != "DPAPI":
        dpapi.set_dev_auth_dir(paths.auth)
    blob = dpapi.protect_for_current_user(b"material", entropy=b"e1")
    assert blob != b"material"
    assert dpapi.unprotect_for_current_user(blob, entropy=b"e1") == b"material"
    with pytest.raises(dpapi.DPAPIError):
        dpapi.unprotect_for_current_user(blob, entropy=b"e2")


def test_corrupt_secret_rejected_not_plaintext(data_root):
    paths = build_app_paths(data_root)
    ensure_app_directories(paths)
    load_or_create_install_secret(paths)
    # Corrupt the blob; load must fail closed (returns None internally) and a
    # fresh call re-creates rather than returning garbage.
    (paths.auth / "install-secret.bin").write_bytes(b"garbage")
    secret = load_or_create_install_secret(paths)
    assert len(secret) == 32
