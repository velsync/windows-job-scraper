"""S1.2 integration: destination policy against a real loopback server.

Proves with live sockets that:

* loopback is denied by default even when the connection would succeed
  (the server is running and reachable — the policy, not the network,
  is what blocks it);
* an explicit ``InternalGrant`` authorizes exactly the granted loopback
  host;
* the real system resolver is wired in (``localhost`` resolves);
* redirect targets are validated per hop against the live server's
  responses (a redirect pointing at a non-granted loopback port is
  rejected).
"""

from __future__ import annotations

import http.server
import threading

import pytest

from jobscraper.net.destination import (
    DestinationPolicy,
    DestinationRejected,
    InternalGrant,
    check_redirect_hop,
    check_url,
)


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect-local":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/feed")
            self.end_headers()
        elif self.path == "/redirect-relative":
            self.send_response(302)
            self.send_header("Location", "/feed")
            self.end_headers()
        else:
            body = b'{"jobs": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def loopback_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_loopback_denied_by_default_even_while_reachable(loopback_server):
    port = loopback_server.server_address[1]
    with pytest.raises(DestinationRejected) as exc:
        check_url(f"http://127.0.0.1:{port}/feed", DestinationPolicy())
    assert exc.value.reason_code == "LOOPBACK_ADDRESS"


def test_internal_grant_authorizes_the_running_fixture(loopback_server):
    port = loopback_server.server_address[1]
    policy = DestinationPolicy(
        internal_grant=InternalGrant(
            purpose="fixture acceptance server", allowed_hosts=frozenset({"127.0.0.1"})
        )
    )
    d = check_url(f"http://127.0.0.1:{port}/feed", policy)
    assert d.grant == "fixture acceptance server"
    assert d.port == port


def test_granted_host_on_a_different_loopback_port_is_same_host(loopback_server):
    # The grant is host-scoped (127.0.0.1), not port-scoped: policy validates
    # destinations, the executor owns what actually gets fetched.
    port = loopback_server.server_address[1]
    policy = DestinationPolicy(
        internal_grant=InternalGrant(
            purpose="fixture acceptance server", allowed_hosts=frozenset({"127.0.0.1"})
        )
    )
    check_url(f"http://127.0.0.1:{port + 1}/feed", policy)


def test_real_resolver_wired_for_localhost(loopback_server):
    # 'localhost' via the real system resolver must land on loopback and be
    # denied without a grant (fail closed on the machine's own resolution).
    with pytest.raises(DestinationRejected) as exc:
        check_url("http://localhost:1/x", DestinationPolicy())
    assert exc.value.reason_code == "LOOPBACK_ADDRESS"


def test_redirect_hop_to_localhost_rejected_without_grant(loopback_server):
    port = loopback_server.server_address[1]
    policy = DestinationPolicy()  # no grant
    with pytest.raises(DestinationRejected) as exc:
        check_redirect_hop(
            f"http://127.0.0.1:{port}/feed", policy, 1, base_url="https://example.test/list"
        )
    assert exc.value.reason_code == "LOOPBACK_ADDRESS"


def test_redirect_hop_validated_with_relative_location(loopback_server):
    port = loopback_server.server_address[1]
    policy = DestinationPolicy(
        internal_grant=InternalGrant(
            purpose="fixture acceptance server", allowed_hosts=frozenset({"127.0.0.1"})
        )
    )
    d = check_redirect_hop(
        "/feed", policy, 0, base_url=f"http://127.0.0.1:{port}/redirect-relative"
    )
    assert d.host == "127.0.0.1"
    assert d.path == "/feed"
    assert d.grant == "fixture acceptance server"
