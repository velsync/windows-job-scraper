"""Browser-worker wire protocol v1 (newline-delimited JSON on stdin/stdout).

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.9;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md sections 4.2/4.3
(WIN-02, WIN-05).

Message types (worker receives):

    {"type": "PING",     "request_id": "..."}
    {"type": "VERSION",  "request_id": "..."}
    {"type": "SMOKE",    "request_id": "..."}
    {"type": "SHUTDOWN", "request_id": "..."}

Every response carries ``protocol_version``, the matching ``request_id``,
``ok`` and either a typed ``payload`` or a typed ``error``
(``{"kind": ..., "message": ...}``).

Malformed, unknown-typed and oversized messages are *rejected with typed
errors* — they must never crash the worker or leak exception details.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024
MAX_REQUEST_ID_LEN = 128

MESSAGE_TYPES = ("PING", "VERSION", "SMOKE", "SHUTDOWN")


class ProtocolError(Exception):
    """A typed protocol violation (malformed/unknown/oversized)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class WorkerRequest:
    type: str
    request_id: str


@dataclass(frozen=True)
class WorkerResponse:
    request_id: str | None
    ok: bool
    payload: dict | None = None
    error_kind: str | None = None
    error_message: str | None = None

    def to_line(self) -> str:
        body: dict = {"protocol_version": PROTOCOL_VERSION, "request_id": self.request_id, "ok": self.ok}
        if self.ok:
            body["payload"] = self.payload or {}
        else:
            body["error"] = {"kind": self.error_kind or "ERROR", "message": self.error_message or ""}
        return json.dumps(body, sort_keys=True) + "\n"


def parse_request(line: str | bytes) -> WorkerRequest:
    """Parse one wire line into a request; raise ProtocolError on violation."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise ProtocolError("OVERSIZED", f"message exceeds {MAX_LINE_BYTES} bytes")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("MALFORMED", "message is not valid UTF-8") from exc
    if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
        raise ProtocolError("OVERSIZED", f"message exceeds {MAX_LINE_BYTES} bytes")
    text = line.strip()
    if not text:
        raise ProtocolError("MALFORMED", "empty message")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProtocolError("MALFORMED", "message is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ProtocolError("MALFORMED", "message must be a JSON object")
    msg_type = data.get("type")
    if not isinstance(msg_type, str) or msg_type not in MESSAGE_TYPES:
        raise ProtocolError("UNKNOWN_TYPE", f"unknown message type: {msg_type!r}")
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("MALFORMED", "request_id must be a non-empty string")
    if len(request_id) > MAX_REQUEST_ID_LEN:
        raise ProtocolError("MALFORMED", f"request_id exceeds {MAX_REQUEST_LEN_REF} characters")
    # Reject unexpected extra structure defensively? Additional keys are
    # ignored (forward-compatible reads); type/request_id remain strict.
    return WorkerRequest(type=msg_type, request_id=request_id)


MAX_REQUEST_LEN_REF = MAX_REQUEST_ID_LEN


def error_response(request_id: str | None, kind: str, message: str) -> WorkerResponse:
    return WorkerResponse(request_id=request_id, ok=False, error_kind=kind, error_message=message)


def ok_response(request_id: str, payload: dict) -> WorkerResponse:
    return WorkerResponse(request_id=request_id, ok=True, payload=payload)
