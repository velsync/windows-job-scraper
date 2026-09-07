"""Service process lifecycle wiring.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.8;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md sections 4/WIN-04.

Owns the service-side lifecycle actions around the HTTP server:

* publish/remove the signed runtime descriptor (only after the listener is
  live — enforced by the runner's publisher task);
* durable, redacted lifecycle events (SERVICE_STARTED / SERVICE_STOPPED /
  SERVICE_CRASHED / recovery outcomes);
* supervised child resources (the browser worker joins in S0.9).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from jobscraper.config import AppConfig
from jobscraper.db.connection import Database
from jobscraper.diagnostics.events import append_event, event
from jobscraper.launcher.runtime_descriptor import (
    RuntimeDescriptor,
    process_start_identity,
    remove_runtime_descriptor,
    write_runtime_descriptor,
)
from jobscraper.timeutil import utc_now_s


@dataclass
class ServiceLifespan:
    config: AppConfig
    db: Database
    secret: bytes
    supervisor: object | None = None  # browser-worker supervisor (S0.9)
    _current_descriptor: RuntimeDescriptor | None = field(default=None, repr=False)

    @property
    def runtime_dir(self) -> Path:
        return self.config.paths.runtime

    def build_descriptor(
        self, *, service_instance_id: str, host: str, port: int, service_epoch: str
    ) -> RuntimeDescriptor:
        from jobscraper.launcher.runtime_descriptor import complete_descriptor, make_descriptor

        desc = make_descriptor(
            service_instance_id=service_instance_id,
            pid=os.getpid(),
            process_start_identity=process_start_identity(os.getpid()),
            host=host,
            port=port,
            service_epoch=service_epoch,
            created_at_utc=utc_now_s(),
        )
        return complete_descriptor(desc, self.secret)

    def publish_descriptor(self, desc: RuntimeDescriptor) -> None:
        """Publish the signed descriptor (caller must only invoke this after
        the loopback listener is live)."""
        write_runtime_descriptor(self.runtime_dir, desc)
        self._current_descriptor = desc
        append_event(
            self.db.conn,
            event(
                "INFO",
                "SERVICE_STARTED",
                "service listener live; runtime descriptor published",
                data={
                    "service_instance_id": desc.service_instance_id,
                    "port": desc.port,
                    "pid": desc.pid,
                },
            ),
        )

    def remove_descriptor(self) -> None:
        remove_runtime_descriptor(self.runtime_dir)
        self._current_descriptor = None

    def emit_stopped(self, reason: str = "clean shutdown") -> None:
        append_event(
            self.db.conn,
            event("INFO", "SERVICE_STOPPED", f"service stopped: {reason}", data={}),
        )

    def emit_crashed(self, detail: str) -> None:
        # detail passes through central redaction; keep it factual.
        append_event(
            self.db.conn,
            event("ERROR", "SERVICE_CRASHED", f"service aborted: {detail}", data={}),
        )

    async def start_background(self) -> None:
        """Start supervised background resources (browser worker in S0.9)."""
        if self.supervisor is not None:
            await self.supervisor.start()

    async def stop_background(self) -> None:
        """Stop supervised background resources; failures become events, not
        silent exceptions (never swallow-and-pass)."""
        if self.supervisor is None:
            return
        try:
            await self.supervisor.stop()
        except Exception as exc:
            append_event(
                self.db.conn,
                event(
                    "ERROR",
                    "SUPERVISOR_STOP_FAILED",
                    "background supervisor stop failed",
                    data={"error_type": type(exc).__name__, "error": str(exc)[:300]},
                ),
            )
