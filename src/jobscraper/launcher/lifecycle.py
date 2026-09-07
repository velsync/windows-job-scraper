"""Launcher-side lifecycle: start/attach to the service, open the dashboard.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.8;
docs/spec/v0.3.1.3/04_security_and_authentication.md SEC-01 (launcher →
service → dashboard bootstrap); module 05 WIN-04.

Lifecycle sequence:

    launcher acquires named mutex
    → if first: spawn service
    → service binds 127.0.0.1:0 and publishes the signed descriptor
    → launcher validates descriptor + PID/start identity + health instance ID
    → launcher requests a one-time bootstrap ticket with a launcher proof
    → launcher opens http://127.0.0.1:<port>/#bootstrap=<ticket>

Stale descriptor, dead PID, PID reuse/start-identity mismatch, wrong HMAC and
old-port impersonation all fail validation and enter bounded recovery.
"""

from __future__ import annotations

import json
import secrets as _secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from jobscraper.config import AppConfig
from jobscraper.launcher.runtime_descriptor import (
    DescriptorError,
    RuntimeDescriptor,
    check_descriptor_identity,
    load_runtime_descriptor,
    service_health_instance_id,
)

SPAWN_TIMEOUT_S = 30.0
ATTACH_RETRY_ATTEMPTS = 10
ATTACH_RETRY_DELAY_S = 0.5


def service_command(config: AppConfig, mode: str) -> list[str]:
    """Command line to start this program in an internal mode.

    In a packaged (PyInstaller --onedir) build the executable re-invokes
    itself; in development the package runs via ``python -m jobscraper``.
    """
    if getattr(sys, "frozen", False):  # pragma: no cover - packaged build
        return [sys.executable, f"--{mode}", "--data-root", str(config.data_root)]
    from jobscraper.procutils import child_python_executable

    return [
        child_python_executable(),
        "-m",
        "jobscraper",
        f"--{mode}",
        "--data-root",
        str(config.data_root),
    ]


def start_service_process(config: AppConfig) -> subprocess.Popen:
    """Spawn the owned service in its own process group.

    The separate process group prevents launcher control signals from
    cascading into the service automatically and, on Windows, enables a
    process-directed CTRL_BREAK_EVENT for graceful Uvicorn shutdown.
    """
    from jobscraper.procutils import child_process_env, graceful_process_group_kwargs

    return subprocess.Popen(
        service_command(config, "service"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=child_process_env(),
        **graceful_process_group_kwargs(),
    )


def validate_running_service(
    desc: RuntimeDescriptor, secret: bytes, *, health_timeout_s: float = 2.0
) -> None:
    """Full validation: HMAC, liveness, start identity, health instance ID.

    Raises :class:`DescriptorError` with a factual reason on failure. The
    health-instance check defeats an unrelated listener that happens to be
    bound to a previously used port (old-port impersonation).
    """
    check_descriptor_identity(desc, secret)
    instance_id = service_health_instance_id(desc, timeout_s=health_timeout_s)
    if instance_id is None:
        raise DescriptorError("health endpoint unreachable or malformed")
    if instance_id != desc.service_instance_id:
        raise DescriptorError(
            "health endpoint reports a different service instance (impersonation)"
        )


def load_valid_descriptor(config: AppConfig, secret: bytes) -> RuntimeDescriptor | None:
    """Load and validate the current descriptor, or None when absent."""
    try:
        desc = load_runtime_descriptor(config.paths.runtime)
    except DescriptorError:
        return None
    if desc is None:
        return None
    try:
        validate_running_service(desc, secret)
    except DescriptorError:
        return None
    return desc


def wait_for_valid_service(
    config: AppConfig, secret: bytes, *, timeout_s: float = SPAWN_TIMEOUT_S
) -> RuntimeDescriptor:
    """Poll for a *valid, live* service descriptor (used after spawning)."""
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            desc = load_runtime_descriptor(config.paths.runtime)
        except DescriptorError as exc:
            last_error = exc
            desc = None
        if desc is not None:
            try:
                validate_running_service(desc, secret)
                return desc
            except DescriptorError as exc:
                last_error = exc
        time.sleep(0.1)
    raise DescriptorError(
        f"service did not become valid within {timeout_s}s: {last_error or 'no descriptor'}"
    )


def attach_with_retries(
    config: AppConfig, secret: bytes, *, attempts: int = ATTACH_RETRY_ATTEMPTS
) -> RuntimeDescriptor | None:
    """Bounded recovery when the mutex is already owned: the owner may be
    mid-restart, so retry validation briefly before giving up."""
    for _ in range(attempts):
        desc = load_valid_descriptor(config, secret)
        if desc is not None:
            return desc
        time.sleep(ATTACH_RETRY_DELAY_S)
    return None


def request_bootstrap_ticket(
    desc: RuntimeDescriptor,
    secret: bytes,
    *,
    issued_at: int | None = None,
    timeout_s: float = 5.0,
) -> str:
    """Request a one-time bootstrap ticket over the authenticated launcher
    channel (HMAC proof; the secret itself is never sent)."""
    from jobscraper.web.bootstrap import make_launcher_proof

    nonce = _secrets.token_hex(16)
    issued_at = int(time.time()) if issued_at is None else issued_at
    proof = make_launcher_proof(secret, desc.service_instance_id, nonce, issued_at)
    payload = json.dumps(
        {
            "instance_id": desc.service_instance_id,
            "nonce": nonce,
            "issued_at": issued_at,
            "proof": proof,
        }
    ).encode("utf-8")
    url = f"http://{desc.host}:{desc.port}/__launcher/bootstrap-ticket"
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise DescriptorError(f"launcher bootstrap request failed: {exc}") from exc
    ticket = body.get("ticket") if isinstance(body, dict) else None
    if not isinstance(ticket, str) or not ticket:
        raise DescriptorError("launcher bootstrap response contained no ticket")
    return ticket


def open_dashboard(port: int, ticket: str, *, host: str = "127.0.0.1") -> str:
    """Build and open the one-time bootstrap URL. The ticket is a fragment,
    never a query/path component, so it is not sent in HTTP request lines."""
    import webbrowser

    url = f"http://{host}:{int(port)}/#bootstrap={ticket}"
    webbrowser.open(url)
    return url
