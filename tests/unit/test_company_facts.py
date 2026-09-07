"""Unit tests: company resolution and fact extraction (module 01 §33/34)."""

from jobscraper.normalize.company import domain_from_url, normalize_company_name, resolve_company
from jobscraper.normalize.facts import extract_facts


def test_normalize_company_name_strips_legal_suffixes():
    assert normalize_company_name("Acme Technologies, Inc.") == "acme technologies"
    assert normalize_company_name("Foo Bar GmbH") == "foo bar"
    assert normalize_company_name("  Baz Ltd.  ") == "baz"


def test_domain_from_url():
    assert domain_from_url("https://www.example.com/careers") == "example.com"
    assert domain_from_url("https://job-boards.greenhouse.io/acme") == "job-boards.greenhouse.io"
    assert domain_from_url("not a url") is None


def test_resolve_company_domain_match_is_strong(db):
    a = resolve_company(db, name="Acme Inc", domain="acme.com")
    b = resolve_company(db, name="Acme Incorporated", domain="acme.com")
    assert a == b  # same domain -> same company


def test_resolve_company_name_only_requires_missing_domain(db):
    a = resolve_company(db, name="Acme", domain=None)
    b = resolve_company(db, name="Acme", domain=None)
    assert a == b
    # A domain arriving for the same normalized name attaches to the
    # existing company (name match + no conflicting domain on either side).
    c = resolve_company(db, name="Acme", domain="other.com")
    assert c == a
    row = db.query_one("SELECT domain FROM companies WHERE id=?", (a,))
    assert row["domain"] == "other.com"
    # A DIFFERENT domain for the same name no longer merges: new company.
    d = resolve_company(db, name="Acme", domain="acme2.com")
    assert d != c


def test_extract_facts_seniority_employment_skills():
    text = (
        "We are hiring a Senior Staff Engineer. This is a full-time role. "
        "You will work with Python, Kubernetes and PostgreSQL. "
        "No visa sponsorship available. Remote within UTC-5 to UTC+3. German required."
    )
    facts = {f.fact_type: f for f in extract_facts("Senior Staff Engineer", text)}
    assert facts["seniority"].value == "senior"
    assert facts["employment_type"].value == "FULL_TIME"
    skills = facts["skills"].value
    assert "python" in skills and "kubernetes" in skills
    assert facts["work_authorization"].value.startswith("NO_SPONSORSHIP")
    assert facts["seniority"].evidence_start is not None and facts["seniority"].evidence_start >= 0
    assert facts["seniority"].rule_id


def test_extract_facts_evidence_offsets():
    title = "Engineer"
    text = "Senior role requiring Python skills."
    corpus = f"{title}\n{text}"
    facts = list(extract_facts(title, text))
    assert facts
    for f in facts:
        # The [start, end) span must be a real substring of the corpus.
        span = corpus[f.evidence_start : f.evidence_end]
        assert span and span in corpus
        # Evidence text contains the matched span (context window).
        assert span in f.evidence_text or f.evidence_text in span or span == f.evidence_text


def test_extract_facts_no_invention():
    facts = list(extract_facts("Role", "Nothing here."))
    by_type = {f.fact_type for f in facts}
    assert "work_authorization" not in by_type
    assert "skills" not in by_type or not [f for f in facts if f.fact_type == "skills"][0].value
