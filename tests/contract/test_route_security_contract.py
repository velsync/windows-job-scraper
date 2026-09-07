"""Route security contract tests (S0.5/S0.6).

Proves the *classification* of every route in the Slice 0 service under the
security model: exactly the two declared public routes, the launcher channel,
the bootstrap exchange, and fail-closed defaults for everything unknown.
"""

import pytest

from jobscraper.service.app import classify_request


class TestPublicRoutes:
    def test_liveness_public_non_mutation(self):
        policy = classify_request("GET", "/health/live")
        assert policy.public is True
        assert policy.mutation is False
        assert policy.csrf_required is False

    def test_bootstrap_shell_public(self):
        policy = classify_request("GET", "/")
        assert policy.public is True
        assert policy.mutation is False


class TestLauncherChannel:
    def test_launcher_bootstrap_ticket_channel(self):
        policy = classify_request("POST", "/__launcher/bootstrap-ticket")
        assert policy.launcher_channel is True
        assert policy.public is False
        assert policy.csrf_required is False  # launcher proof is the auth

    def test_bootstrap_exchange_requires_exact_origin(self):
        policy = classify_request("POST", "/api/bootstrap")
        assert policy.public is False


class TestProtectedRoutes:
    @pytest.mark.parametrize(
        "path", ["/app", "/api/events", "/api/events/stream", "/jobs", "/api/jobs"]
    )
    def test_reads_require_session(self, path):
        policy = classify_request("GET", path)
        assert policy.public is False
        assert policy.mutation is False

    def test_logout_is_mutation_with_csrf(self):
        policy = classify_request("POST", "/api/session/logout")
        assert policy.mutation is True
        assert policy.csrf_required is True

    def test_unknown_post_is_mutation(self):
        policy = classify_request("POST", "/api/unknown")
        assert policy.mutation is True
        assert policy.csrf_required is True

    def test_unknown_delete_is_mutation(self):
        policy = classify_request("DELETE", "/anything")
        assert policy.mutation is True
        assert policy.csrf_required is True

    def test_no_product_routes_in_slice0(self):
        for path in ("/api/jobs", "/api/profiles", "/api/sources", "/api/runs"):
            # classify is fail-closed, but the route itself must not exist.
            policy = classify_request("GET", path)
            assert policy.public is False
