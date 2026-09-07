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
    # SCHEMA_VERSION tracks the schema the code expects; after S0.2 the
    # Slice 0 baseline schema is version 2 (metadata + events). The stronger
    # agreement with the migration steps is asserted in test_migrations.
    from jobscraper.db.schema_sql import LATEST_SCHEMA_VERSION

    assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION >= 10
