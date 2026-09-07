"""Health check result model and Doctor helpers.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.10;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md WIN-09 (Doctor
minimum checks; Doctor is diagnostic and must not silently mutate user data).
"""

from __future__ import annotations

from dataclasses import dataclass, field

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# A check whose subject has not been provisioned yet (fresh install) is an
# actionable WARN, not a hard failure; broken/provisioned-but-unusable is FAIL.
_STATUS_ORDER = {PASS: 0, WARN: 1, FAIL: 2}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    summary: str
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in (PASS, WARN, FAIL):
            raise ValueError(f"invalid check status: {self.status!r}")

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


def doctor_exit_code(results: list[CheckResult]) -> int:
    """0 when no check failed (warnings allowed); 1 on any FAIL."""
    return 1 if any(r.status == FAIL for r in results) else 0


def doctor_json(results: list[CheckResult]) -> dict:
    return {
        "schema_version": 1,
        "checks": [r.as_dict() for r in results],
        "failed": sum(1 for r in results if r.status == FAIL),
        "warned": sum(1 for r in results if r.status == WARN),
        "exit_code": doctor_exit_code(results),
    }


def format_doctor_report(results: list[CheckResult]) -> str:
    lines = []
    for result in results:
        marker = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[result.status]
        lines.append(f"{marker} {result.name}: {result.summary}")
    exit_code = doctor_exit_code(results)
    lines.append("")
    lines.append(f"doctor exit code: {exit_code}")
    return "\n".join(lines)
