"""Regression tests for stable process identity in the native acceptance harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "native_acceptance.py"


def _load_native_acceptance_module():
    spec = importlib.util.spec_from_file_location("wjs_native_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_tasklist_identity_ignores_volatile_memory_usage():
    module = _load_native_acceptance_module()
    before = '"chrome.exe","4242","Console","1","100,000 K"'
    after = '"chrome.exe","4242","Console","1","175,000 K"'

    assert module._process_identity(before) == ("chrome.exe", 4242)
    assert module._process_identity(after) == ("chrome.exe", 4242)


def test_process_identity_ignores_tasklist_header():
    module = _load_native_acceptance_module()
    header = '"Image Name","PID","Session Name","Session#","Mem Usage"'

    assert module._process_identity(header) is None
