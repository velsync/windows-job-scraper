"""Tests for central secret redaction (S0.4 / SEC-08)."""

import pytest

from jobscraper.security.redaction import (
    RedactionError,
    redact_headers,
    redact_string,
    redact_value,
)


def test_secret_keys_redacted():
    data = {
        "authorization": "Bearer abc",
        "Cookie": "session=xyz",
        "set_cookie": "a=b",
        "api_key": "k123",
        "X-API-Key": "k456",
        "password": "hunter2",
        "storage_state": {"cookies": []},
        "session_id": "abc",
        "bootstrap_ticket": "t",
        "nested": {"token": "deep", "ok": "fine", "auth_header": "x"},
    }
    out = redact_value(data)
    assert out["authorization"] == "[REDACTED]"
    assert out["Cookie"] == "[REDACTED]"
    assert out["set_cookie"] == "[REDACTED]"
    assert out["api_key"] == "[REDACTED]"
    assert out["X-API-Key"] == "[REDACTED]"
    assert out["password"] == "[REDACTED]"
    assert out["storage_state"] == "[REDACTED]"
    assert out["session_id"] == "[REDACTED]"
    assert out["bootstrap_ticket"] == "[REDACTED]"
    assert out["nested"]["token"] == "[REDACTED]"
    assert out["nested"]["auth_header"] == "[REDACTED]"
    assert out["nested"]["ok"] == "fine"


def test_benign_keys_survive():
    data = {"title": "Senior Engineer", "status_code": 200, "ok": True, "url": "https://x.example/jobs"}
    out = redact_value(data)
    assert out == data


def test_secret_shaped_values_redacted():
    text = "request failed with Authorization: Bearer supersecret and wjs_session=abc123"
    out = redact_string(text)
    assert "supersecret" not in out
    assert "abc123" not in out
    assert "[REDACTED]" in out


def test_nested_json_redaction():
    data = {"headers": {"Authorization": "Bearer x", "Content-Type": "application/json"}}
    out = redact_value(data)
    assert out["headers"]["Authorization"] == "[REDACTED]"
    assert out["headers"]["Content-Type"] == "application/json"


def test_depth_guard():
    deep = {}
    node = deep
    for _ in range(50):
        node["child"] = {}
        node = node["child"]
    with pytest.raises(RedactionError):
        redact_value(deep)


def test_unknown_object_types_never_serialized():
    class Secretish:
        def __str__(self):
            return "should-not-serialize"

    out = redact_value({"obj": Secretish()})
    assert out["obj"] == "[REDACTED]"


def test_redact_headers():
    headers = {"Authorization": "Bearer t", "Cookie": "c", "Content-Type": "text/html"}
    out = redact_headers(headers)
    assert out["Authorization"] == "[REDACTED]"
    assert out["Cookie"] == "[REDACTED]"
    assert out["Content-Type"] == "text/html"


def test_exception_text_redaction_shape():
    # Exceptions may embed headers; redaction must handle string forms.
    text = "HTTPError('401', headers={'Set-Cookie': 'leaky'})"
    out = redact_string(text)
    assert "leaky" not in out
