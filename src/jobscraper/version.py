"""Application version identity.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md (S0.0).
"""

APP_NAME = "Windows Job Scraper"
APP_VERSION = "0.3.1.3"
SPEC_VERSION = "v0.3.1.3"

# Current database schema version. Forward-only migrations live in
# jobscraper.db.migrations and MUST equal this value at the newest step.
SCHEMA_VERSION = 12

# Machine-readable identity for service-instance epochs and descriptors.
PRODUCT_ID = "windows-job-scraper"
