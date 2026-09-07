"""Tests for deterministic configuration (S0.1)."""

from pathlib import Path

from jobscraper.config import AppConfig, config_from_env


def test_defaults():
    cfg = AppConfig(data_root=Path("/tmp/x"))
    assert cfg.loopback_host == "127.0.0.1"
    assert cfg.busy_timeout_ms == 5000
    assert cfg.lease_window_s > cfg.heartbeat_interval_s > 0


def test_env_override_data_root(tmp_path):
    cfg = config_from_env({"WJS_DATA_ROOT": str(tmp_path)})
    assert cfg.data_root == tmp_path
    assert cfg.paths.root == tmp_path


def test_env_scheduler_disable():
    cfg = config_from_env({"WJS_DATA_ROOT": "/tmp/x", "WJS_SCHEDULER_ENABLED": "0"})
    assert cfg.scheduler_enabled is False


def test_with_data_root_rebuilds_paths(tmp_path):
    cfg = AppConfig(data_root=tmp_path / "a")
    cfg2 = cfg.with_data_root(tmp_path / "b")
    assert cfg2.paths.root == tmp_path / "b"
    assert cfg.paths.root == tmp_path / "a"


def test_config_frozen():
    cfg = AppConfig(data_root=Path("/tmp/x"))
    try:
        cfg.busy_timeout_ms = 1  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("AppConfig must be frozen")
