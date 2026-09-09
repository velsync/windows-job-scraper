"""Deterministic content normalization (01 §34, §37).

raw/structured description → deterministic cleaning → Markdown → plain
text → language → hashes. Salary parsing preserves the original text and
never fabricates numbers: unknown salary stays NULL (never zero).

S2.3: the description cleaning step (Markdown/plain text/entities/link
scheme + tracking-parameter rules) lives in
``pipeline/contentclean.py`` (``content-clean-v1``); this module consumes
it so there is exactly one owner of the deterministic cleaner.  The
cleaning version is surfaced on ``NormalizedContent`` and recorded on the
canonical row (``jobs.content_cleaning_version``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from jobscraper.pipeline.contentclean import (
    CONTENT_CLEANING_VERSION,
    clean,
    detect_language,
)

#: Version of the deterministic normalization performed here.  Recorded on
#: every parse attempt and evaluation row (ARC-10, RUN-21).  The description
#: cleaning step is versioned separately (``CONTENT_CLEANING_VERSION``).
NORMALIZATION_VERSION = "normalize-v1"

_CURRENCY_PATTERNS = [
    ("EUR", re.compile(r"(€|\bEUR\b|\bEuros?\b)", re.IGNORECASE)),
    ("USD", re.compile(r"(\$|\bUSD\b|\bdollars?\b)", re.IGNORECASE)),
    ("GBP", re.compile(r"(£|\bGBP\b|\bpounds?\b)", re.IGNORECASE)),
    ("RON", re.compile(r"(\bRON\b|\blei\b)", re.IGNORECASE)),
]
_PERIOD_PATTERNS = [
    ("YEAR", re.compile(r"(per year|a year|annually|/year|/yr|per annum|jahr)", re.IGNORECASE)),
    ("MONTH", re.compile(r"(per month|a month|monthly|/month|/mo|lună|luna|monat)", re.IGNORECASE)),
    ("HOUR", re.compile(r"(per hour|an hour|hourly|/h\b|/hr)", re.IGNORECASE)),
]
_RANGE_RE = re.compile(
    r"(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:\.\d+)?)\s*(?:-|–|—|to|bis| până la )\s*"
    r"(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:\.\d+)?)"
    r"(\s*k\b)?",
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(
    r"(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:\.\d+)?)(\s*k\b)?", re.IGNORECASE
)


def _number(raw: str, k_suffix: bool) -> float | None:
    cleaned = raw.replace(" ", "").replace(",", ".").rstrip(".")
    # European format like 80.000 -> 80000 (thousands separator)
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if k_suffix:
        value *= 1000
    return value


def parse_salary(text: str):
    """Parse (min, max, currency, period) from salary text; unknowns are None.

    The original text is always preserved by the caller; this never
    fabricates numbers (01 §37: unknown salary is a distinct state, never
    numeric zero)."""
    if not text:
        return None, None, None, None
    currency = next((code for code, pattern in _CURRENCY_PATTERNS if pattern.search(text)), None)
    period = next((code for code, pattern in _PERIOD_PATTERNS if pattern.search(text)), None)
    match = _RANGE_RE.search(text)
    if match:
        low = _number(match.group(1), bool(match.group(3)))
        high = _number(match.group(2), bool(match.group(3)))
        if low is not None and high is not None and low <= high:
            return low, high, currency, period
    match = _SINGLE_RE.search(text)
    if match:
        value = _number(match.group(1), bool(match.group(2)))
        if value is not None:
            return value, value, currency, period
    return None, None, currency, period


@dataclass(frozen=True)
class NormalizedContent:
    title: str
    normalized_title: str
    company_name: str | None
    normalized_company: str | None
    description_md: str | None
    description_text: str | None
    description_lang: str | None
    description_hash: str | None
    salary_original_text: str | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    posted_at: str | None
    locations: tuple
    # ---- Slice 2 S2.2 (01 §33.1/§33.2): structured location set + the two
    # categorical fields the canonical projection presents.
    location_records: tuple = ()
    remote_mode: str = "UNSPECIFIED"
    remote_worldwide: int = 0
    employment_type: str | None = None
    experience_level: str | None = None
    # §33.1 company identity signals, surfaced by normalization so resolution
    # reads normalized evidence rather than re-reading raw fields
    careers_url: str | None = None
    organization_domains: tuple[str, ...] = ()
    company_country: str | None = None
    # S2.3: which cleaner revision produced the description projection (None
    # when the observation carried no description to clean)
    content_cleaning_version: str | None = None

    @property
    def content_hash(self) -> str:
        basis = json_dump(
            {
                "title": self.title,
                "company": self.normalized_company,
                "description": self.description_hash,
            }
        )
        return hashlib.sha256(basis.encode()).hexdigest()


def json_dump(value) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def normalize_title(title: str) -> str:
    lowered = (title or "").lower().strip()
    lowered = re.sub(r"[^a-z0-9äöüßăâîșț +#./-]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def normalize_company(name: str) -> str:
    lowered = (name or "").lower().strip()
    lowered = re.sub(
        r"\b(gmbh|ag|srl|sa|inc|llc|ltd|limited|bv|nv|oy|ab|as|plc|corp|co|company)\b",
        "",
        lowered,
    )
    lowered = re.sub(r"[^a-z0-9ăâîșțäöüß&]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


#: Categorical employment types the product distinguishes (01 §33).  Anything
#: the source words differently stays None: an unlisted value is never coerced
#: into a listed one.
EMPLOYMENT_TYPES = {
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "parttime": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
    "internship": "INTERNSHIP",
    "intern": "INTERNSHIP",
    "trainee": "INTERNSHIP",
    "volunteer": "VOLUNTEER",
    "freelance": "FREELANCE",
    "seasonal": "SEASONAL",
    "apprenticeship": "APPRENTICESHIP",
}

#: Experience levels, from the source's own wording (01 §33).
EXPERIENCE_LEVELS = {
    "intern": "INTERN",
    "entry": "ENTRY",
    "entry level": "ENTRY",
    "junior": "JUNIOR",
    "mid": "MID",
    "mid level": "MID",
    "intermediate": "MID",
    "senior": "SENIOR",
    "staff": "STAFF",
    "principal": "PRINCIPAL",
    "lead": "LEAD",
    "manager": "MANAGER",
    "head": "HEAD",
    "director": "DIRECTOR",
    "vp": "VP",
    "vice president": "VP",
    "executive": "EXECUTIVE",
    "c level": "EXECUTIVE",
}


def normalize_employment_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return EMPLOYMENT_TYPES.get(value.strip().lower())


def normalize_experience_level(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in EXPERIENCE_LEVELS:
        return EXPERIENCE_LEVELS[text]
    # a sentence-level marker ("Senior Backend Engineer") is still the
    # source's own word about level, so the leading token is consulted
    first = text.split()[0] if text.split() else ""
    return EXPERIENCE_LEVELS.get(first)


def normalize_observation(
    observation, *, observed_at: str | None = None
) -> NormalizedContent:
    """Deterministic normalization of one observation's fields (01 §34)."""
    fields = dict(observation.fields or {})
    title = str(fields.get("title") or "").strip()
    company = str(fields.get("company") or "").strip() or None
    description_raw = str(fields.get("description") or "").strip() or None
    if description_raw:
        cleaned = clean(description_raw)
        description_md = cleaned.markdown
        description_text = cleaned.text
        description_lang = cleaned.lang
        description_hash = cleaned.content_hash
    else:
        description_md = description_text = None
        description_lang = None
        description_hash = None
    salary_text = str(fields.get("salary") or "").strip() or None
    s_min = s_max = s_cur = s_per = None
    if salary_text:
        s_min, s_max, s_cur, s_per = parse_salary(salary_text)
    location_fields = fields.get("locations") or fields.get("location") or []
    if isinstance(location_fields, str):
        location_fields = [location_fields]
    from jobscraper.pipeline.locations import (
        normalize_locations,
        remote_mode_of,
        remote_worldwide,
    )

    location_records = normalize_locations(
        location_fields,
        job_location_type=fields.get("job_location_type"),
        applicant_location_requirements=fields.get("applicant_location_requirements"),
        reference_at=observed_at,
    )
    locations = tuple(record.raw_text for record in location_records)
    return NormalizedContent(
        title=title,
        normalized_title=normalize_title(title),
        company_name=company,
        normalized_company=normalize_company(company) if company else None,
        careers_url=str(fields.get("careers_url") or "").strip() or None,
        organization_domains=tuple(
            str(v).strip()
            for v in (fields.get("organization_domains") or [])
            if str(v).strip()
        ),
        company_country=str(fields.get("company_country") or "").strip() or None,
        description_md=description_md,
        description_text=description_text,
        description_lang=description_lang,
        description_hash=description_hash,
        content_cleaning_version=(
            CONTENT_CLEANING_VERSION if description_raw else None
        ),
        salary_original_text=salary_text,
        salary_min=s_min,
        salary_max=s_max,
        salary_currency=s_cur,
        salary_period=s_per,
        posted_at=fields.get("posted_at"),
        locations=locations,
        location_records=location_records,
        remote_mode=remote_mode_of(location_records),
        remote_worldwide=1 if remote_worldwide(location_records) else 0,
        employment_type=normalize_employment_type(fields.get("employment_type")),
        experience_level=normalize_experience_level(fields.get("experience_level")),
    )


__all__ = [
    "EMPLOYMENT_TYPES",
    "EXPERIENCE_LEVELS",
    "NORMALIZATION_VERSION",
    "NormalizedContent",
    "detect_language",
    "normalize_company",
    "normalize_employment_type",
    "normalize_experience_level",
    "normalize_observation",
    "normalize_title",
    "parse_salary",
]
