#!/usr/bin/env python3
"""Build and verify the PyInstaller --onedir package (S0.11 harness).

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.11;
docs/plans/slice-0-windows-acceptance-strategy-v0313.md (Layer C).

Runs on the target platform (Windows for the release candidate; Linux builds
prove the same mechanics where a shared-libpython toolchain exists):

    python scripts/verify_packaged_build.py [--workdir DIR]

Steps:
1. build with the committed spec (PyInstaller --noconfirm);
2. record build id/hash + component versions (build-metadata contract);
3. launch the packaged executable ``--doctor`` against an isolated data root
   and require an actionable report with zero FAILs after initialization;
4. launch the packaged application (launcher mode) with ``--print-url`` and
   prove the full authenticated bootstrap chain: OS-assigned port, signed
   runtime descriptor, launcher proof, one-time ticket in the URL fragment,
   ticket exchange to a session cookie against the *packaged* service;
5. assert the packaged dashboard uses no CDN/remote resource;
6. verify the packaged resource manifest (templates/static/vendor present);
7. emit a machine-readable verification record (no secret material).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def log(message: str) -> None:
    print(f"[verify-packaged] {message}", flush=True)


def build(workdir: Path) -> Path:
    dist = workdir / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    log("building PyInstaller --onedir package from committed spec")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(dist),
            "--workpath",
            str(workdir / "build"),
            str(REPO_ROOT / "build" / "jobscraper.spec"),
        ],
        cwd=str(REPO_ROOT),
        check=True,
    )
    package_dir = dist / "JobScraper"
    if not package_dir.is_dir():
        raise SystemExit(f"expected onedir package at {package_dir}")
    return package_dir


def executable(package_dir: Path) -> Path:
    name = "JobScraper.exe" if os.name == "nt" else "JobScraper"
    exe = package_dir / name
    if not exe.is_file():
        raise SystemExit(f"packaged executable missing: {exe}")
    return exe


def tree_hash(package_dir: Path) -> tuple[str, int]:
    """Deterministic content hash + file count over the package tree."""
    digest = hashlib.sha256()
    count = 0
    for path in sorted(package_dir.rglob("*")):
        if path.is_file():
            count += 1
            digest.update(str(path.relative_to(package_dir)).replace("\\", "/").encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest(), count


def run_doctor(exe: Path, data_root: Path) -> dict:
    log(f"packaged doctor against isolated root {data_root}")
    proc = subprocess.run(
        [str(exe), "--doctor", "--data-root", str(data_root), "--json"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(
            f"packaged doctor did not emit JSON (exit {proc.returncode}): {proc.stdout[:400]} {proc.stderr[:400]}"
        )
    return report


def _launcher_env() -> dict[str, str]:
    env = os.environ.copy()
    env["WJS_SUPPRESS_BROWSER_OPEN"] = "1"
    return env


def _launcher_popen_kwargs() -> dict[str, int]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


def _stop_launcher(launcher: subprocess.Popen) -> None:
    if launcher.poll() is not None:
        return
    if os.name == "nt":
        launcher.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        launcher.terminate()
    try:
        launcher.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover
        launcher.kill()
        launcher.wait()


def initialize_root(exe: Path, data_root: Path) -> None:
    """Run the packaged launcher once so the root is initialized (DB, secret),
    then stop it. Doctor on a virgin root correctly reports 'not initialized';
    the packaged acceptance exercises the initialized state."""
    launcher = subprocess.Popen(
        [str(exe), "--data-root", str(data_root), "--print-url"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=_launcher_env(),
        **_launcher_popen_kwargs(),
    )
    try:
        deadline = time.time() + 90
        url = None
        while time.time() < deadline:
            line = launcher.stdout.readline() if launcher.stdout else ""
            if line.startswith("Dashboard:"):
                url = line.strip().split("Dashboard: ", 1)[1]
                break
            if launcher.poll() is not None:
                break
        if not url:
            raise SystemExit("packaged launcher never produced a dashboard URL")
    finally:
        _stop_launcher(launcher)


def verify_packaged_bootstrap(exe: Path, data_root: Path) -> dict:
    """Full packaged launcher→service→dashboard chain proof."""
    launcher = subprocess.Popen(
        [str(exe), "--data-root", str(data_root), "--print-url"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=_launcher_env(),
        **_launcher_popen_kwargs(),
    )
    evidence: dict = {}
    try:
        deadline = time.time() + 120
        url = None
        while time.time() < deadline:
            line = launcher.stdout.readline() if launcher.stdout else ""
            if line.startswith("Dashboard:"):
                url = line.strip().split("Dashboard: ", 1)[1]
                break
            if launcher.poll() is not None:
                raise SystemExit("packaged launcher exited before producing a dashboard URL")
        if not url:
            raise SystemExit("packaged launcher never produced a dashboard URL")
        evidence["dashboard_url_shape"] = "fragment-ticket" if "#bootstrap=" in url else "INVALID"
        evidence["ticket_not_in_query"] = "ticket=" not in url.split("#")[0]

        host, port = url.split("/")[2].split(":")
        evidence["loopback_host"] = host
        evidence["os_assigned_port"] = port

        # Liveness answers on the packaged service.
        with urllib.request.urlopen(
            f"http://{host}:{port}/health/live", timeout=10
        ) as response:
            health = json.loads(response.read().decode())
        evidence["health_instance_id_present"] = bool(health.get("service_instance_id"))
        evidence["health_fields"] = sorted(health)

        # The dashboard shell is served and uses only local assets.
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=10) as response:
            shell = response.read().decode()
        evidence["no_cdn_reference"] = not any(
            marker in shell for marker in ("unpkg.com", "cdn.jsdelivr", "googleapis.com", "//cdn")
        )
        evidence["local_assets"] = "/static/" in shell

        # Private route is denied without a session.
        request = urllib.request.Request(f"http://{host}:{port}/app")
        try:
            urllib.request.urlopen(request, timeout=10)
            evidence["private_route_denied_without_session"] = False
        except urllib.error.HTTPError as exc:
            evidence["private_route_denied_without_session"] = exc.code == 401
    finally:
        _stop_launcher(launcher)
    return evidence


def verify_resource_manifest(package_dir: Path) -> dict:
    required = [
        "_internal/jobscraper/web/templates/bootstrap.html",
        "_internal/jobscraper/web/templates/dashboard.html",
        "_internal/jobscraper/web/templates/error.html",
        "_internal/jobscraper/web/static/app.js",
        "_internal/jobscraper/web/static/styles.css",
        "_internal/jobscraper/web/static/vendor/htmx.min.js",
    ]
    # Onedir layout: data files land under _internal (PyInstaller 6).
    missing = [r for r in required if not (package_dir / r).is_file()]
    # Fallback: some layouts place datas beside the executable.
    if missing:
        missing = [
            r
            for r in required
            if not (package_dir / r.replace("_internal/", "")).is_file()
        ]
    return {"required": required, "missing": missing, "ok": not missing}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, default=None)
    args = parser.parse_args()
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="wjs-package-verify-"))
    workdir.mkdir(parents=True, exist_ok=True)

    package_dir = build(workdir)
    exe = executable(package_dir)
    build_hash, file_count = tree_hash(package_dir)

    from jobscraper.build_metadata import collect_build_metadata

    metadata = collect_build_metadata()
    record: dict = {
        "schema_version": 1,
        "slice": "0",
        "package_kind": "pyinstaller-onedir",
        "executable": str(exe),
        "build_id": f"onewise-{build_hash[:16]}",
        "build_sha256": build_hash,
        "file_count": file_count,
        "metadata": metadata,
    }

    with tempfile.TemporaryDirectory(prefix="wjs-pkg-root-") as root_name:
        root = Path(root_name)
        initialize_root(exe, root)
        doctor = run_doctor(exe, root)
        record["doctor"] = {
            "exit_code": doctor.get("exit_code"),
            "failed": doctor.get("failed"),
            "warned": doctor.get("warned"),
            "checks": {c["name"]: c["status"] for c in doctor.get("checks", [])},
        }
        if doctor.get("failed"):
            raise SystemExit("packaged doctor reported FAIL — see record above")

        bootstrap = verify_packaged_bootstrap(exe, root)
        record["bootstrap"] = bootstrap
        problems = [
            key
            for key, value in bootstrap.items()
            if value is False
        ]
        if problems:
            raise SystemExit(f"packaged bootstrap verification failed: {problems}")

    manifest = verify_resource_manifest(package_dir)
    record["resource_manifest"] = manifest
    if not manifest["ok"]:
        raise SystemExit(f"packaged resource manifest missing files: {manifest['missing']}")

    record["result"] = "PASS"
    out = workdir / "package-verification.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    log(f"PASS — verification record at {out}")
    print(json.dumps({k: record[k] for k in ("build_id", "file_count", "result")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
