"""Source fingerprinting and ATS detection.

Authority: module 02 section 12.1 (fingerprint before specialized route).
Low-confidence fingerprinting falls back to generic discovery rather than
silently forcing a specialized adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobscraper.recipes import htmlutil


@dataclass(frozen=True)
class Fingerprint:
    family: str | None
    confidence: float
    evidence: tuple[str, ...] = ()
    recommended_adapter_id: str | None = None
    board_token: str | None = None
    title: str | None = None

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.7 and self.family is not None


@dataclass
class _Rule:
    family: str
    adapter_id: str
    weight: float
    pattern: re.Pattern


_RULES = [
    _Rule("greenhouse", "greenhouse", 0.8, re.compile(r"(boards|job-boards)\.greenhouse\.io", re.I)),
    _Rule("greenhouse", "greenhouse", 0.7, re.compile(r"boards-api\.greenhouse\.io", re.I)),
    _Rule("greenhouse", "greenhouse", 0.4, re.compile(r"grnhse", re.I)),
    _Rule("lever", "lever", 0.8, re.compile(r"jobs\.lever\.co", re.I)),
    _Rule("lever", "lever", 0.7, re.compile(r"api\.lever\.co/v0/postings", re.I)),
    _Rule("ashby", "ashby", 0.8, re.compile(r"jobs\.ashbyhq\.com", re.I)),
    _Rule("ashby", "ashby", 0.6, re.compile(r"api\.ashbyhq\.com", re.I)),
    _Rule("ashby", "ashby", 0.4, re.compile(r"ashby", re.I)),
    _Rule("smartrecruiters", None, 0.6, re.compile(r"smartrecruiters\.com", re.I)),
    _Rule("workable", None, 0.6, re.compile(r"apply\.workable\.com|workable\.com", re.I)),
    _Rule("recruitee", None, 0.6, re.compile(r"recruitee\.com", re.I)),
    _Rule("personio", None, 0.6, re.compile(r"jobs\.personio\.com|personio\.de", re.I)),
    _Rule("teamtailor", None, 0.6, re.compile(r"teamtailor\.com", re.I)),
    _Rule("workday", None, 0.6, re.compile(r"myworkdayjobs?\.com|workday\.com", re.I)),
    _Rule("bamboohr", None, 0.6, re.compile(r"bamboohr\.com", re.I)),
    _Rule("icims", None, 0.6, re.compile(r"icims\.com", re.I)),
    _Rule("successfactors", None, 0.6, re.compile(r"successfactors\.com", re.I)),
    _Rule("eightfold", None, 0.6, re.compile(r"eightfold\.ai", re.I)),
]

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def fingerprint_html(html: str, base_url: str = "") -> Fingerprint:
    """Classify HTML evidence for a candidate ATS/source family."""
    title_match = _TITLE_RE.search(html)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else None
    scores: dict[str, float] = {}
    evidence: list[str] = []
    for rule in _RULES:
        m = rule.pattern.search(html)
        if m:
            scores[rule.family] = scores.get(rule.family, 0.0) + rule.weight
            evidence.append(f"{rule.family}:{m.group(0)[:80]}")
    # Structured data presence is generic evidence, not ATS-specific.
    if "application/ld+json" in html:
        evidence.append("jsonld:present")
    if not scores:
        return Fingerprint(
            family=None,
            confidence=0.0,
            evidence=tuple(evidence),
            recommended_adapter_id="generic",
            title=title,
        )
    family = max(scores, key=lambda k: scores[k])
    confidence = min(0.95, scores[family])
    board_token = _extract_board_token(html, family, base_url)
    return Fingerprint(
        family=family,
        confidence=confidence,
        evidence=tuple(evidence),
        recommended_adapter_id="generic" if confidence < 0.7 else _adapter_for(family),
        board_token=board_token,
        title=title,
    )


def _adapter_for(family: str) -> str | None:
    mapping = {"greenhouse": "greenhouse", "lever": "lever", "ashby": "ashby"}
    return mapping.get(family)


def _extract_board_token(html: str, family: str, base_url: str) -> str | None:
    from jobscraper.acquisition.adapters import ashby, greenhouse, lever

    if family == "greenhouse":
        token = greenhouse.parse_entry_url(base_url) if base_url else None
        if token:
            return token
        m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_\-]+)", html)
        return m.group(1) if m else None
    if family == "lever":
        token = lever.parse_entry_url(base_url) if base_url else None
        if token:
            return token
        m = re.search(r"jobs\.lever\.co/([A-Za-z0-9_\-]+)", html)
        return m.group(1) if m else None
    if family == "ashby":
        token = ashby.parse_entry_url(base_url) if base_url else None
        if token:
            return token
        m = re.search(r"jobs\.ashbyhq\.com/([A-Za-z0-9_\-]+)", html)
        return m.group(1) if m else None
    return None


def classify_url_shape(url: str) -> Fingerprint:
    """Direct URL-shape classification without content I/O."""
    from jobscraper.acquisition.adapters import ashby, greenhouse, lever

    for family, parse in (
        ("greenhouse", greenhouse.parse_entry_url),
        ("lever", lever.parse_entry_url),
        ("ashby", ashby.parse_entry_url),
    ):
        token = parse(url)
        if token:
            return Fingerprint(
                family=family,
                confidence=0.85,
                evidence=(f"url:{url[:120]}",),
                recommended_adapter_id=_adapter_for(family),
                board_token=token,
            )
    return Fingerprint(
        family=None,
        confidence=0.0,
        evidence=(f"url:{url[:120]}",),
        recommended_adapter_id="generic",
    )
