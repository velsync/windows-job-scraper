"""Location parsing and multi-location support.

Authority: module 01 section 33.2 (a job may have multiple locations; the
canonical model does not assume one location_text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COUNTRIES = {
    "usa": "US", "united states": "US", "us": "US", "u.s.": "US", "u.s.a.": "US", "america": "US",
    "uk": "GB", "united kingdom": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "canada": "CA", "germany": "DE", "deutschland": "DE", "france": "FR", "netherlands": "NL",
    "spain": "ES", "italy": "IT", "portugal": "PT", "poland": "PL", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "switzerland": "CH", "austria": "AT", "ireland": "IE",
    "belgium": "BE", "australia": "AU", "new zealand": "NZ", "japan": "JP", "singapore": "SG",
    "india": "IN", "brazil": "BR", "mexico": "MX", "israel": "IL", "u.a.e.": "AE", "uae": "AE",
    "south africa": "ZA", "south korea": "KR", "china": "CN", "hong kong": "HK", "taiwan": "TW",
    "czech republic": "CZ", "czechia": "CZ", "greece": "GR", "hungary": "HU", "romania": "RO",
    "turkey": "TR", "argentina": "AR", "chile": "CL", "colombia": "CO",
}

ISO_CODE_RE = re.compile(r"\b([A-Z]{2})\b")

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
}
CA_PROVINCES = {"ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU"}

# 2-3 letter tokens that are region codes, not countries ("City, XX" style).
REGION_CODES = US_STATES | CA_PROVINCES

# Input codes that alias to canonical ISO country codes.
CODE_ALIASES = {"UK": "GB", "UAE": "AE", "USA": "US", "U.S.": "US", "U.S.A": "US"}

REMOTE_PATTERNS = ("remote", "work from home", "anywhere", "distributed", "telecommut")

# Separators between sibling locations. "City, ST" commas are NOT separators.
# NOTE on ambiguity: a trailing 2-letter code that is a US state (e.g. "DE")
# is read as a state (US posting convention); non-US postings spell country
# names ("Munich, Germany"), which the country-name table handles.
_SPLIT_RE = re.compile(r"\s*(?:;|·|\||/|,?\s+or\s+|,?\s+and\s+)\s*")


@dataclass
class ParsedLocation:
    raw_text: str
    country: str | None = None
    region: str | None = None
    city: str | None = None
    remote: bool = False
    confidence: float = 0.4

    def as_dict(self) -> dict:
        return {
            "raw_text": self.raw_text,
            "country": self.country,
            "region": self.region,
            "city": self.city,
            "remote": self.remote,
            "confidence": self.confidence,
        }


def _detect_country(part: str) -> str | None:
    low = part.strip().lower()
    if low in COUNTRIES:
        return COUNTRIES[low]
    stripped = part.strip()
    # Bare code or trailing ", XX" / ", XX[XX]" code.
    m = re.search(r"(?:^|,\s*)([A-Z]{2}(?:[A-Z])?)$", stripped)
    if m:
        code = m.group(1)
        if code in REGION_CODES:
            # "City, CA"/"City, ON": state/province code implies country.
            return "US" if code in US_STATES else "CA"
        if code in CODE_ALIASES:
            return CODE_ALIASES[code]
        if code in {c for c in COUNTRIES.values()}:
            return code
    for name, code in COUNTRIES.items():
        if name in low and len(name) > 3:
            return code
    return None


def _detect_region(part: str) -> str | None:
    stripped = part.strip()
    m = re.search(r",\s*([A-Z]{2})$", stripped)
    if m and m.group(1) in REGION_CODES:
        return m.group(1)
    return None


def _scan_country_token(text: str) -> str | None:
    """Standalone country code/name token anywhere in a remote string."""
    codes = {c for c in COUNTRIES.values()} | {"USA", "UK"}
    for tok in re.findall(r"\b[A-Za-z]{2,3}\b", text):
        if tok in CODE_ALIASES:
            return CODE_ALIASES[tok]
        if tok in codes and tok not in REGION_CODES:
            return tok
    for name, code in COUNTRIES.items():
        if len(name) > 3 and re.search(rf"\b{re.escape(name)}\b", text.lower()):
            return code
    return None


def parse_location(raw: str | None) -> list[ParsedLocation]:
    """Parse a raw location string into one or more structured locations."""
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    lowered = text.lower()
    # Hybrid prefixes carry a real location after them.
    hybrid = lowered.startswith("hybrid")
    if hybrid:
        text = re.sub(r"^hybrid\s*[-–—:|,]?\s*", "", text, flags=re.IGNORECASE).strip() or raw.strip()
        lowered = text.lower()
    if any(p in lowered for p in REMOTE_PATTERNS):
        # Remote may combine with a country constraint ("Remote — US only").
        country = _detect_country(text) or _scan_country_token(text)
        return [ParsedLocation(raw_text=raw.strip(), country=country, remote=True, confidence=0.8)]
    parts = [p.strip(" ,") for p in _SPLIT_RE.split(text) if p.strip(" ,")]
    if len(parts) <= 1:
        country = _detect_country(text)
        region = _detect_region(text)
        segments = [s.strip() for s in text.split(",") if s.strip()]
        city = segments[0] if segments else None
        if region is None:
            region = segments[1] if len(segments) > 1 else None
        return [
            ParsedLocation(
                raw_text=text,
                country=country,
                region=region,
                city=city,
                remote=False,
                confidence=0.6 if country else 0.3,
            )
        ]
    out = []
    for part in parts[:10]:  # bounded
        out.extend(parse_location(part))
    return out or [ParsedLocation(raw_text=text, remote=False, confidence=0.2)]


def normalize_locations(raw: str | None, secondary: list[str] | None = None, *, remote_hint: bool = False) -> list[dict]:
    """Normalize primary + secondary locations into job_locations dicts."""
    results: list[dict] = []
    seen: set[str] = set()
    for loc in parse_location(raw):
        key = (loc.country, loc.region, loc.city, loc.remote)
        if key in seen:
            continue
        seen.add(key)
        results.append(loc.as_dict())
    for extra in secondary or []:
        for loc in parse_location(extra):
            key = (loc.country, loc.region, loc.city, loc.remote)
            if key in seen:
                continue
            seen.add(key)
            results.append(loc.as_dict())
    if remote_hint and results and not any(r["remote"] for r in results):
        results.append(ParsedLocation(raw_text="Remote", remote=True, confidence=0.7).as_dict())
    return results[:12]
