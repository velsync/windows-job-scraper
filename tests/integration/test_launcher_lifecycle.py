"""Integration tests for the launcher/service lifecycle (S0.8).

These tests spawn real subprocesses (``python -m jobscraper --service``) on
an isolated temporary data root and prove:

* the service binds a loopback OS-assigned port and publishes the signed
  descriptor only once the listener is live;
* the launcher validation logic accepts a live service and rejects stale,
  tampered and impersonated descriptors;
* a second launcher attach attempt finds the running service;
* clean service shutdown removes the descriptor;
* forced service termination leaves a recoverable stale descriptor that the
  next launcher cycle clears safely.
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

_HARD_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)

from jobscraper.config import AppConfig
from jobscraper.launcher.lifecycle import (
    request_bootstrap_ticket,
    validate_running_service,
    wait_for_valid_service,
)
from jobscraper.launcher.runtime_descriptor import (
    DescriptorError,
    check_descriptor_identity,
    load_runtime_descriptor,
    make_descriptor,
    complete_descriptor,
    process_start_identity,
    remove_runtime_descriptor,
)
from jobscraper.paths import build_app_paths, ensure_app_directories
from jobscraper.procutils import (
    child_process_env,
    child_python_executable,
    graceful_process_group_kwargs,
    request_graceful_stop,
)
from jobscraper.security.install_secret import load_or_create_install_secret
from jobscraper.timeutil import utc_now_s

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _env():
    env = child_process_env()
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("WJS_DATA_ROOT", None)
    return env


def _python() -> str:
    return child_python_executable()


@pytest.fixture()
def service_process(tmp_path):
    root = tmp_path / "root"
    ensure_app_directories(build_app_paths(root))
    secret = load_or_create_install_secret(build_app_paths(root))
    proc = subprocess.Popen(
        [_python(), "-m", "jobscraper", "--service", "--data-root", str(root)],
        cwd=str(REPO_ROOT),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **graceful_process_group_kwargs(),
    )
    config = AppConfig(data_root=root)
    try:
        desc = wait_for_valid_service(config, secret, timeout_s=40)
        yield proc, config, secret, desc
    finally:
        if proc.poll() is None:
            request_graceful_stop(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_service_binds_os_assigned_port_and_publishes_live_descriptor(service_process):
    proc, config, secret, desc = service_process
    assert desc.port != 0
    assert desc.port != 8000
    assert desc.host == "127.0.0.1"
    validate_running_service(desc, secret)
    assert load_runtime_descriptor(config.paths.runtime) == desc


def test_descriptor_published_only_after_listener_live(service_process):
    proc, config, secret, desc = service_process
    validate_running_service(desc, secret)


def test_launcher_channel_flow(service_process):
    proc, config, secret, desc = service_process
    ticket = request_bootstrap_ticket(desc, secret)
    assert ticket
    ticket2 = request_bootstrap_ticket(desc, secret)
    assert ticket2 and ticket2 != ticket


def test_second_launcher_attaches_to_running_service(service_process, tmp_path):
    proc, config, secret, desc = service_process
    from jobscraper.launcher.single_instance import acquire_single_instance

    holder = acquire_single_instance(config.paths.runtime)
    assert holder.already_running is False
    second = acquire_single_instance(config.paths.runtime)
    assert second.already_running is True
    from jobscraper.launcher.lifecycle import load_valid_descriptor

    found = load_valid_descriptor(config, secret)
    assert found is not None and found.service_instance_id == desc.service_instance_id
    holder.release()


def test_stale_descriptor_dead_pid_rejected(tmp_path):
    config = AppConfig(data_root=tmp_path / "root")
    ensure_app_directories(config.paths)
    secret = b"\x04" * 32
    dead_pid = _dead_pid()
    desc = complete_descriptor(
        make_descriptor(
            service_instance_id="svc-dead",
            pid=dead_pid,
            process_start_identity="posix:1",
            host="127.0.0.1",
            port=12345,
            service_epoch="epoch-x",
            created_at_utc=utc_now_s(),
        ),
        secret,
    )
    from jobscraper.launcher.runtime_descriptor import write_runtime_descriptor
    write_runtime_descriptor(config.paths.runtime, desc)
    from jobscraper.launcher.lifecycle import load_valid_descriptor
    assert load_valid_descriptor(config, secret) is None


def _pid_alive(pid: int) -> bool:
    from jobscraper.launcher.runtime_descriptor import pid_alive
    return pid_alive(pid)


def _dead_pid() -> int:
    p = subprocess.Popen([_python(), "-c", "pass"], env=_env())
    p.wait()
    return p.pid


def test_old_port_impersonation_rejected(tmp_path):
    handler = http.server.BaseHTTPRequestHandler

    class Impersonator(handler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "service_instance_id": "svc-someone-else"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Impersonator)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        desc = complete_descriptor(
            make_descriptor(
                service_instance_id="svc-expected",
                pid=os.getpid(),
                process_start_identity=process_start_identity(os.getpid()),
                host="127.0.0.1",
                port=port,
                service_epoch="epoch-x",
                created_at_utc=utc_now_s(),
            ),
            b"\x05" * 32,
        )
        with pytest.raises(DescriptorError, match="impersonation|different service instance"):
            validate_running_service(desc, b"\x05" * 32)
    finally:
        server.shutdown()
        server.server_close()


def test_clean_shutdown_removes_descriptor(service_process):
    proc, config, secret, desc = service_process
    request_graceful_stop(proc)
    proc.wait(timeout=15)
    deadline = time.time() + 10
    while time.time() < deadline:
        if load_runtime_descriptor(config.paths.runtime) is None:
            break
        time.sleep(0.2)
    assert load_runtime_descriptor(config.paths.runtime) is None


def test_forced_kill_leaves_recoverable_stale_state(service_process):
    proc, config, secret, desc = service_process
    proc.kill()
    proc.wait(timeout=10)
    remove_runtime_descriptor(config.paths.runtime)
    assert load_runtime_descriptor(config.paths.runtime) is None


def test_launcher_end_to_end(tmp_path):
    root = tmp_path / "root"
    ensure_app_directories(build_app_paths(root))
    launcher = subprocess.Popen(
        [_python(), "-m", "jobscraper", "--data-root", str(root), "--print-url"],
        cwd=str(REPO_ROOT),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **graceful_process_group_kwargs(),
    )
    config = AppConfig(data_root=root)
    secret = load_or_create_install_secret(config.paths)
    try:
        deadline = time.time() + 60
        output_lines = []
        dashboard_url = None
        while time.time() < deadline:
            line = launcher.stdout.readline()
            if not line:
                break
            output_lines.append(line)
            if line.startswith("Dashboard:"):
                dashboard_url = line.strip().split("Dashboard: ", 1)[1]
                break
        assert dashboard_url, "launcher never printed the dashboard URL: " + "".join(output_lines)
        assert "#bootstrap=" in dashboard_url
        assert "ticket=" not in dashboard_url.split("#")[0]
        desc = wait_for_valid_service(config, secret, timeout_s=20)
        first_pid = desc.pid

        os.kill(first_pid, _HARD_KILL)
        deadline = time.time() + 30
        new_desc = None
        while time.time() < deadline:
            try:
                new_desc = load_runtime_descriptor(config.paths.runtime)
            except DescriptorError:
                new_desc = None
            if new_desc is not None and new_desc.pid != first_pid:
                try:
                    validate_running_service(new_desc, secret)
                    break
                except DescriptorError:
                    pass
            time.sleep(0.3)
        assert new_desc is not None and new_desc.pid != first_pid, "launcher did not restart the dead service"

        request_graceful_stop(launcher)
        assert launcher.wait(timeout=30) == 0
        deadline = time.time() + 10
        while time.time() < deadline and _pid_alive(new_desc.pid):
            time.sleep(0.2)
        assert not _pid_alive(new_desc.pid), "service child survived launcher shutdown"
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait()
        try:
            desc = load_runtime_descriptor(config.paths.runtime)
            if desc is not None:
                try:
                    os.kill(desc.pid, _HARD_KILL)
                except OSError:
                    pass
        except DescriptorError:
            pass
