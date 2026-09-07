# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows Job Scraper --onedir package (S0.11).

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.11;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md section 56
(production baseline: PyInstaller --onedir).

Build from the repository root with the committed environment:

    python -m PyInstaller --noconfirm build/jobscraper.spec

The entry module is ``jobscraper.__main__`` so the single executable
dispatches every internal mode (launcher default, --service,
--browser-worker, --doctor, --version). The launcher/service/browser-worker
re-invoke ``sys.executable`` in frozen builds (see
jobscraper.procutils/lifecycle).

Package contents: Python runtime, FastAPI/uvicorn/Jinja2, local templates/
static + vendored HTMX, the pinned tzdata resource, and Playwright with its
driver (the browser-worker runtime). Mutable user data stays under
%LOCALAPPDATA%\\WindowsJobScraper (WJS_DATA_ROOT / --data-root override) —
never inside the package.
"""

import os

REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC_ROOT = os.path.join(REPO_ROOT, "src")

datas = [
    (
        os.path.join(SRC_ROOT, "jobscraper", "web", "templates"),
        os.path.join("jobscraper", "web", "templates"),
    ),
    (
        os.path.join(SRC_ROOT, "jobscraper", "web", "static"),
        os.path.join("jobscraper", "web", "static"),
    ),
]

# uvicorn resolves its loop/protocol/lifespan implementations dynamically.
hiddenimports = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.off",
    "uvicorn.lifespan.on",
    "zoneinfo",
    "tzdata",
    "email.message",  # pydantic
]

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Pinned timezone data (WIN-03A: never rely on an ambient IANA database).
datas += collect_data_files("tzdata", include_py_files=False)
# Playwright package data: the Node driver runtime for the browser worker.
datas += collect_data_files("playwright")
hiddenimports += collect_submodules("playwright")

a = Analysis(
    [os.path.join(SRC_ROOT, "jobscraper", "__main__.py")],
    pathex=[SRC_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the package lean; nothing may import these.
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PySide6",
        "PyQt5",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JobScraper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=None,  # no icon resource is required for Slice 0
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JobScraper",
)
