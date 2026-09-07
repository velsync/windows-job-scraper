"""CI-only browser-worker smoke harness for Slice 0.

Runs the real ``--browser-worker`` process through its newline-delimited JSON
protocol, requires a successful inert Chromium smoke, then requires a clean
protocol shutdown. No network/job-source navigation is performed.
"""

from __future__ import annotations

import json
import subprocess
import sys


TIMEOUT_S = 90
PROTOCOL_VERSION = 1
SMOKE_REQUEST_ID = "ci-smoke"
SHUTDOWN_REQUEST_ID = "ci-shutdown"
EXPECTED_TITLE = "WJS-Inert-Smoke"


def _fail(message: str, *, stderr: str = "") -> int:
    print(f"CI browser smoke FAILED: {message}", file=sys.stderr)
    if stderr.strip():
        print(stderr[-2000:], file=sys.stderr)
    return 1


def _validate_response(data: object, *, request_id: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    if data.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError(f"unexpected protocol version: {data.get('protocol_version')!r}")
    if data.get("request_id") != request_id:
        raise ValueError(f"unexpected request_id: {data.get('request_id')!r}")
    if data.get("ok") is not True:
        raise ValueError(f"worker returned error: {data.get('error')!r}")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("successful response has no payload object")
    return payload


def main() -> int:
    requests = "".join(
        json.dumps({"type": msg_type, "request_id": request_id}) + "\n"
        for msg_type, request_id in (
            ("SMOKE", SMOKE_REQUEST_ID),
            ("SHUTDOWN", SHUTDOWN_REQUEST_ID),
        )
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "jobscraper", "--browser-worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(input=requests, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return _fail(f"worker did not complete within {TIMEOUT_S}s", stderr=stderr)

    if proc.returncode != 0:
        return _fail(f"worker exited with code {proc.returncode}", stderr=stderr)

    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return _fail(f"expected exactly 2 protocol responses, got {len(lines)}", stderr=stderr)

    try:
        smoke = json.loads(lines[0])
        shutdown = json.loads(lines[1])
        smoke_payload = _validate_response(smoke, request_id=SMOKE_REQUEST_ID)
        shutdown_payload = _validate_response(shutdown, request_id=SHUTDOWN_REQUEST_ID)
    except (ValueError, json.JSONDecodeError) as exc:
        return _fail(str(exc), stderr=stderr)

    if smoke_payload.get("ok") is not True:
        return _fail(f"SMOKE payload not successful: {smoke_payload!r}", stderr=stderr)
    if smoke_payload.get("page_title") != EXPECTED_TITLE:
        return _fail(f"unexpected inert page title: {smoke_payload.get('page_title')!r}", stderr=stderr)
    if not smoke_payload.get("chromium_version"):
        return _fail("SMOKE did not report Chromium version", stderr=stderr)
    if not smoke_payload.get("chromium_revision"):
        return _fail("SMOKE did not report Chromium revision", stderr=stderr)
    if smoke_payload.get("browser_exited_cleanly") is not True:
        return _fail("SMOKE did not prove browser cleanup", stderr=stderr)
    if shutdown_payload.get("shutting_down") is not True:
        return _fail(f"unexpected SHUTDOWN payload: {shutdown_payload!r}", stderr=stderr)

    print(
        "CI browser smoke PASS: "
        f"Chromium {smoke_payload['chromium_version']} / "
        f"{smoke_payload['chromium_revision']} / {smoke_payload['page_title']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
