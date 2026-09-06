import json

from build.build_metadata import collect_build_metadata


REQUIRED_KEYS = {
    "application_version",
    "browser_revision",
    "fastapi_version",
    "playwright_version",
    "pyinstaller_version",
    "python_version",
    "tzdata_version",
}


def test_collect_build_metadata_reports_only_explicit_version_facts(monkeypatch) -> None:
    secret_canary = "S0_SECRET_CANARY_7d2b8e24"
    monkeypatch.setenv("S0_SECRET_CANARY", secret_canary)

    metadata = collect_build_metadata()

    assert REQUIRED_KEYS <= metadata.keys()
    assert all(isinstance(value, str) and value for value in metadata.values())
    rendered = json.dumps(metadata, sort_keys=True)
    assert secret_canary not in rendered
    assert "S0_SECRET_CANARY" not in rendered
