#!/usr/bin/env python3
"""Slice 1 automated acceptance gate (S1.11).

Aggregates the Slice-1 verification evidence the plan requires — no test
logic is duplicated here:

    1. contract suite       tests/contract (slice boundary, security
                            shell, migration discipline, locks)
    2. full regression      tests/unit tests/contract tests/integration
                            (includes the Slice-0 acceptance suite and
                            the Slice-1 E2E acceptance:
                            tests/integration/test_slice1_acceptance.py
                            — full vertical path, restart persistence,
                            idempotent resume, mid-run cancellation,
                            crash-kill recovery)
    3. pip check            dependency closure intact
    4. doctor               Slice-0 operational gate on an initialized
                            isolated root (regression: Slice 1 must not
                            break the Slice-0 shell)

Exit code 0 only when every gate passes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


def log(message: str) -> None:
    print(f"[slice1-gate] {message}", flush=True)


def run_gate(name: str, command: list[str], *, env: dict | None = None) -> bool:
    log(f"{name}: {' '.join(command)}")
    proc = subprocess.run(command, cwd=str(REPO_ROOT), env=env)
    status = "PASS" if proc.returncode == 0 else "FAIL"
    log(f"{name}: {status} (exit {proc.returncode})")
    return proc.returncode == 0


def initialize_root(data_root: Path) -> None:
    """One real launcher run so the root has DB + secret (then stopped)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        # Windows has no cooperative SIGTERM via Popen. Create a dedicated
        # process group so CTRL_BREAK_EVENT reaches the launcher, which can
        # then stop its owned service and release the single-instance mutex.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    launcher = subprocess.Popen(
        [sys.executable, "-m", "jobscraper", "--data-root", str(data_root), "--print-url"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        **popen_kwargs,
    )
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            line = launcher.stdout.readline() if launcher.stdout else ""
            if line.startswith("Dashboard:"):
                return
            if launcher.poll() is not None:
                return
    finally:
        if launcher.poll() is None:
            if os.name == "nt":
                launcher.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                launcher.terminate()
            try:
                launcher.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                launcher.kill()
                launcher.wait()


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")

    results = {}
    results["contract"] = run_gate(
        "contract-suite",
        [sys.executable, "-m", "pytest", "tests/contract", "-q"],
        env=env,
    )
    results["acceptance_e2e"] = run_gate(
        "slice1-e2e-acceptance",
        [sys.executable, "-m", "pytest", "tests/integration/test_slice1_acceptance.py", "-q"],
        env=env,
    )
    results["tests"] = run_gate(
        "full-regression",
        [
            sys.executable, "-m", "pytest",
            "tests/unit", "tests/contract", "tests/integration", "-q",
        ],
        env=env,
    )
    results["pip_check"] = run_gate("pip-check", [sys.executable, "-m", "pip", "check"], env=env)

    with tempfile.TemporaryDirectory(prefix="wjs-slice1-gate-") as root_name:
        root = Path(root_name)
        log("initializing isolated doctor root with one launcher run")
        initialize_root(root)
        results["doctor"] = run_gate(
            "doctor",
            [sys.executable, "-m", "jobscraper", "--doctor", "--data-root", str(root)],
            env=env,
        )

    aggregate = all(results.values())
    log(
        "gate summary: "
        + json.dumps({k: ("PASS" if v else "FAIL") for k, v in results.items()})
    )
    log("SLICE 1 AUTOMATED GATE: " + ("PASS" if aggregate else "FAIL"))
    return 0 if aggregate else 1


if __name__ == "__main__":
    raise SystemExit(main())
