"""Browser-worker process main: stdin/stdout protocol loop.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.9.

Runs as ``python -m jobscraper --browser-worker`` (or the packaged executable
in ``--browser-worker`` mode). Reads newline-delimited JSON requests, writes
one response per request. Malformed requests produce typed errors, never a
crash. SHUTDOWN responds and exits 0.

This process imports no FastAPI/service objects and no product domain code;
Playwright is imported lazily only for SMOKE.
"""

from __future__ import annotations

import json
import sys

from jobscraper.browser_worker.protocol import (
    ProtocolError,
    WorkerRequest,
    error_response,
    ok_response,
    parse_request,
)
from jobscraper.browser_worker.playwright_runtime import (
    browser_runtime_status,
    run_inert_smoke,
)
from jobscraper.version import APP_VERSION


def handle_request(request: WorkerRequest):
    """Dispatch one parsed request to a response."""
    if request.type == "PING":
        return ok_response(request.request_id, {"pong": True})
    if request.type == "VERSION":
        status = browser_runtime_status()
        return ok_response(
            request.request_id,
            {
                "app_version": APP_VERSION,
                "worker_pid": _pid(),
                "python_version": _python_version(),
                **status.as_dict(),
            },
        )
    if request.type == "SMOKE":
        result = run_inert_smoke()
        if result.ok:
            return ok_response(request.request_id, result.as_dict())
        return error_response(
            request.request_id,
            result.error_kind or "SMOKE_FAILED",
            result.error_message or "inert smoke failed",
        )
    if request.type == "SHUTDOWN":
        return ok_response(request.request_id, {"shutting_down": True})
    # Unreachable: parse_request rejects unknown types.
    return error_response(request.request_id, "UNKNOWN_TYPE", request.type)  # pragma: no cover


def _pid() -> int:
    import os

    return os.getpid()


def _python_version() -> str:
    import platform

    return platform.python_version()


def main(stdin=None, stdout=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for raw_line in stdin:
        try:
            request = parse_request(raw_line)
        except ProtocolError as exc:
            response = error_response(None, exc.kind, exc.message)
            stdout.write(response.to_line())
            stdout.flush()
            continue
        response = handle_request(request)
        stdout.write(response.to_line())
        stdout.flush()
        if request.type == "SHUTDOWN":
            return 0
    # stdin closed without SHUTDOWN: exit cleanly (supervisor owns restart).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
