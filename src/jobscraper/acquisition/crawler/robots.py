"""Host-owned robots planning/evaluation for generic public crawling.

Network I/O is never performed here.  A robots fetch is represented as an
ordinary durable ``SOURCE_CRAWL`` request, planned here, wrapped in the common
``ExecutionPlanEnvelope`` by the service and executed by ``dispatch_http``.
The bounded parsed policy is safe to persist in acquisition evidence so later
page decisions after restart do not need a hidden re-fetch.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Mapping
import re
from urllib.parse import urlsplit, urlunsplit

from jobscraper.acquisition.envelope import RequestPlan
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.net.urlnorm import normalize_url
from jobscraper.runtime.requests import enqueue_request

ROBOTS_USER_AGENT = "WindowsJobScraper"
ROBOTS_MAX_BYTES = 512_000
ROBOTS_TIMEOUT_S = 10.0
ROBOTS_MAX_POLICY_CHARS = 65_536


class RobotsDecisionKind(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"
    NOT_REQUIRED = "NOT_REQUIRED"


class RobotsPolicyStatus(Enum):
    AVAILABLE = "AVAILABLE"
    NOT_PRESENT = "NOT_PRESENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RobotsDecision:
    kind: RobotsDecisionKind
    reason: str
    evidence_ref: str | None = None


@dataclass(frozen=True)
class RobotsPolicy:
    status: RobotsPolicyStatus
    reason: str
    lines: tuple[str, ...] = ()

    def to_detail(self) -> dict:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "lines": list(self.lines),
        }

    @classmethod
    def from_detail(cls, detail: Mapping[str, object]) -> "RobotsPolicy":
        try:
            status = RobotsPolicyStatus(str(detail["status"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("invalid persisted robots policy status") from exc
        reason = str(detail.get("reason") or "")
        lines_raw = detail.get("lines", [])
        if not isinstance(lines_raw, list) or not all(isinstance(v, str) for v in lines_raw):
            raise ValueError("invalid persisted robots policy lines")
        lines = tuple(lines_raw)
        if sum(len(line) + 1 for line in lines) > ROBOTS_MAX_POLICY_CHARS:
            raise ValueError("persisted robots policy exceeds bounded size")
        return cls(status, reason, lines)


@dataclass(frozen=True)
class RobotsGate:
    state: str  # MISSING | PENDING | READY | UNKNOWN
    policy: RobotsPolicy | None = None
    request_id: str | None = None


def robots_url(source_entry_url: str) -> str:
    normalized = normalize_url(source_entry_url)
    if normalized.scheme not in {"http", "https"} or not normalized.host:
        raise ValueError("robots source URL must be http(s)")
    parts = urlsplit(normalized.normalized)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def build_robots_request_plan(source_entry_url: str) -> RequestPlan:
    return RequestPlan(
        method="GET",
        url=robots_url(source_entry_url),
        headers={"Accept": "text/plain,*/*;q=0.1"},
        expected_content_types=("text/plain",),
        timeout_s=ROBOTS_TIMEOUT_S,
        max_bytes=ROBOTS_MAX_BYTES,
        purpose="SOURCE_CRAWL",
        expected_operation_class="READ",
    )


def parse_robots_result(result: ResultEnvelope) -> RobotsPolicy:
    if result.failure is not None or result.status_code is None:
        return RobotsPolicy(RobotsPolicyStatus.UNKNOWN, "ROBOTS_FETCH_UNAVAILABLE")
    status = int(result.status_code)
    if status in {404, 410}:
        return RobotsPolicy(RobotsPolicyStatus.NOT_PRESENT, "ROBOTS_NOT_PRESENT")
    if status < 200 or status >= 300:
        return RobotsPolicy(RobotsPolicyStatus.UNKNOWN, f"ROBOTS_HTTP_{status}")
    body = result.body or b""
    if len(body) > ROBOTS_MAX_POLICY_CHARS:
        return RobotsPolicy(RobotsPolicyStatus.UNKNOWN, "ROBOTS_POLICY_TOO_LARGE")
    text = body.decode("utf-8", "replace")
    # Persist only bounded directives/comments as text; no headers/cookies or
    # unrelated response metadata enter the policy evidence.
    lines = tuple(line.rstrip() for line in text.splitlines())
    if not any(line.strip().lower().startswith("user-agent:") for line in lines):
        return RobotsPolicy(RobotsPolicyStatus.UNKNOWN, "ROBOTS_MALFORMED_NO_USER_AGENT")
    return RobotsPolicy(RobotsPolicyStatus.AVAILABLE, "ROBOTS_POLICY_AVAILABLE", lines)


def _robots_groups(lines: tuple[str, ...]) -> list[tuple[tuple[str, ...], tuple[tuple[bool, str], ...]]]:
    """Parse bounded robots directives into deterministic user-agent groups.

    We intentionally implement only the matching semantics needed for policy
    enforcement here instead of depending on ``urllib.robotparser``: Python's
    parser uses first-match rule ordering, while the modern robots protocol
    requires the most specific matching rule to win (with Allow winning an
    equal-specificity tie).
    """
    groups: list[tuple[tuple[str, ...], tuple[tuple[bool, str], ...]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    saw_rule = False

    def flush() -> None:
        nonlocal agents, rules, saw_rule
        if agents:
            groups.append((tuple(agents), tuple(rules)))
        agents, rules, saw_rule = [], [], False

    for raw in lines:
        text = raw.split("#", 1)[0].strip()
        if not text or ":" not in text:
            continue
        field, value = (part.strip() for part in text.split(":", 1))
        key = field.lower()
        if key == "user-agent":
            if saw_rule:
                flush()
            token = value.lower()
            if token:
                agents.append(token)
            continue
        if key in {"allow", "disallow"} and agents:
            saw_rule = True
            # Empty Disallow means no restriction. Empty Allow carries no
            # useful matching authority either, so both are ignored.
            if value:
                rules.append((key == "allow", value))
    flush()
    return groups


def _agent_matches(token: str, user_agent: str) -> bool:
    if token == "*":
        return True
    ua = user_agent.lower()
    return token in ua


def _rule_match(rule: str, path_query: str) -> tuple[bool, int]:
    anchored = rule.endswith("$")
    source = rule[:-1] if anchored else rule
    specificity = len(source.replace("*", ""))
    pattern = re.escape(source).replace(r"\*", ".*")
    regex = "^" + pattern + ("$" if anchored else "")
    try:
        return re.search(regex, path_query) is not None, specificity
    except re.error:
        return False, 0


def evaluate_robots_policy(
    policy: RobotsPolicy,
    target_url: str,
    *,
    user_agent: str = ROBOTS_USER_AGENT,
) -> RobotsDecision:
    if policy.status is RobotsPolicyStatus.NOT_PRESENT:
        return RobotsDecision(RobotsDecisionKind.ALLOW, policy.reason)
    if policy.status is RobotsPolicyStatus.UNKNOWN:
        return RobotsDecision(RobotsDecisionKind.UNKNOWN, policy.reason)

    try:
        normalized = normalize_url(target_url)
    except Exception:
        return RobotsDecision(RobotsDecisionKind.UNKNOWN, "ROBOTS_TARGET_INVALID")
    parts = urlsplit(normalized.normalized)
    path_query = parts.path or "/"
    if parts.query:
        path_query += "?" + parts.query

    groups = _robots_groups(policy.lines)
    if not groups:
        return RobotsDecision(RobotsDecisionKind.UNKNOWN, "ROBOTS_PARSE_ERROR")

    specific = [g for g in groups if any(a != "*" and _agent_matches(a, user_agent) for a in g[0])]
    selected = specific or [g for g in groups if "*" in g[0]]
    if not selected:
        return RobotsDecision(RobotsDecisionKind.ALLOW, "ROBOTS_NO_APPLICABLE_GROUP")

    matches: list[tuple[int, bool]] = []
    for _agents, rules in selected:
        for allow, rule in rules:
            matched, specificity = _rule_match(rule, path_query)
            if matched:
                matches.append((specificity, allow))
    if not matches:
        return RobotsDecision(RobotsDecisionKind.ALLOW, "ROBOTS_ALLOWED")

    best = max(specificity for specificity, _allow in matches)
    allowed = any(allow for specificity, allow in matches if specificity == best)
    return RobotsDecision(
        RobotsDecisionKind.ALLOW if allowed else RobotsDecisionKind.DENY,
        "ROBOTS_ALLOWED" if allowed else "ROBOTS_DENIED",
    )


def evaluate_robots_result(
    result: ResultEnvelope,
    *,
    target_url: str,
    user_agent: str = ROBOTS_USER_AGENT,
) -> RobotsDecision:
    return evaluate_robots_policy(
        parse_robots_result(result), target_url, user_agent=user_agent
    )


def ensure_robots_request(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    plan_row: Mapping[str, object],
    source_entry_url: str,
    parent_request_id: str | None,
    priority: int,
    now: str,
    commit: bool = True,
) -> tuple[str, bool]:
    """Create/reuse the one durable robots fetch for this immutable run plan."""
    url = robots_url(source_entry_url)
    return enqueue_request(
        conn,
        run_id=run_id,
        run_source_plan_id=str(plan_row["id"]),
        source_id=str(plan_row["source_id"]),
        binding_id=str(plan_row["binding_id"]),
        request_type="SOURCE_CRAWL",
        target_identity=url,
        payload={"role": "ROBOTS", "target_reference": url},
        strategy=str(plan_row["strategy"]),
        execution_class=str(plan_row["execution_class"]),
        priority=int(priority),
        depth=0,
        parent_request_id=parent_request_id,
        logical_key=f"ROBOTS:{url}",
        now=now,
        commit=commit,
    )


def load_robots_gate(conn: sqlite3.Connection, run_source_plan_id: str) -> RobotsGate:
    """Resolve persisted policy or robots work state for one immutable plan."""
    evidence = conn.execute(
        """
        SELECT ae.detail_json, ae.ref, req.id AS request_id
          FROM acquisition_evidence ae
          JOIN scrape_requests req ON req.id = ae.request_id
         WHERE req.run_source_plan_id = ?
           AND req.request_type = 'SOURCE_CRAWL'
           AND ae.ref = 'robots://POLICY'
         ORDER BY ae.observed_at DESC, ae.id DESC
         LIMIT 1
        """,
        (run_source_plan_id,),
    ).fetchone()
    if evidence is not None:
        try:
            detail = json.loads(evidence["detail_json"] or "{}")
            return RobotsGate(
                "READY", RobotsPolicy.from_detail(detail), evidence["request_id"]
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return RobotsGate("UNKNOWN", None, evidence["request_id"])

    rows = conn.execute(
        """
        SELECT id, status, payload_json
          FROM scrape_requests
         WHERE run_source_plan_id = ? AND request_type = 'SOURCE_CRAWL'
         ORDER BY created_at ASC, id ASC
        """,
        (run_source_plan_id,),
    ).fetchall()
    robots_rows = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("role") == "ROBOTS":
            robots_rows.append(row)
    if not robots_rows:
        return RobotsGate("MISSING")
    latest = robots_rows[-1]
    if latest["status"] in {"PENDING", "RUNNING", "RETRY_WAIT"}:
        return RobotsGate("PENDING", None, latest["id"])
    # A terminal robots request without durable policy evidence is a visible
    # unknown policy outcome, never permission to crawl.
    return RobotsGate("UNKNOWN", None, latest["id"])


__all__ = [
    "ROBOTS_MAX_BYTES", "ROBOTS_MAX_POLICY_CHARS", "ROBOTS_TIMEOUT_S", "ROBOTS_USER_AGENT",
    "RobotsDecision", "RobotsDecisionKind", "RobotsGate", "RobotsPolicy", "RobotsPolicyStatus",
    "build_robots_request_plan", "ensure_robots_request", "evaluate_robots_policy",
    "evaluate_robots_result", "load_robots_gate", "parse_robots_result", "robots_url",
]
