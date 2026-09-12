#!/usr/bin/env python3
"""Authoritative automated Slice-3 acceptance gate (S3.13).

The gate orchestrates accepted test owners instead of duplicating their
assertions.  It preserves Slice-0/1/2 verifier semantics, runs the complete
focused Slice-3 matrix, then runs the repository-wide regression suite and
dependency closure check.

This is source-level evidence.  Exact-SHA CI, packaged-build verification,
native W3 execution and promotion closure remain separate evidence gates.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

PRIOR_GATES = (
    ("Slice-0 regression gate", REPO_ROOT / "scripts" / "verify_slice0.py"),
    ("Slice-1 regression gate", REPO_ROOT / "scripts" / "verify_slice1.py"),
    ("Slice-2 regression gate", REPO_ROOT / "scripts" / "verify_slice2.py"),
)

FOCUSED_SLICE3 = (
    # Contract + schema/migration authority.
    "tests/contract/test_slice3_contract.py",
    "tests/integration/test_schema_slice3.py",

    # Queue / lease / ownership / authorization / capacity / rate / locking.
    "tests/integration/test_runtime_core.py",
    "tests/integration/test_slice3_ownership.py",
    "tests/integration/test_s31_corrective3.py",
    "tests/integration/test_s32_authorization_fence.py",
    "tests/unit/test_s33_capacity.py",
    "tests/integration/test_s33_claim_deferral.py",
    "tests/unit/test_s34_retry_rate.py",
    "tests/integration/test_s34_rate_cancellation.py",
    "tests/integration/test_s3_lock_contention.py",
    "tests/integration/test_n3a_clock_checkpoint.py",

    # Generic crawler / cursor / frontier / robots / sitemap / revalidation.
    "tests/integration/test_s35_generic_http_crawler.py",
    "tests/unit/test_crawler_budget.py",
    "tests/unit/test_crawler_canonicalize.py",
    "tests/unit/test_crawler_cursor.py",
    "tests/unit/test_crawler_frontier.py",
    "tests/unit/test_crawler_pagination.py",
    "tests/unit/test_crawler_scope.py",
    "tests/unit/test_crawler_robots.py",
    "tests/integration/test_s36_sitemap.py",
    "tests/unit/test_crawler_sitemap.py",
    "tests/integration/test_s37_revalidation.py",
    "tests/unit/test_crawler_revalidation.py",

    # Coverage / fallback / temporal projection / local obligations.
    "tests/integration/test_s38_coverage.py",
    "tests/integration/test_s39_fallback_groups.py",
    "tests/integration/test_s310_availability.py",
    "tests/integration/test_obligations_evaluation.py",

    # Real driver + crash/recovery coverage, including the final vertical.
    "tests/integration/test_run_driver.py",
    "tests/integration/test_recovery.py",
    "tests/integration/test_s312_recovery.py",
    "tests/integration/test_slice3_acceptance.py",
)


def log(message: str) -> None:
    print(f"[slice3-gate] {message}", flush=True)


def run_gate(name: str, command: list[str], *, env: dict[str, str]) -> bool:
    log(f"{name}: {' '.join(command)}")
    proc = subprocess.run(command, cwd=str(REPO_ROOT), env=env)
    ok = proc.returncode == 0
    log(f"{name}: {'PASS' if ok else 'FAIL'} (exit {proc.returncode})")
    return ok


def required_paths_missing() -> list[str]:
    required = list(FOCUSED_SLICE3)
    required.extend(str(path.relative_to(REPO_ROOT)) for _, path in PRIOR_GATES)
    return [rel for rel in required if not (REPO_ROOT / rel).is_file()]


def main() -> int:
    missing = required_paths_missing()
    if missing:
        for rel in missing:
            log(f"MISSING required gate input: {rel}")
        return 2

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["WJS_SUPPRESS_BROWSER_OPEN"] = "1"

    results: list[tuple[str, bool]] = []

    # Preserve the accepted meaning of every earlier slice verifier.
    for name, script in PRIOR_GATES:
        ok = run_gate(name, [sys.executable, str(script)], env=env)
        results.append((name, ok))

    # S3.13 focused ownership matrix.  test_schema_slice3 includes the
    # promoted-v14 -> current migration proof added by this package.
    focused_name = "Slice-3 focused matrix"
    focused_cmd = [sys.executable, "-m", "pytest", "-q", *FOCUSED_SLICE3]
    results.append((focused_name, run_gate(focused_name, focused_cmd, env=env)))

    # Final source-tree regression across every committed test family.
    full_name = "Full repository test suite"
    full_cmd = [sys.executable, "-m", "pytest", "-q", "tests"]
    results.append((full_name, run_gate(full_name, full_cmd, env=env)))

    dep_name = "Dependency closure"
    dep_cmd = [sys.executable, "-m", "pip", "check"]
    results.append((dep_name, run_gate(dep_name, dep_cmd, env=env)))

    log("summary:")
    for name, ok in results:
        log(f"  {'PASS' if ok else 'FAIL'}  {name}")

    failed = [name for name, ok in results if not ok]
    if failed:
        log(
            "Slice-3 automated gate FAILED; candidate freeze/native promotion "
            "is blocked."
        )
        return 1

    log(
        "Slice-3 automated gate PASS. This is source-level evidence only; "
        "exact-SHA CI, packaged-build verification, W0/W1/W2/W3 native "
        "evidence and promotion closure are still required."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
