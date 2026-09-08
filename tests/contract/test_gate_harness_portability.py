"""Regression contract for Windows launcher-harness cleanup and automation hygiene."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WINDOWS_LAUNCHER_HARNESSES = (
    REPO_ROOT / "scripts" / "verify_slice0.py",
    REPO_ROOT / "scripts" / "verify_slice1.py",
    REPO_ROOT / "scripts" / "verify_packaged_build.py",
    REPO_ROOT / "scripts" / "native_acceptance.py",
)
AUTOMATED_BROWSER_HARNESSES = WINDOWS_LAUNCHER_HARNESSES


def test_windows_launcher_harnesses_compile():
    for path in WINDOWS_LAUNCHER_HARNESSES:
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")


def test_windows_launcher_harnesses_do_not_use_nonexistent_subprocess_sigterm():
    for path in WINDOWS_LAUNCHER_HARNESSES:
        text = path.read_text(encoding="utf-8")
        assert "subprocess.SIGTERM" not in text, path


def test_windows_launcher_harnesses_use_process_group_and_ctrl_break():
    for path in WINDOWS_LAUNCHER_HARNESSES:
        text = path.read_text(encoding="utf-8")
        assert "subprocess.CREATE_NEW_PROCESS_GROUP" in text, path
        assert "signal.CTRL_BREAK_EVENT" in text, path


def test_automated_launcher_harnesses_suppress_real_browser_open():
    for path in AUTOMATED_BROWSER_HARNESSES:
        text = path.read_text(encoding="utf-8")
        assert "WJS_SUPPRESS_BROWSER_OPEN" in text, path
