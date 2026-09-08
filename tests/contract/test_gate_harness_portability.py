"""Regression contract for Slice-0/1 gate launcher cleanup on Windows."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATES = (
    REPO_ROOT / "scripts" / "verify_slice0.py",
    REPO_ROOT / "scripts" / "verify_slice1.py",
)


def test_gate_harnesses_do_not_use_nonexistent_subprocess_sigterm():
    for path in GATES:
        text = path.read_text(encoding="utf-8")
        assert "subprocess.SIGTERM" not in text, path


def test_gate_harnesses_use_windows_process_group_and_ctrl_break():
    for path in GATES:
        text = path.read_text(encoding="utf-8")
        assert "subprocess.CREATE_NEW_PROCESS_GROUP" in text, path
        assert "signal.CTRL_BREAK_EVENT" in text, path
