"""Service-mode entry point: bind loopback, serve, publish descriptor.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.8;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md section 4.1
(collision-safe port allocation: bind loopback (including OS-assigned
ephemeral port) and only then publish the selected port — no
probe-close-bind).

The service binds ``127.0.0.1:0`` before serving, learns the OS-assigned
port from the bound socket, and publishes the signed runtime descriptor only
after the uvicorn listener is live.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

from jobscraper.config import AppConfig
from jobscraper.db.backup import create_backup_generation
from jobscraper.db.connection import Database
from jobscraper.db.migrations import open_database_at_latest
from jobscraper.diagnostics.events import append_event, event
from jobscraper.paths import ensure_app_directories
from jobscraper.runtime.capacity import configure_service_capacity
from jobscraper.security.install_secret import load_or_create_install_secret
from jobscraper.service.app import create_service_app
from jobscraper.service.lifespan import ServiceLifespan
from jobscraper.timeutil import utc_now_s

PUBLISH_POLL_S = 0.05
PUBLISH_TIMEOUT_S = 30.0
RECOVERY_FAILURE_EXIT_CODE = 4


def _bind_loopback_socket(host: str) -> socket.socket:
    """Bind a loopback TCP socket with an OS-assigned port.

    The socket is bound but not yet listening; uvicorn completes the listen
    when it adopts it. There is no probe-close-bind sequence: the port is
    owned by this socket from bind() onward.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
    except OSError:
        sock.close()
        raise
    return sock


def open_service_database(config: AppConfig) -> Database:
    """Open (and migrate if needed, with the mandatory backup gate) the
    service database."""
    paths = config.paths

    def _backup(kind: str):
        holder = _BackupConnection(paths.database_file)
        try:
            return create_backup_generation(paths, holder.conn, kind=kind)
        finally:
            holder.close()

    return open_database_at_latest(paths.database_file, create_backup=_backup)


class _BackupConnection:
    """Short-lived connection used only for SQLite-consistent backup capture."""

    def __init__(self, path) -> None:
        from jobscraper.db.connection import connect_db

        self.conn = connect_db(path)

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # pragma: no cover
            pass
        self.conn.close()


def run_service(config: AppConfig, *, install_secret: bytes | None = None) -> int:
    """Run the local service process. Returns a process exit code."""
    import uvicorn

    paths = config.paths
    ensure_app_directories(paths)
    secret = install_secret or load_or_create_install_secret(paths)

    # S3.3: install the one process-lifetime coordinator before opening any
    # service resource.  A configuration refusal therefore cannot leak an
    # already-open database connection. Executors receive reservations; they
    # never own durable claims or capacity policy.
    configure_service_capacity(config)
    db = open_service_database(config)
    try:
        sock = _bind_loopback_socket(config.loopback_host)
    except OSError as exc:
        append_event(
            db.conn,
            event("ERROR", "SERVICE_BIND_FAILED", f"loopback bind failed: {exc}", data={}),
        )
        db.close()
        return 3
    port = int(sock.getsockname()[1])

    # Restart recovery (03 RUN-07/RUN-09, §18/§50): open the fresh service
    # epoch FIRST (recording any still-open epoch as ended by
    # SERVICE_RESTART). The service app requires a live service epoch —
    # fail-closed (§50) — so the epoch must precede create_service_app;
    # the guard minted there then covers every service claim for the
    # lifetime of this process.
    from jobscraper.runtime.clock import begin_service_epoch
    from jobscraper.runtime.recovery import recover_interrupted_requests

    service_epoch = begin_service_epoch(db.conn)

    app, state = create_service_app(config, db, port=port, secret=secret)
    lifespan = ServiceLifespan(config, db, secret)
    app.state.lifespan = lifespan

    # Then reclaim the orphaned RUNNING requests and finalize any
    # cancellation the crash interrupted. This is a correctness gate, not
    # best-effort diagnostics: after the epoch advances, old RUNNING owners
    # are invalid and must be durably reclaimed before the service can serve
    # or accept new acquisition work (§50/RUN-19).
    try:
        recovered = recover_interrupted_requests(db.conn)
        if recovered["reclaimed"] or recovered["finalized_cancelled_runs"]:
            append_event(
                db.conn,
                event(
                    "INFO",
                    "SERVICE_RECOVERY",
                    "restart recovery reclaimed orphaned requests",
                    data={
                        "service_epoch_id": service_epoch.epoch_id,
                        "reclaimed_requests": len(recovered["reclaimed"]),
                        "finalized_cancelled_runs": len(
                            recovered["finalized_cancelled_runs"]
                        ),
                    },
                ),
            )
    except Exception as exc:
        append_event(
            db.conn,
            event(
                "ERROR",
                "SERVICE_RECOVERY_FAILED",
                f"restart recovery failed: {exc}",
                data={"error_type": type(exc).__name__},
            ),
        )
        # The socket is bound but uvicorn has not adopted/listened on it yet.
        # Fail closed: do not provision/serve with invalidated prior-epoch
        # RUNNING work still unreconciled.
        sock.close()
        db.close()
        return RECOVERY_FAILURE_EXIT_CODE

    # S2.3 (01 §45): provision the search surface (FTS5 when the host has it,
    # an honest SUBSTRING_FALLBACK record otherwise).  Capability-gated and
    # idempotent; never blocks boot.
    from jobscraper.search.provision import provision_search

    try:
        provision_search(db.conn, now=utc_now_s())
    except Exception as exc:  # pragma: no cover - defensive
        append_event(
            db.conn,
            event(
                "ERROR",
                "SERVICE_SEARCH_PROVISION_FAILED",
                f"search provisioning failed: {exc}",
                data={"error_type": type(exc).__name__},
            ),
        )

    config_uv = uvicorn.Config(
        app,
        log_level="warning",
        # No access log: request paths must not land in ordinary logs (the
        # bootstrap ticket lives in a URL fragment and is never sent, but the
        # default policy is still to not write request logs at all).
        access_log=False,
        # The service binds a loopback socket itself; uvicorn only adopts it.
        host=None,
        port=None,
    )
    server = uvicorn.Server(config_uv)

    async def _publish_when_started() -> None:
        waited = 0.0
        while not server.started and not server.should_exit:
            await asyncio.sleep(PUBLISH_POLL_S)
            waited += PUBLISH_POLL_S
            if waited > PUBLISH_TIMEOUT_S:  # pragma: no cover - defensive
                server.should_exit = True
                return
        if server.started:
            desc = lifespan.build_descriptor(
                service_instance_id=state.instance_id,
                host=config.loopback_host,
                port=port,
                service_epoch="epoch-" + uuid.uuid4().hex[:12],
            )
            lifespan.publish_descriptor(desc)

    async def _on_shutdown() -> None:
        """App-level shutdown hook: uvicorn runs this during graceful
        shutdown, BEFORE serve() returns. (uvicorn re-raises captured signals
        after restoring their handlers, so cleanup that waits for serve() to
        return would never run on SIGTERM/SIGINT.)"""
        await lifespan.stop_background()
        if server.started:
            # Clean shutdown: invalidate the descriptor before exit.
            lifespan.remove_descriptor()
            lifespan.emit_stopped()
        else:
            lifespan.emit_crashed("server stopped before becoming live")
        try:
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # pragma: no cover
            pass

    app.router.add_event_handler("shutdown", _on_shutdown)

    async def _main() -> None:
        await lifespan.start_background()
        publisher = asyncio.create_task(_publish_when_started())
        try:
            await server.serve(sockets=[sock])
        finally:
            publisher.cancel()
            try:
                await publisher
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown path
                pass
            # Non-signal exits (exceptions before shutdown events): defensive
            # idempotent cleanup.
            lifespan.remove_descriptor()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:  # pragma: no cover - signal path
        pass
    finally:
        db.close()
    return 0
