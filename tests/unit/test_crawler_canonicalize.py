import pytest

from jobscraper.acquisition.crawler.canonicalize import crawl_identity, crawl_url


def test_crawl_identity_reuses_existing_tracking_normalization():
    a = crawl_identity("HTTPS://Jobs.Example.test:443/jobs/42?utm_source=x&team=eng#top")
    b = crawl_identity("https://jobs.example.test/jobs/42?team=eng")
    assert a == b == "https://jobs.example.test/jobs/42?team=eng"


def test_crawl_url_resolves_relative_reference_against_existing_base():
    result = crawl_url("../jobs/2?fbclid=noise", base="https://jobs.example.test/careers/page/1")
    assert result.normalized == "https://jobs.example.test/careers/jobs/2"
    assert result.raw == "../jobs/2?fbclid=noise"


@pytest.mark.parametrize("value", ["ftp://jobs.example.test/jobs", "https://u:p@jobs.example.test/jobs"])
def test_crawl_url_refuses_non_http_or_embedded_credentials(value):
    with pytest.raises(ValueError):
        crawl_url(value)
