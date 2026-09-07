"""Unit tests: content, salary and location normalization (module 01 §33)."""

from jobscraper.normalize import content as content_norm
from jobscraper.normalize import locations as loc
from jobscraper.normalize.salary import parse_salary


# ------------------------------------------------------------------ content
def test_html_to_text_strips_scripts_and_tags():
    html = (
        "<html><head><style>.a{color:red}</style><script>var x=1;</script></head>"
        "<body><noscript>enable js</noscript><h1>Senior Engineer</h1>"
        "<p>We use <b>Python</b> &amp; FastAPI.</p><!-- comment --></body></html>"
    )
    text = content_norm.html_to_text(html)
    assert "Senior Engineer" in text
    assert "Python" in text and "FastAPI" in text
    assert "var x" not in text
    assert "enable js" not in text
    assert "color:red" not in text
    assert "<" not in text


def test_html_to_markdown_keeps_structure():
    md = content_norm.html_to_markdown("<h2>Requirements</h2><ul><li>Python</li><li>Rust</li></ul>")
    assert "Requirements" in md
    assert "Python" in md and "Rust" in md


def test_detect_language_heuristics():
    assert content_norm.detect_language("软件工程师 负责平台开发")[0] == "zh"
    assert content_norm.detect_language("エンジニアを募集しています")[0] == "ja"
    assert content_norm.detect_language("We are hiring a senior engineer")[0] == "en"
    lang, conf = content_norm.detect_language("")
    assert lang == "unknown" and conf == 0.0


def test_content_hash_is_deterministic_and_truncated():
    h1 = content_norm.content_hash("T", "C", "x" * 50000)
    h2 = content_norm.content_hash("T", "C", "x" * 50000)
    h3 = content_norm.content_hash("T2", "C", "x" * 50000)
    assert h1 == h2 and h1 != h3
    assert len(h1) == 64  # sha256 hex


# ------------------------------------------------------------------- salary
def test_parse_salary_usd_annual():
    s = parse_salary("$120,000 - $140,000 per year")
    assert s and s.min == 120000 and s.max == 140000
    assert s.currency == "USD" and s.period == "YEAR"


def test_parse_salary_k_suffix_and_eur():
    s = parse_salary("€80k - €95k")
    assert s.min == 80000 and s.max == 95000
    assert s.currency == "EUR"
    lo, hi, ref = s.annualized()
    assert lo is not None and lo > 0
    assert "builtin" in (ref or "") or ref  # fx reference recorded


def test_parse_salary_hourly_annualizes():
    s = parse_salary("$50 - $60/hr")
    assert s.period == "HOUR"
    lo, hi, _ = s.annualized()
    assert lo == 50 * 2080 and hi == 60 * 2080


def test_parse_salary_monthly_usd():
    s = parse_salary("$4,500 per month")
    assert s.period == "MONTH"
    lo, hi, _ = s.annualized()
    assert lo == 4500 * 12  # USD: no conversion


def test_parse_salary_monthly_gbp_converts_to_usd():
    from jobscraper.normalize.salary import BUILTIN_FX_USD

    s = parse_salary("£4,500 per month")
    lo, hi, ref = s.annualized()
    assert lo == 4500 * 12 * BUILTIN_FX_USD["GBP"]


def test_competitive_salary_is_none_not_zero():
    assert parse_salary("Competitive salary") is None
    assert parse_salary("Salary negotiable") is None
    assert parse_salary("") is None


def test_unknown_currency_keeps_none_not_zero():
    s = parse_salary("50000 ZAR per year")
    assert s is not None
    lo, hi, ref = s.annualized()
    assert lo is None and hi is None  # unknown FX: stays unknown, never zero


def test_salary_parse_as_dict_has_no_zero_min():
    s = parse_salary("$100k")
    d = s.as_dict()
    assert d["min"] == 100000


# ---------------------------------------------------------------- locations
def test_parse_location_variants():
    assert loc.parse_location("Berlin, Germany")[0].country == "DE"
    assert loc.parse_location("London, UK")[0].country == "GB"
    assert loc.parse_location("New York, NY, USA")[0].country == "US"
    assert loc.parse_location("San Francisco, CA")[0].country == "US"
    assert loc.parse_location("Toronto, ON")[0].country == "CA"
    assert loc.parse_location("Remote")[0].remote is True
    assert loc.parse_location("Remote - US only")[0].country == "US"
    assert loc.parse_location("Remote (Germany)")[0].country == "DE"


def test_parse_location_multi():
    parsed = loc.parse_location("Amsterdam; Berlin or Paris")
    cities = {p.city for p in parsed}
    assert cities == {"Amsterdam", "Berlin", "Paris"}


def test_parse_location_hybrid_prefix():
    parsed = loc.parse_location("Hybrid — London")
    assert parsed[0].city == "London"


def test_normalize_locations_dedupe_and_cap():
    out = loc.normalize_locations("Berlin; Berlin; Remote", ["Berlin, Germany"])
    keys = [(o["country"], o["city"], o["remote"]) for o in out]
    assert len(keys) == len(set(keys))
    capped = loc.normalize_locations(None, [f"City{i}" for i in range(20)])
    assert len(capped) <= 12


def test_normalize_locations_remote_hint():
    out = loc.normalize_locations("Berlin, Germany", remote_hint=True)
    assert any(o["remote"] for o in out)
