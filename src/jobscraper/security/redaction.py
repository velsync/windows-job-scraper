"""Central secret redaction.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md SEC-08;
module 05 section 46 (unified event log).

Redaction occurs *before persistence* on the event path; diagnostics export
re-applies the same redactor defensively.
"""

from __future__ import annotations

import re
from typing import Any

# Keys whose values are always secret when present in structured data.
SECRET_KEY_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^authorization$",
        r"^proxy[-_]?authorization$",
        r"^cookie$",
        r"^set[-_]?cookie$",
        r"^x[-_]?api[-_]?key$",
        r"^api[-_]?key$",
        r"^apikey$",
        r"^token$",
        r"^.*[-_]token$",
        r"^token[-_].*$",
        r"^.*[-_]secret$",
        r"^secret[-_].*$",
        r"^password$",
        r"^passwd$",
        r"^credentials?$",
        r"^storage[-_]?state$",
        r"^session[-_]?id$",
        r"^bootstrap[-_]?ticket$",
        r"^csrf$",
        r"^wjs[-_]?session$",
        r"^wjs[-_]?csrf$",
        r"^install[-_]?secret$",
        r"^auth[-_]?header$",
        r"^bearer$",
    )
]

# Value shapes that look like secrets even under benign keys.
SECRET_VALUE_PATTERNS = [
    re.compile(r"bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(
        r"\b(?:wjs_session|wjs_csrf|bootstrap_ticket|ticket|token|secret|password)"
        r"['\"]?\s*[=:]\s*['\"]?\S+",
        re.IGNORECASE,
    ),
    re.compile(r"Authorization:\s*\S+", re.IGNORECASE),
    re.compile(r"Cookie:\s*\S+", re.IGNORECASE),
    re.compile(r"Set-Cookie:\s*\S+", re.IGNORECASE),
    re.compile(r"['\"](?:set[-_]?cookie|authorization|api[-_]?key|token|secret)['\"]?\s*[:=]\s*['\"]?\S+", re.IGNORECASE),
]

REDACTED = "[REDACTED]"

# Maximum depth/size guards so redaction itself cannot be a DoS vector.
_MAX_DEPTH = 12
_MAX_ITEMS = 10_000


class RedactionError(Exception):
    """Raised when input is too large/deep to redact safely."""


def _is_secret_key(key: str) -> bool:
    return any(p.search(key) is not None for p in SECRET_KEY_PATTERNS)


def redact_string(value: str) -> str:
    out = value
    for pattern in SECRET_VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def redact_value(value: Any, depth: int = 0, count: int = 0) -> Any:
    """Recursively redact a JSON-like structure before persistence.

    Raises :class:`RedactionError` when the structure exceeds safe bounds —
    callers must not silently persist unredacted oversized data.
    """
    if depth > _MAX_DEPTH:
        raise RedactionError("structure too deep to redact safely")
    count += 1
    if count > _MAX_ITEMS:
        raise RedactionError("structure too large to redact safely")
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if _is_secret_key(key_s):
                out[key_s] = REDACTED
            else:
                out[key_s] = redact_value(item, depth + 1, count)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_value(v, depth + 1, count) for v in value]
    if isinstance(value, str):
        return redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Unknown types (sets, objects...) — never serialize arbitrary objects.
    return REDACTED


def redact_headers(headers: dict[str, str] | list[tuple[str, str]]) -> dict[str, str]:
    """Redact an HTTP header mapping while preserving non-sensitive context."""
    if isinstance(headers, list):
        headers = {str(k): str(v) for k, v in headers}
    out: dict[str, str] = {}
    for key, value in headers.items():
        if _is_secret_key(str(key)):
            out[str(key)] = REDACTED
        else:
            out[str(key)] = redact_string(str(value))
    return out


def contains_secret_material(text: str, secret: str) -> bool:
    """Test helper: does ``text`` contain the exact secret value?"""
    return bool(secret) and secret in text
