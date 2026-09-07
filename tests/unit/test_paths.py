"""Tests for application data-root ownership (S0.1)."""

from pathlib import Path

import pytest

from jobscraper.paths import (
    ALL_DIRS,
    DURABLE_DIRS,
    RUNTIME_DIRS,
    AppPaths,
    build_app_paths,
    default_data_root,
    ensure_app_directories,
)


def test_default_root_is_user_data_root_not_install_dir(tmp_path):
    root = default_data_root()
    assert root.name == "WindowsJobScraper"
    # Must not be the current working directory / repo / package tree.
    cwd = Path.cwd().resolve()
    assert root.resolve() != cwd
    assert cwd not in root.resolve().parents


def test_all_eight_subdirectories_derived_deterministically(tmp_path):
    p1 = build_app_paths(tmp_path)
    p2 = build_app_paths(tmp_path)
    assert p1 == p2
    for name in ALL_DIRS:
        assert getattr(p1, name) == tmp_path / name


def test_durable_and_runtime_dirs_are_disjoint_sets():
    assert set(DURABLE_DIRS).isdisjoint(RUNTIME_DIRS)
    assert set(ALL_DIRS) == set(DURABLE_DIRS) | set(RUNTIME_DIRS)


def test_ensure_directories_idempotent(tmp_path):
    paths = build_app_paths(tmp_path / "root")
    ensure_app_directories(paths)
    first = {name: getattr(paths, name).stat().st_ino for name in ALL_DIRS}
    ensure_app_directories(paths)
    second = {name: getattr(paths, name).stat().st_ino for name in ALL_DIRS}
    assert first == second


def test_injected_temp_root_isolates_tests(tmp_path):
    paths = build_app_paths(tmp_path / "isolated")
    ensure_app_directories(paths)
    assert paths.root.exists()
    # Nothing may be created under the real default root by these calls.
    default_root = default_data_root()
    if default_root.exists():
        assert default_root not in paths.root.parents


def test_database_file_location(tmp_path):
    paths = build_app_paths(tmp_path)
    assert paths.database_file == paths.db / "jobscraper.sqlite3"


def test_app_paths_frozen(tmp_path):
    paths = build_app_paths(tmp_path)
    with pytest.raises(Exception):
        paths.db = tmp_path  # type: ignore[misc]


def test_default_root_under_localappdata_on_windows():
    """WIN-06: on Windows the default root must be %LOCALAPPDATA%\\WindowsJobScraper."""
    import os
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows-only default-root assertion (runs on windows-latest CI)")
    local_app_data = os.environ["LOCALAPPDATA"]
    root = default_data_root()
    assert root == Path(local_app_data) / "WindowsJobScraper"
