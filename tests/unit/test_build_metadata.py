"""Tests for build metadata collection (S0.0).

Proves the required keys exist and no secret/environment value is dumped
wholesale.
"""

import os

from buildtools.build_metadata import build_id, collect_build_metadata

REQUIRED_KEYS = {
    "app_version",
    "spec_version",
    "schema_version",
    "python_version",
    "python_version_impl" if False else "python_implementation",
    "fastapi_version",
    "playwright_version",
    "pyinstaller_version",
    "tzdata_version",
    "expected_browser_revision",
    "installed_browser_path",
}


def test_required_keys_present():
    md = collect_build_metadata()
    assert REQUIRED_KEYS <= set(md.keys())


def test_no_environment_dump():
    md = collect_build_metadata()
    # No key may embed the full environment or obvious secret-bearing values.
    for key, value in md.items():
        assert isinstance(value, str)
        assert "os.environ" not in value
        for env_name, env_value in os.environ.items():
            if env_value and len(env_value) > 8 and env_value in value:
                if env_name not in {"PATH", "HOME", "LANG", "PWD", "OLDPWD"}:
                    raise AssertionError(f"environment value of {env_name} leaked into {key}")


def test_build_id_deterministic_and_short():
    md = collect_build_metadata()
    assert build_id(md) == build_id(dict(md))
    assert build_id(md).startswith("b-")
    assert len(build_id(md)) == 18
