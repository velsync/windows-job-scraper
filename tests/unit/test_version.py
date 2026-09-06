import re

from jobscraper.version import APP_NAME, APP_VERSION, SCHEMA_VERSION


SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def test_version_contract_is_initial_and_semantic() -> None:
    assert APP_NAME == "Windows Job Scraper"
    assert SEMVER_RE.fullmatch(APP_VERSION)
    assert SCHEMA_VERSION == 0
