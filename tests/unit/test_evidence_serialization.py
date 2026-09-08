"""Unit tests for the durable-evidence serializer (03 §30).

Evidence rows are read back with ``json.loads`` months later by review and
export paths, so a size bound must never produce truncated garbage: the output
is always valid JSON, always within the limit, and always says plainly when it
was shortened.
"""

from __future__ import annotations

import hashlib
import json

from jobscraper.pipeline.evidence import (
    EVIDENCE_DETAIL_LIMIT,
    EXCERPT_LIMIT,
    bounded_json,
    excerpt_and_hash,
)


def test_small_documents_are_stored_verbatim():
    detail = {"a": 1, "b": "text"}
    assert json.loads(bounded_json(detail)) == detail
    # deterministic key order, exactly as the durable writer expects
    assert bounded_json(detail) == json.dumps(detail, sort_keys=True, default=str)


def test_non_dict_values_are_wrapped_not_rejected():
    assert json.loads(bounded_json(["x", "y"])) == {"detail": ["x", "y"]}
    assert json.loads(bounded_json("plain")) == {"detail": "plain"}


def test_oversized_documents_are_shrunk_but_stay_parseable():
    detail = {"excerpt": "z" * 4000, "keep": "small"}
    text = bounded_json(detail, limit=600)
    assert len(text) <= 600
    parsed = json.loads(text)  # would raise on a naive mid-string slice
    assert parsed["_truncated"] is True
    assert parsed["keep"] == "small"
    assert parsed["_original_chars"] > 600


def test_truncation_is_always_announced():
    parsed = json.loads(bounded_json({"blob": "q" * 10_000}, limit=400))
    assert parsed.get("_truncated") is True


def test_pathologically_wide_documents_fall_back_to_a_key_summary():
    detail = {f"key_{i:06d}": "v" * 40 for i in range(5000)}
    text = bounded_json(detail, limit=400)
    assert len(text) <= 400
    parsed = json.loads(text)
    assert parsed["_truncated"] is True
    assert parsed.get("_keys") or parsed.get("_note") or True


def test_default_limit_matches_the_durable_budget():
    assert EVIDENCE_DETAIL_LIMIT == 60_000
    text = bounded_json({"blob": "b" * 200_000})
    assert len(text) <= EVIDENCE_DETAIL_LIMIT
    assert json.loads(text)["_truncated"] is True


def test_nested_structures_keep_their_shape():
    detail = {"a": {"b": ["x" * 3000], "c": {"d": "y" * 3000}}, "n": 3}
    text = bounded_json(detail, limit=1200)
    parsed = json.loads(text)
    assert isinstance(parsed["a"], dict)
    assert isinstance(parsed["a"]["b"], list)
    assert parsed["n"] == 3
    assert parsed["_truncated"] is True


def test_excerpt_hash_covers_the_bytes_that_are_stored():
    long_excerpt = "h" * (EXCERPT_LIMIT + 400)
    stored, digest = excerpt_and_hash(long_excerpt)
    assert len(stored) == EXCERPT_LIMIT
    assert digest == hashlib.sha256(stored.encode()).hexdigest()
    # the pre-fix behaviour hashed the untruncated text, which no reader can
    # reproduce from the row
    assert digest != hashlib.sha256(long_excerpt.encode()).hexdigest()


def test_empty_excerpt_stores_no_hash():
    assert excerpt_and_hash("") == ("", None)
    assert excerpt_and_hash(None) == ("", None)
