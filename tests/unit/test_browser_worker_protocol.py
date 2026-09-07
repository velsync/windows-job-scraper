"""Unit tests for the browser-worker protocol (S0.9)."""

import json

import pytest

from jobscraper.browser_worker.protocol import (
    MAX_LINE_BYTES,
    ProtocolError,
    WorkerResponse,
    error_response,
    ok_response,
    parse_request,
)


class TestParseRequest:
    def test_valid_ping(self):
        req = parse_request(json.dumps({"type": "PING", "request_id": "r1"}))
        assert req.type == "PING"
        assert req.request_id == "r1"

    @pytest.mark.parametrize("msg_type", ["PING", "VERSION", "SMOKE", "SHUTDOWN"])
    def test_all_message_types(self, msg_type):
        req = parse_request(json.dumps({"type": msg_type, "request_id": "r"}))
        assert req.type == msg_type

    def test_unknown_type_rejected(self):
        with pytest.raises(ProtocolError) as excinfo:
            parse_request(json.dumps({"type": "EXECUTE", "request_id": "r"}))
        assert excinfo.value.kind == "UNKNOWN_TYPE"

    def test_malformed_json_rejected(self):
        with pytest.raises(ProtocolError) as excinfo:
            parse_request("{not json")
        assert excinfo.value.kind == "MALFORMED"

    def test_non_object_rejected(self):
        with pytest.raises(ProtocolError) as excinfo:
            parse_request(json.dumps(["PING"]))
        assert excinfo.value.kind == "MALFORMED"

    def test_empty_line_rejected(self):
        with pytest.raises(ProtocolError) as excinfo:
            parse_request("   \n")
        assert excinfo.value.kind == "MALFORMED"

    def test_missing_or_bad_request_id_rejected(self):
        with pytest.raises(ProtocolError):
            parse_request(json.dumps({"type": "PING"}))
        with pytest.raises(ProtocolError):
            parse_request(json.dumps({"type": "PING", "request_id": ""}))
        with pytest.raises(ProtocolError):
            parse_request(json.dumps({"type": "PING", "request_id": 42}))
        with pytest.raises(ProtocolError):
            parse_request(json.dumps({"type": "PING", "request_id": "x" * 200}))

    def test_oversized_line_rejected(self):
        huge = json.dumps({"type": "PING", "request_id": "r" + "x" * (MAX_LINE_BYTES)})
        with pytest.raises(ProtocolError) as excinfo:
            parse_request(huge)
        assert excinfo.value.kind == "OVERSIZED"
        with pytest.raises(ProtocolError) as excinfo:
            parse_request(b"x" * (MAX_LINE_BYTES + 1))
        assert excinfo.value.kind == "OVERSIZED"

    def test_non_utf8_bytes_rejected(self):
        with pytest.raises(ProtocolError) as excinfo:
            parse_request(b"\xff\xfe\xfa")
        assert excinfo.value.kind == "MALFORMED"


class TestResponses:
    def test_ok_response_serialization(self):
        line = ok_response("r1", {"a": 1}).to_line()
        data = json.loads(line)
        assert data["protocol_version"] == 1
        assert data["request_id"] == "r1"
        assert data["ok"] is True
        assert data["payload"] == {"a": 1}
        assert line.endswith("\n")

    def test_error_response_serialization(self):
        line = error_response("r1", "OVERSIZED", "too big").to_line()
        data = json.loads(line)
        assert data["ok"] is False
        assert data["error"]["kind"] == "OVERSIZED"
        assert data["error"]["message"] == "too big"

    def test_error_response_without_request_id(self):
        data = json.loads(error_response(None, "MALFORMED", "bad").to_line())
        assert data["request_id"] is None
