"""Coverage regression for all ATS infrastructure hosts known to Slice 2."""

from jobscraper.acquisition.atsendpoints import ATS_ENDPOINT_SPECS
from jobscraper.pipeline.companies import CompanySignals


def test_every_known_ats_platform_host_is_excluded_from_company_host_identity():
    """A provider host shared by tenants can never be an employer-unique key."""
    for spec in ATS_ENDPOINT_SPECS:
        for host in spec.hosts():
            signals = CompanySignals(
                name=f"Tenant on {spec.provider}",
                application_host=host,
                careers_url=f"https://{host}/tenant",
            )
            keys = set(signals.identifier_keys())
            assert ("APP_HOST", host) not in keys, (spec.provider, host, keys)
            assert ("CAREERS_HOST", host) not in keys, (spec.provider, host, keys)
