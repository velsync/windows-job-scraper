"""Evidence-backed fact extraction.

Authority: module 01 section 34. Rules are versioned data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

FACTS_RULE_VERSION = "facts-rules-v1"

SENIORITY_PATTERNS = [
    ("intern", "intern"), ("internship", "intern"),
    ("junior", "junior"), ("entry-level", "junior"), ("entry level", "junior"),
    ("mid-level", "mid"), ("mid level", "mid"),
    ("senior", "senior"), ("sr.", "senior"), ("sr ", "senior"),
    ("staff", "staff"), ("principal", "principal"), ("distinguished", "distinguished"),
    ("lead", "lead"), ("head of", "lead"), ("manager", "manager"), ("director", "manager"),
    ("vp ", "manager"), ("chief", "manager"),
]

EMPLOYMENT_PATTERNS = [
    ("full-time", "FULL_TIME"), ("full time", "FULL_TIME"),
    ("part-time", "PART_TIME"), ("part time", "PART_TIME"),
    ("contract", "CONTRACT"), ("contractor", "CONTRACT"), ("freelance", "CONTRACT"),
    ("internship", "INTERNSHIP"), ("temporary", "TEMPORARY"),
]

SKILLS = (
    "python", "javascript", "typescript", "java", "golang", "rust", "c++", "c#",
    "react", "vue", "angular", "svelte", "node.js", "django", "flask", "fastapi",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch", "kafka", "rabbitmq",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible",
    "machine learning", "deep learning", "pytorch", "tensorflow", "llm", "nlp",
    "css", "html", "tailwind", "graphql", "rest api", "microservices",
    "ci/cd", "jenkins", "github actions", "linux", "bash", "sql", "spark", "dbt",
)

AUTHORIZATION_PATTERNS = [
    # Negative senses FIRST: "No visa sponsorship available" must not match
    # the positive "visa sponsorship" rule.
    (r"no (?:visa |work )?sponsorship", "NO_SPONSORSHIP"),
    (r"not (?:be )?(?:able to )?offer(?:ing)? (?:visa )?sponsorship", "NO_SPONSORSHIP"),
    (r"(?:visa )?sponsorship (?:is )?not (?:available|offered|provided)", "NO_SPONSORSHIP"),
    (r"unable to (?:provide|offer) (?:visa )?sponsorship", "NO_SPONSORSHIP"),
    (r"without (?:visa )?sponsorship", "NO_SPONSORSHIP"),
    (r"cannot (?:provide|offer) (?:visa )?sponsorship", "NO_SPONSORSHIP"),
    (r"visa sponsorship", "SPONSORSHIP_OFFERED"),
    (r"sponsor(?:ship)? (?:is )?(?:available|offered|provided)", "SPONSORSHIP_OFFERED"),
    (r"must (?:be able to )?(?:obtain|have) .{0,40}work authorization", "AUTHORIZATION_REQUIRED"),
    (r"authorized to work in (?:the )?([A-Za-z .]+)", "AUTHORIZED_IN"),
    (r"requires? (?:a )?(?:valid )?work (?:permit|visa)", "WORK_PERMIT_REQUIRED"),
]

REMOTE_PATTERNS = [
    (r"fully remote|100% remote|work from anywhere", "REMOTE_FULL"),
    (r"hybrid", "HYBRID"),
    (r"on-?site|in-?office", "ONSITE"),
    (r"remote-first", "REMOTE_FIRST"),
]

TIMEZONE_PATTERNS = [
    (r"(?:gmt|utc)([+-]\d{1,2})", "UTC_OFFSET"),
    (r"overlap with ([A-Za-z ]+)(?: time)?", "OVERLAP"),
    (r"(\w+(?: \w+)?) time zone", "ZONE_NAME"),
]


@dataclass
class ExtractedFact:
    fact_type: str
    value: object
    confidence: float
    evidence_text: str
    evidence_start: int
    evidence_end: int
    rule_id: str
    rule_version: str = FACTS_RULE_VERSION

    def as_row(self, job_id: str, observed_at: str) -> dict:
        return {
            "job_id": job_id,
            "fact_type": self.fact_type,
            "value_json": json.dumps(self.value, sort_keys=True),
            "confidence": self.confidence,
            "evidence_text": self.evidence_text[:500],
            "evidence_start": self.evidence_start,
            "evidence_end": self.evidence_end,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "observed_at": observed_at,
        }


def _fact(fact_type, value, confidence, text, start, end, rule_id):
    return ExtractedFact(fact_type, value, confidence, text[max(0, start - 40):end + 40], start, end, rule_id)


def extract_facts(title: str, description_text: str, employment_hint: str | None = None) -> list[ExtractedFact]:
    """Extract versioned, evidence-backed facts from title + description."""
    facts: list[ExtractedFact] = []
    corpus = f"{title}\n{description_text}"
    low = corpus.lower()
    title_low = (title or "").lower()

    # Seniority — prefer title evidence.
    for pattern, level in SENIORITY_PATTERNS:
        idx = title_low.find(pattern)
        if idx >= 0:
            facts.append(_fact("seniority", level, 0.85, title, idx, idx + len(pattern), f"seniority:{pattern}"))
            break
    else:
        for pattern, level in SENIORITY_PATTERNS:
            idx = low.find(pattern)
            if idx >= 0:
                facts.append(_fact("seniority", level, 0.5, corpus, idx, idx + len(pattern), f"seniority:{pattern}"))
                break

    # Employment type.
    if employment_hint:
        facts.append(_fact("employment_type", employment_hint, 0.9, "structured-field", 0, 0, "employment:structured"))
    else:
        for pattern, etype in EMPLOYMENT_PATTERNS:
            idx = low.find(pattern)
            if idx >= 0:
                facts.append(_fact("employment_type", etype, 0.6, corpus, idx, idx + len(pattern), f"employment:{pattern}"))
                break

    # Skills.
    found_skills: list[str] = []
    for skill in SKILLS:
        idx = low.find(skill)
        if idx >= 0:
            found_skills.append(skill)
    if found_skills:
        first_idx = low.find(found_skills[0])
        facts.append(
            ExtractedFact(
                "skills", found_skills, 0.7,
                corpus[first_idx : first_idx + len(found_skills[0])],
                first_idx, first_idx + len(found_skills[0]), "skills:list",
            )
        )

    # Work authorization.
    for pattern, meaning in AUTHORIZATION_PATTERNS:
        m = re.search(pattern, low)
        if m:
            value = meaning
            if meaning == "AUTHORIZED_IN":
                value = f"AUTHORIZED_IN:{m.group(1).strip()[:40]}"
            facts.append(
                _fact("work_authorization", value, 0.8, corpus, m.start(), m.end(), f"auth:{pattern[:30]}")
            )
            break

    # Remote scope.
    for pattern, scope in REMOTE_PATTERNS:
        m = re.search(pattern, low)
        if m:
            facts.append(_fact("remote_scope", scope, 0.75, corpus, m.start(), m.end(), f"remote:{pattern[:30]}"))
            break

    # Timezone requirement.
    for pattern, kind in TIMEZONE_PATTERNS:
        m = re.search(pattern, low)
        if m:
            facts.append(
                _fact("timezone_requirement", {"kind": kind, "match": m.group(0)[:60]}, 0.5,
                      corpus, m.start(), m.end(), f"tz:{pattern[:30]}")
            )
            break

    # Language mention (cheap deterministic: English default).
    if re.search(r"\b(?:fluent|proficient|native)\b (?:in )?(english|german|french|spanish)", low):
        m = re.search(r"(fluent|proficient|native)[^.]{0,30}(english|german|french|spanish)", low)
        if m:
            facts.append(
                _fact("language", m.group(2), 0.6, corpus, m.start(), m.end(), "language:explicit")
            )

    return facts
