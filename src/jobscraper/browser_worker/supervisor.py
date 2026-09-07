"""Browser-worker supervision from the service process.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.9;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md sections 4.2/4.3
(WIN-05: supervision owns lifecycle, restart/backoff, restart-rate ceiling,
process-tree cleanup).

The supervisor talks to the worker only over the typed stdin/stdout
protocol — the service process never imports Playwright and never runs
Chromium itself (ARC-06).
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from jobscraper.browser_worker.protocol import WorkerResponse

REQUEST_TIMEOUT_S = 120.0  # SMOKE can legitimately take tens of seconds
READINESS_TIMEOUT_S = 20.0
RESTART_BACKOFF_S = 1.0
MAX_RESTARTS = 5
RESTART_WINDOW_S = 300.0


class SupervisorError(Exception):
    pass


@dataclass
class SupervisorStats:
    spawns: int = 0
    restarts: int = 0
    last_error: str | None = None


class BrowserWorkerSupervisor:
    """Supervises one browser-worker child process (default capacity: 1)."""

    def __init__(self, *, on_event=None) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stdin = None
        self._responses: "queue.Queue[WorkerResponse | BaseException]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._stopping = False
        self._restart_times: list[float] = []
        self.stats = SupervisorStats()
        self._on_event = on_event or (lambda level, kind, message: None)

    # ------------------------------------------------------------- lifecycle
    def worker_command(self) -> list[str]:
        if getattr(sys, "frozen", False):  # pragma: no cover - packaged build
            return [sys.executable, "--browser-worker"]
        from jobscraper.procutils import child_python_executable

        return [child_python_executable(), "-m", "jobscraper", "--browser-worker"]

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._spawn_locked()

    def _spawn_locked(self) -> None:
        self._stopping = False
        from jobscraper.procutils import child_process_env

        proc = subprocess.Popen(
            self.worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=child_process_env(),
        )
        self._proc = proc
        self._stdin = proc.stdin
        self.stats.spawns += 1
        self._responses = queue.Queue()
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc,), name="wjs-bw-reader", daemon=True
        )
        self._reader.start()
        self._watchdog = threading.Thread(
            target=self._watch_loop, args=(proc,), name="wjs-bw-watchdog", daemon=True
        )
        self._watchdog.start()
        self._emit("INFO", "BROWSER_WORKER_STARTED", f"worker pid {proc.pid}")

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = __import__("json").loads(line)
                    response = WorkerResponse(
                        request_id=data.get("request_id"),
                        ok=bool(data.get("ok")),
                        payload=data.get("payload") or {},
                        error_kind=(data.get("error") or {}).get("kind"),
                        error_message=(data.get("error") or {}).get("message"),
                    )
                except (ValueError, AttributeError):
                    response = SupervisorError("worker sent a malformed response line")
                self._responses.put(response)
        finally:
            self._responses.put(SupervisorError("worker stdout closed"))

    def _watch_loop(self, proc: subprocess.Popen) -> None:
        """Restart the worker after death, with backoff and a rate ceiling."""
        proc.wait()
        with self._lock:
            if self._stopping or self._proc is not proc:
                return
            now = time.monotonic()
            self._restart_times = [t for t in self._restart_times if now - t < RESTART_WINDOW_S]
            if len(self._restart_times) >= MAX_RESTARTS:
                self._emit(
                    "ERROR",
                    "BROWSER_WORKER_GAVE_UP",
                    f"worker restarted {len(self._restart_times)} times in "
                    f"{RESTART_WINDOW_S:.0f}s; not restarting again",
                )
                self.stats.last_error = "restart ceiling reached"
                return
            self._restart_times.append(now)
            self.stats.restarts += 1
            backoff = min(RESTART_BACKOFF_S * len(self._restart_times), 10.0)
        time.sleep(backoff)
        with self._lock:
            if self._stopping or self._proc is not proc:
                return
            self._spawn_locked()

    def stop(self, *, timeout_s: float = 15.0) -> None:
        """Graceful SHUTDOWN, bounded wait, then terminate/kill."""
        with self._lock:
            proc = self._proc
            self._stopping = True
        if proc is None:
            return
        try:
            if proc.poll() is None:
                self._request_locked("SHUTDOWN", timeout_s=min(timeout_s, 5.0))
        except Exception:  # noqa: BLE001 - best-effort graceful path
            pass
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                proc.wait()
        self._emit("INFO", "BROWSER_WORKER_STOPPED", f"worker pid {proc.pid} exited ({proc.returncode})")

    # -------------------------------------------------------------- requests
    def _request_locked(self, msg_type: str, *, timeout_s: float = REQUEST_TIMEOUT_S) -> WorkerResponse:
        import secrets

        request_id = "req-" + secrets.token_hex(8)
        assert self._stdin is not None
        self._stdin.write(
            __import__("json").dumps({"type": msg_type, "request_id": request_id}) + "\n"
        )
        self._stdin.flush()
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SupervisorError(f"worker request {msg_type} timed out")
            try:
                item = self._responses.get(timeout=remaining)
            except queue.Empty:
                continue
            if isinstance(item, SupervisorError):
                raise item
            if item.request_id != request_id:
                continue  # stale response from a prior request
            return item

    def request(self, msg_type: str, *, timeout_s: float = REQUEST_TIMEOUT_S) -> WorkerResponse:
        """Send one request to a live worker and await its response."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise SupervisorError("browser worker is not running")
            return self._request_locked(msg_type, timeout_s=timeout_s)

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def worker_pid(self) -> int | None:
        with self._lock:
            if self._proc is None:
                return None
            return self._proc.pid

    def _emit(self, level: str, kind: str, message: str) -> None:
        try:
            self._on_event(level, kind, message)
        except Exception:  # noqa: BLE001 - events must not break supervision
            pass
