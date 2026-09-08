"""Durable-evidence serialization helpers (03 §30).

Evidence rows hold *references, hashes and redacted metadata*, and they must
stay parseable forever: a later review reads them with ``json.loads``.  A raw
``json.dumps(...)[:limit]`` can cut a document in half and silently store
unusable bytes, so bounding here is structural: the longest string leaves are
shortened first, and the row always carries an explicit ``_truncated`` marker
when it was shortened.  Truncation is therefore always visible — it can never
look like the evidence said less than it did.
"""

from __future__ import annotations

import json

#: Default budget for one durable evidence payload (characters of JSON text).
EVIDENCE_DETAIL_LIMIT = 60_000

#: Leaf widths tried, widest first, when a document does not fit.
_SHRINK_STEPS = (2_000, 400, 80, 24)


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _shrink(value, leaf_limit: int):
    """Shorten long string leaves recursively, preserving the structure."""
    if isinstance(value, str):
        if len(value) <= leaf_limit:
            return value
        return value[: max(0, leaf_limit - 1)] + "…"
    if isinstance(value, dict):
        return {str(k): _shrink(v, leaf_limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shrink(v, leaf_limit) for v in value]
    return value


def bounded_json(
    detail,
    *,
    limit: int = EVIDENCE_DETAIL_LIMIT,
) -> str:
    """Serialize ``detail`` as valid JSON of at most ``limit`` characters.

    Always returns parseable JSON: the full document when it fits, a
    leaf-shrunk copy plus a ``_truncated`` marker when it does not, and a
    keys-only summary in the pathological case (a very wide document).
    """
    original = detail if isinstance(detail, dict) else {"detail": detail}
    full = _dump(original)
    if len(full) <= limit:
        return full

    original_chars = len(full)
    for step in _SHRINK_STEPS:
        shrunk = _shrink(original, step)
        marker = {
            "_truncated": True,
            "_original_chars": original_chars,
            "_leaf_limit": step,
        }
        if isinstance(shrunk, dict):
            candidate = {**shrunk, **marker}
        else:  # pragma: no cover - "detail" wrapper above makes this a dict
            candidate = {"detail": shrunk, **marker}
        text = _dump(candidate)
        if len(text) <= limit:
            return text

    # pathological width (huge number of keys): keep the shape, not the content
    keys = sorted(str(key) for key in original)[:200]
    fallback = {
        "_truncated": True,
        "_original_chars": original_chars,
        "_keys": keys,
        "_note": "evidence detail exceeded the durable limit; keys retained",
    }
    text = _dump(fallback)
    while len(text) > limit and fallback["_keys"]:
        fallback["_keys"] = fallback["_keys"][:-1]
        text = _dump(fallback)
    return text if len(text) <= limit else _dump({"_truncated": True})


EXCERPT_LIMIT = 500


def excerpt_and_hash(excerpt: str) -> tuple[str, str | None]:
    """The stored excerpt and the hash *of what is stored*.

    Hashing the full excerpt while persisting a truncated one would leave a
    reader unable to verify the row, so the two are derived together.
    """
    stored = (excerpt or "")[:EXCERPT_LIMIT]
    if not stored:
        return "", None
    import hashlib

    return stored, hashlib.sha256(stored.encode()).hexdigest()


__all__ = [
    "EVIDENCE_DETAIL_LIMIT",
    "EXCERPT_LIMIT",
    "bounded_json",
    "excerpt_and_hash",
]
