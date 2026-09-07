"""Launcher entry point: the product's double-click main.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.8;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md sections 4/WIN-04.

Owns the product lifecycle:

    acquire single-instance mutex
    → attach to a live service if one exists (second launch)
    → otherwise spawn the service, wait for a valid descriptor, recover
      bounded from service death
    → request a one-time bootstrap ticket and open the dashboard
    → supervise until shutdown; on launcher shutdown, stop the service

Unexpected failures become durable events (never silently swallowed), and the
install secret/tickets never appear in events or ordinary logs.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

from jobscraper.config import AppConfig, config_from_env
from jobscraper.diagnostics.events import append_event, event
from jobscraper.launcher.lifecycle import (
    open_dashboard,
    request_bootstrap_ticket,
    start_service_process,
    wait_for_valid_service,
)
from jobscraper.launcher.runtime_descriptor import (
    DescriptorError,
    remove_runtime_descriptor,
)
from jobscraper.launcher.single_instance import acquire_single_instance
from jobscraper.paths import ensure_app_directories
from jobscraper.security.install_secret import load_or_create_install_secret

MAX_SERVICE_RESTARTS = 3
RESTART_BACKOFF_S = 1.0
SUPERVISE_POLL_S = 0.2
SERVICE_STOP_TIMEOUT_S = 15.0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobscraper",
        description="Windows Job Scraper — local-first job-search workspace",
    )
    parser.add_argument("--data-root", type=Path, default=None, help="application data root")
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="print the dashboard URL to stdout (automation/acceptance aid)",
    )
    return parser


def _emit(config: AppConfig, level: str, kind: str, message: str, **data) -> None:
    """Best-effort durable event from the launcher (never fatal)."""
    try:
        from jobscraper.db.connection import connect_db

        conn = connect_db(config.paths.database_file)
        try:
            append_event(conn, event(level, kind, message, data=data or {}))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - diagnostics must not crash the launcher
        pass


def _load_secret(config: AppConfig) -> bytes:
    return load_or_create_install_secret(config.paths)


def _bootstrap_flow(config: AppConfig, desc, *, print_url: bool) -> int:
    """Launcher proof → one-time ticket → open dashboard. Returns exit code."""
    ticket = request_bootstrap_ticket(desc, _load_secret(config))
    url = open_dashboard(desc.port, ticket, host=desc.host)
    if print_url:
        # Automation/acceptance aid; the ticket is in the fragment and goes to
        # the launcher's own stdout, not to server request/event logs. stdout
        # is block-buffered when piped, so flush explicitly for consumers.
        print(f"Dashboard: {url}", flush=True)
    _emit(
        config,
        "INFO",
        "LAUNCHER_DASHBOARD_OPENED",
        "dashboard bootstrap ticket issued",
        service_instance_id=desc.service_instance_id,
        port=desc.port,
    )
    return 0


class _ShutdownIntent:
    """Cooperative shutdown flag set by launcher signals."""

    def __init__(self) -> None:
        self.requested = False
        self._old_handlers: dict = {}

    def install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old_handlers[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    def restore(self) -> None:
        for sig, handler in self._old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass
        self._old_handlers.clear()

    def _handle(self, signum, frame) -> None:  # noqa: ARG002
        self.requested = True


def _stop_service(config: AppConfig, proc: subprocess.Popen) -> None:
    """Stop the service we own: graceful terminate, bounded wait, force kill."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=SERVICE_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
        proc.wait()
    _emit(config, "INFO", "LAUNCHER_STOPPED_SERVICE", "launcher stopped the service cleanly")


def launch(config: AppConfig, *, print_url: bool = False) -> int:
    ensure_app_directories(config.paths)
    secret = _load_secret(config)

    ownership = acquire_single_instance(config.paths.runtime)
    if ownership.already_running:
        # Second launch: attach to the existing service if it is valid.
        from jobscraper.launcher.lifecycle import attach_with_retries

        desc = attach_with_retries(config, secret)
        if desc is None:
            _emit(
                config,
                "ERROR",
                "LAUNCHER_ATTACH_FAILED",
                "mutex owned but no valid service descriptor; owner is recovering or wedged",
            )
            print(
                "Windows Job Scraper is already starting or recovering; try again shortly.",
                file=sys.stderr,
            )
            return 1
        _emit(
            config,
            "INFO",
            "LAUNCHER_ATTACHED",
            "attached to existing service instance",
            service_instance_id=desc.service_instance_id,
            port=desc.port,
        )
        return _bootstrap_flow(config, desc, print_url=print_url)

    # First launch: own the lifecycle and supervise the service.
    try:
        return _run_as_owner(config, secret, print_url=print_url)
    finally:
        ownership.release()


def _run_as_owner(config: AppConfig, secret: bytes, *, print_url: bool) -> int:
    intent = _ShutdownIntent()
    intent.install()
    restarts = 0
    service_proc: subprocess.Popen | None = None
    try:
        while True:
            # A stale descriptor from a previous run (crash/forced kill) is
            # safe to clear only now that we own the lifecycle; its
            # PID/identity checks would reject it anyway.
            try:
                remove_runtime_descriptor(config.paths.runtime)
            except OSError:  # pragma: no cover
                pass

            _emit(
                config,
                "INFO",
                "LAUNCHER_STARTING_SERVICE",
                "spawning service process",
                attempt=restarts + 1,
            )
            try:
                service_proc = start_service_process(config)
            except OSError as exc:
                _emit(
                    config,
                    "ERROR",
                    "LAUNCHER_SPAWN_FAILED",
                    f"could not spawn service process: {exc}",
                )
                print(f"Failed to start the service: {exc}", file=sys.stderr)
                return 1

            try:
                desc = wait_for_valid_service(config, secret)
            except DescriptorError as exc:
                _emit(
                    config,
                    "ERROR",
                    "LAUNCHER_SERVICE_INVALID",
                    f"service did not become valid: {exc}",
                )
                service_proc.kill()
                service_proc.wait()
                return 1

            _emit(
                config,
                "INFO",
                "LAUNCHER_SERVICE_READY",
                "service is live and validated",
                service_instance_id=desc.service_instance_id,
                port=desc.port,
            )
            code = _bootstrap_flow(config, desc, print_url=print_url)
            if code != 0:
                # Terminate the service we started; do not leave an orphan.
                _stop_service(config, service_proc)
                return code

            # Supervise: on launcher shutdown stop the service; on service
            # death restart bounded.
            while True:
                if intent.requested:
                    _stop_service(config, service_proc)
                    return 0
                exit_code = service_proc.poll()
                if exit_code is None:
                    time.sleep(SUPERVISE_POLL_S)
                    continue
                if restarts >= MAX_SERVICE_RESTARTS:
                    _emit(
                        config,
                        "ERROR",
                        "LAUNCHER_GAVE_UP",
                        f"service died repeatedly (last exit {exit_code}); giving up",
                    )
                    return 1
                restarts += 1
                _emit(
                    config,
                    "WARN",
                    "LAUNCHER_RESTARTING_SERVICE",
                    f"service died (exit {exit_code}); restarting",
                    attempt=restarts,
                )
                time.sleep(min(RESTART_BACKOFF_S * restarts, 5.0))
                break  # spawn a fresh service
    finally:
        intent.restore()


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    config = config_from_env()
    if args.data_root is not None:
        config = config.with_data_root(args.data_root)
    return launch(config, print_url=args.print_url)
