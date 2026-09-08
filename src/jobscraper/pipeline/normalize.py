"""Deterministic content normalization (01 §34, §37).

raw/structured description → deterministic cleaning → Markdown → plain
text → language → hashes. Salary parsing preserves the original text and
never fabricates numbers: unknown salary stays NULL (never zero).
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass

_BLOCK_TAGS = re.compile(
    r"</?(p|div|section|article|header|footer|ul|ol|li|h[1-6]|br|tr|table)[^>]*>",
    re.IGNORECASE,
)
_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]+")
_NEWLINES = re.compile(r"\n{3,}")

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


def _strip_html(markup: str) -> str:
    text = _SCRIPT_STYLE.sub(" ", markup or "")
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html_lib.unescape(text)
    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return _NEWLINES.sub("\n\n", "\n".join(lines)).strip()


def _to_markdown(markup: str) -> str:
    text = _SCRIPT_STYLE.sub(" ", markup or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<h([1-6])[^>]*>", lambda m: "#" * int(m.group(1)) + " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<(strong|b)>(.*?)</\1>", r"**\2**", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(em|i)>(.*?)</\1>", r"*\2*", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", text, flags=re.IGNORECASE | re.DOTALL)
    text = _TAG.sub(" ", text)
    text = html_lib.unescape(text)
    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return _NEWLINES.sub("\n\n", "\n".join(lines)).strip()


_EN_STOP = frozenset(
    "the and for with you your our are have this that will from not per job work".split()
)
_DE_STOP = frozenset(
    "der die das und für mit Sie Ihre sind werden nicht aus dem den job".split()
)
_RO_STOP = frozenset(
    "și de la în pentru care vor fi acest nostru dumneavoastră sunt".split()
)


def detect_language(text: str) -> str | None:
    """Cheap deterministic stopword heuristic; None when undeterminable."""
    words = re.findall(r"[a-zăâîșțäöüß]+", (text or "").lower())
    if len(words) < 12:
        return None
    scores = {
        "en": sum(1 for w in words if w in _EN_STOP),
        "de": sum(1 for w in words if w in _DE_STOP),
        "ro": sum(1 for w in words if w in _RO_STOP),
    }
    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 3:
        return None
    return best


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


def normalize_observation(observation) -> NormalizedContent:
    """Deterministic normalization of one observation's fields (01 §34)."""
    fields = dict(observation.fields or {})
    title = str(fields.get("title") or "").strip()
    company = str(fields.get("company") or "").strip() or None
    description_raw = str(fields.get("description") or "").strip() or None
    description_md = _to_markdown(description_raw) if description_raw else None
    description_text = _strip_html(description_raw) if description_raw else None
    description_hash = (
        hashlib.sha256(description_text.encode()).hexdigest() if description_text else None
    )
    salary_text = str(fields.get("salary") or "").strip() or None
    s_min = s_max = s_cur = s_per = None
    if salary_text:
        s_min, s_max, s_cur, s_per = parse_salary(salary_text)
    locations = fields.get("locations") or []
    if isinstance(locations, str):
        locations = [locations]
    return NormalizedContent(
        title=title,
        normalized_title=normalize_title(title),
        company_name=company,
        normalized_company=normalize_company(company) if company else None,
        description_md=description_md,
        description_text=description_text,
        description_lang=detect_language(description_text or ""),
        description_hash=description_hash,
        salary_original_text=salary_text,
        salary_min=s_min,
        salary_max=s_max,
        salary_currency=s_cur,
        salary_period=s_per,
        posted_at=fields.get("posted_at"),
        locations=tuple(str(loc) for loc in locations),
    )


__all__ = [
    "NormalizedContent",
    "detect_language",
    "normalize_company",
    "normalize_observation",
    "normalize_title",
    "parse_salary",
]
