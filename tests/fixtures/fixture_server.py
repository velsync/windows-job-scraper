"""Local HTTP fixture server for integration tests.

Runs on loopback only. The trusted-host test policy explicitly permits this
server (jobscraper.security.netpolicy.make_test_fixture_policy).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FixtureHandler(BaseHTTPRequestHandler):
    """Routes are registered on the server instance under `routes`."""

    def log_message(self, *args):  # silence
        pass

    def _dispatch(self):
        routes = getattr(self.server, "routes", {})
        key = (self.command, self.path.split("?")[0])
        handler = routes.get(key)
        if handler is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        body = b""
        if length := int(self.headers.get("Content-Length") or 0):
            body = self.rfile.read(length)
        status, content_type, payload, extra_headers = handler(self, body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        payload_bytes = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        if status != 304:
            self.send_header("Content-Length", str(len(payload_bytes)))
        self.end_headers()
        if status != 304:
            self.wfile.write(payload_bytes)

    do_GET = _dispatch
    do_POST = _dispatch
    do_HEAD = _dispatch


class FixtureServer:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self._server.routes = {}
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def add(self, method: str, path: str, handler) -> None:
        self._server.routes[(method, path)] = handler

    def add_json(self, path: str, payload, status: int = 200, headers: dict | None = None):
        self.add(
            "GET",
            path,
            lambda req, body: (
                status,
                "application/json",
                json.dumps(payload),
                headers or {},
            ),
        )

    def add_html(self, path: str, html: str, status: int = 200, headers: dict | None = None):
        self.add(
            "GET",
            path,
            lambda req, body: (status, "text/html; charset=utf-8", html, headers or {}),
        )

    def start(self) -> "FixtureServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FixtureServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
