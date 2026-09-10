"""Slice-2 native acceptance harness contract.

The real promotion run is Windows/package-only.  This test exercises the same
W2 orchestration against the development target so the acceptance harness
itself remains regression-tested in ordinary CI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_native_acceptance_harness_supports_slice2_dev(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/native_acceptance.py",
            "--target",
            "dev",
            "--slice",
            "2",
            "--evidence-dir",
            str(evidence),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    record = json.loads((evidence / "native-acceptance.json").read_text(encoding="utf-8"))
    assert record["slice"] == "2"
    assert record["promotion"] == "PENDING_NATIVE_RUN"
    assert set(record["tests"]) == {
        "W2-01",
        "W2-02",
        "W2-03",
        "W2-04",
        "W2-05",
        "W2-06",
    }
    assert set(record["tests"].values()) == {"PASS"}
