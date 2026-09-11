from __future__ import annotations

from jobscraper.acquisition.crawler.sitemap import (
    SitemapDocumentKind,
    SitemapLimits,
    discover_sitemaps_from_robots,
    parse_sitemap,
    prioritized_candidates,
)


def _urlset(*rows: tuple[str, str | None]) -> bytes:
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, lastmod in rows:
        body.append("<url><loc>%s</loc>%s</url>" % (
            url,
            f"<lastmod>{lastmod}</lastmod>" if lastmod else "",
        ))
    body.append("</urlset>")
    return "".join(body).encode()


def _index(*urls: str) -> bytes:
    body = ['<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    body.extend(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
    body.append("</sitemapindex>")
    return "".join(body).encode()


def test_urlset_priority_prefers_career_scope_then_newer_lastmod():
    parsed = parse_sitemap(
        _urlset(
            ("https://example.test/about", "2026-09-11"),
            ("https://example.test/jobs/older", "2026-09-01"),
            ("https://example.test/careers/newer", "2026-09-10"),
        ),
        sitemap_url="https://example.test/sitemap.xml",
    )
    assert parsed.kind is SitemapDocumentKind.URLSET
    ranked = prioritized_candidates(parsed.candidates)
    assert [entry.url for entry, _priority in ranked] == [
        "https://example.test/careers/newer",
        "https://example.test/jobs/older",
        "https://example.test/about",
    ]
    assert ranked[0][1] > ranked[1][1] > ranked[2][1]
    assert parsed.as_dict()["absence_authority"] is False


def test_tracking_only_duplicate_urls_are_suppressed():
    parsed = parse_sitemap(
        _urlset(
            ("https://example.test/jobs/1?utm_source=a", None),
            ("https://example.test/jobs/1?utm_source=b", None),
        ),
        sitemap_url="https://example.test/sitemap.xml",
    )
    assert len(parsed.candidates) == 1
    assert any(d.code == "DUPLICATE_URL" for d in parsed.diagnostics)


def test_doctype_or_entity_is_refused_before_xml_parser():
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<urlset><url><loc>&xxe;</loc></url></urlset>"""
    parsed = parse_sitemap(payload, sitemap_url="https://example.test/sitemap.xml")
    assert parsed.kind is None
    assert [d.code for d in parsed.diagnostics] == ["UNSAFE_XML"]


def test_malformed_xml_is_typed_non_authoritative_diagnostic():
    parsed = parse_sitemap(
        b"<urlset><url><loc>https://example.test/jobs/1</loc></urlset>",
        sitemap_url="https://example.test/sitemap.xml",
    )
    assert parsed.kind is None
    assert parsed.diagnostics[0].code == "MALFORMED_XML"
    assert parsed.diagnostics[0].as_dict()["absence_authority"] is False


def test_oversized_document_is_bounded_before_parse():
    parsed = parse_sitemap(
        b"x" * 17,
        sitemap_url="https://example.test/sitemap.xml",
        limits=SitemapLimits(max_bytes=16, max_items=10, max_index_depth=2, max_loc_chars=100),
    )
    assert parsed.kind is None
    assert parsed.truncated is True
    assert parsed.diagnostics[0].code == "TOO_LARGE"


def test_sitemap_index_depth_bound_stops_further_expansion():
    parsed = parse_sitemap(
        _index("https://example.test/a.xml", "https://example.test/b.xml"),
        sitemap_url="https://example.test/root.xml",
        index_depth=2,
        limits=SitemapLimits(max_bytes=10000, max_items=10, max_index_depth=2, max_loc_chars=1000),
    )
    assert parsed.kind is SitemapDocumentKind.INDEX
    assert parsed.candidates == ()
    assert parsed.truncated is True
    assert parsed.diagnostics[0].code == "INDEX_DEPTH_LIMIT"


def test_robots_sitemap_discovery_is_bounded_deduped_and_absolute():
    found = discover_sitemaps_from_robots(
        [
            "User-agent: *",
            "Sitemap: https://example.test/jobs.xml",
            "sitemap: https://example.test/jobs.xml#fragment",
            "Sitemap: file:///tmp/nope.xml",
        ]
    )
    assert [c.url for c in found.candidates] == ["https://example.test/jobs.xml"]
    codes = {d.code for d in found.diagnostics}
    assert "DUPLICATE_URL" in codes
    assert "INVALID_LOC" in codes


def test_invalid_lastmod_is_preserved_only_as_diagnostic_evidence():
    parsed = parse_sitemap(
        _urlset(("https://example.test/jobs/1", "definitely-not-a-date")),
        sitemap_url="https://example.test/sitemap.xml",
    )
    assert parsed.candidates[0].lastmod == "definitely-not-a-date"
    assert any(d.code == "INVALID_LASTMOD" for d in parsed.diagnostics)
