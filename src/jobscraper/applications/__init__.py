"""Application tracking (S1.9).

Authority: 01 §43 (application workflow), PROD-06 (application
side-effect boundary: opening the direct application URL is supported;
automatic submission is not).

Application records are separate from canonical jobs — a job can be
listing_status=CLOSED while its application remains INTERVIEWING.
"""

from jobscraper.applications.applylink import best_application_url, open_apply_url
from jobscraper.applications.core import (
    ApplicationStatusError,
    create_application,
    list_applications,
    record_listing_closed_if_applicable,
    update_application,
)

__all__ = [
    "ApplicationStatusError",
    "best_application_url",
    "create_application",
    "list_applications",
    "open_apply_url",
    "record_listing_closed_if_applicable",
    "update_application",
]
