"""Deterministic application configuration.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md (S0.1);
docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 15 (policy
defaults) and section 50 (SQLite operating rules).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from jobscraper.paths import AppPaths, build_app_paths, default_data_root, ensure_app_directories


@dataclass(frozen=True)
class AppConfig:
    """Frozen application configuration.

    All timestamps are UTC; the loopback host is fixed. Values are tunable
    policy defaults (not invariants) unless the owning spec module marks them
    otherwise.
    """

    data_root: Path
    loopback_host: str = "127.0.0.1"
    busy_timeout_ms: int = 5000

    # Lease/queue policy defaults (RUN-07; module 03 section 15).
    lease_window_s: int = 120
    heartbeat_interval_s: int = 20
    stale_lease_grace_s: int = 15

    # HTTP executor defaults (module 03 section 15; module 04 section 5.1).
    http_timeout_s: float = 30.0
    http_max_bytes: int = 10 * 1024 * 1024
    http_max_redirects: int = 5
    http_max_concurrency: int = 8
    browser_max_concurrency: int = 1

    # Scheduler policy.
    scheduler_enabled: bool = True
    scheduler_poll_interval_s: int = 30

    # Event log retention (bounded, module 05 section 46).
    event_retention_days: int = 30
    event_list_default_limit: int = 100

    # Snapshot retention.
    snapshot_retention_days: int = 14
    snapshot_disk_budget_mb: int = 2048

    paths: AppPaths = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", build_app_paths(Path(self.data_root)))

    def with_data_root(self, root: Path) -> "AppConfig":
        return replace(self, data_root=Path(root))

    def ensure_directories(self) -> None:
        ensure_app_directories(self.paths)


def config_from_env(env: dict[str, str] | None = None) -> AppConfig:
    """Build configuration from the environment.

    Recognized variables:
      * ``WJS_DATA_ROOT`` — override application data root (tests, acceptance).
      * ``WJS_LOOPBACK_HOST`` — loopback bind host (default 127.0.0.1).
      * ``WJS_BUSY_TIMEOUT_MS`` — SQLite busy timeout.
      * ``WJS_SCHEDULER_ENABLED`` — "0"/"false" disables the local scheduler.
    """
    env = os.environ if env is None else env
    root = env.get("WJS_DATA_ROOT")
    data_root = Path(root) if root else default_data_root()
    cfg = AppConfig(data_root=data_root)
    if host := env.get("WJS_LOOPBACK_HOST"):
        cfg = replace(cfg, loopback_host=host)
    if busy := env.get("WJS_BUSY_TIMEOUT_MS"):
        cfg = replace(cfg, busy_timeout_ms=int(busy))
    if sched := env.get("WJS_SCHEDULER_ENABLED"):
        cfg = replace(cfg, scheduler_enabled=sched.strip().lower() not in {"0", "false", "no"})
    return cfg
