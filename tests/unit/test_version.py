"""Unit tests for application version identity (S0.0)."""

from jobscraper.version import APP_NAME, APP_VERSION, SCHEMA_VERSION, SPEC_VERSION


def test_app_name():
    assert APP_NAME == "Windows Job Scraper"


def test_app_version_is_parseable_numeric_version():
    parts = APP_VERSION.split(".")
    assert len(parts) in (3, 4)
    assert all(p.isdigit() for p in parts)


def test_spec_version_matches_release():
    assert SPEC_VERSION == "v0.3.1.3"


def test_schema_version_positive_int():
    assert isinstance(SCHEMA_VERSION, int)
    assert SCHEMA_VERSION > 0
