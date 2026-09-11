"""Slice-1 contract tests (S1.11).

Repository-level invariants that keep Slice 1 inside the accepted
architecture:

* slice boundary — no Slice-2+ scope *owned by Slice 1* (scheduler, contacts,
  exports, recipes/Adapter Lab, merge UI).  Two of the original whole-tree
  boundary pins ("no FTS5 anywhere", "exactly one built-in adapter") are
  Slice-1 *shipped-state* assertions that ROAD-03 legitimately supersedes;
  they are re-scoped to what Slice 1 itself owns, and the whole-tree rules
  moved to ``test_slice2_contract.py`` (S2.0) rather than being dropped.  No
  Slice-1 architectural invariant was relaxed here;
* provenance-first — adapters never touch the database and nothing
  outside the pipeline writes canonical jobs;
* browser/service boundary — Playwright/Chromium only inside the browser
  worker package, never in the service process;
* security shell — the public route set is unchanged and every Slice-1
  mutation route goes through session+CSRF dependencies;
* migration discipline — forward-only steps with pinned bytes and
  sequential versions;
* dependency discipline — no imports outside the locked set;
* concurrency discipline — network I/O never happens inside a DB write
  transaction (the executor takes no connection; the driver fetches
  outside the fence).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC = REPO_ROOT / "src" / "jobscraper"

SLICE1_PACKAGES = (
    "net",
    "runtime",
    "acquisition",
    "adapters",
    "pipeline",
    "profiles",
    "inbox",
    "applications",
    "service",
)

# Exact dependency set of the Slice-0 production lock.  Slice 1 must not
# add runtime dependencies; amending this set is a deliberate contract
# change that requires re-review.
SLICE0_LOCKED_SET = {
    "annotated-doc",
    "annotated-types",
    "anyio",
    "click",
    "colorama",
    "fastapi",
    "greenlet",
    "h11",
    "idna",
    "jinja2",
    "markupsafe",
    "playwright",
    "pydantic",
    "pydantic-core",
    "pyee",
    "pywin32",
    "starlette",
    "typing-extensions",
    "typing-inspection",
    "tzdata",
    "uvicorn",
}

# The complete Slice-1 service API surface.  Adding a route means changing
# the public contract and must amend this set consciously.
EXPECTED_S1_ROUTES = {
    ("get", "/api/profiles"),
    ("post", "/api/profiles"),
    ("patch", "/api/profiles/{profile_id}"),
    ("get", "/api/runs"),
    ("post", "/api/runs"),
    ("post", "/api/runs/{run_id}/cancel"),
    ("get", "/api/inbox"),
    ("post", "/api/profiles/{profile_id}/jobs/{job_id}/disposition"),
    ("get", "/api/jobs/{job_id}"),
    ("get", "/api/applications"),
    ("post", "/api/applications"),
    ("patch", "/api/applications/{application_id}"),
}


def _source_files(*dirs: str):
    for directory in dirs:
        for path in (SRC / directory).rglob("*.py"):
            yield path


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_unowned_virtual_table_or_fts_capability_probe():
    """S1.11 boundary pin, re-scoped by Slice 2 (S2.0).

    Slice 1 shipped no FTS5/search surface, so the original assertion was
    "the string ``fts5`` appears nowhere outside the capability surfaces".
    ROAD-03 *owns* FTS5, so that whole-tree prohibition is now the Slice-2
    contract (``test_slice2_contract.py::test_fts5_is_capability_gated_and_
    owned_by_the_search_package``).

    What Slice 1 still pins, unchanged: the FTS5 *capability probe* belongs to
    the Slice-0 surfaces only, and no Slice-1 package may create a SQLite
    virtual table.
    """
    capability_surfaces = {
        (SRC / "db" / "connection.py").as_posix(),
        (SRC / "db" / "migrations.py").as_posix(),
        (SRC / "launcher" / "doctor.py").as_posix(),
    }
    probe = (SRC / "db" / "connection.py").as_posix()
    for path in SRC.rglob("*.py"):
        rel = path.as_posix()
        if "__fts5_probe" in (text := path.read_text(encoding="utf-8")):
            assert rel in capability_surfaces, path
        if rel != probe and not rel.startswith((SRC / "search").as_posix()):
            assert "CREATE VIRTUAL TABLE" not in text, path


def test_no_scheduler_or_cron():
    assert not (SRC / "scheduler").exists()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "cron" not in text.lower(), path


def test_no_contacts_exports_adapter_lab_or_merge_ui():
    for forbidden in ("contacts", "exports", "adapter_lab"):
        assert not (SRC / forbidden).exists(), forbidden
    # Slice-2 tables are never referenced by Slice-1 code
    for table in ("contacts", "recipes", "recipe_versions", "navigation_plans",
                  "job_merges", "user_feedback", "fx_rates"):
        for path in _source_files(*SLICE1_PACKAGES):
            text = path.read_text(encoding="utf-8")
            assert not re.search(rf"\b{table}\b", text), (path, table)


def test_feed_adapter_remains_the_slice1_baseline_adapter():
    """S1.11 boundary pin, re-scoped by Slice 2 (S2.0).

    Slice 1 pinned *exactly one* built-in adapter because ROAD-02 ships the
    smallest reliable feed path.  ROAD-03 owns structured acquisition
    breadth, so the exact adapter set is now the Slice-2 contract
    (``test_slice2_contract.py::test_builtin_adapter_registry_is_exact_and_
    manifest_validated``).

    What Slice 1 still pins: its own adapter is registered, manifest-valid,
    HTTP-only, and unchanged in identity.
    """
    from jobscraper.adapters.registry import BUILTIN_ADAPTERS

    cls = BUILTIN_ADAPTERS["json_api_feed"]
    assert cls.manifest.id == "json_api_feed"
    assert cls.manifest.adapter_api_version == "1"
    assert set(cls.manifest.supported_execution_classes) == {"HTTP"}
    assert set(cls.manifest.supported_auth_modes) == {"NONE"}


def test_adapters_never_touch_the_database():
    for path in _source_files("adapters"):
        text = path.read_text(encoding="utf-8")
        assert "jobscraper.db" not in text, path
        assert "sqlite3" not in text, path
        assert "INSERT INTO" not in text, path
        assert "UPDATE " not in text.replace("updated_at", ""), path


def test_only_pipeline_writes_canonical_jobs():
    writers = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"INSERT INTO jobs\b", text):
            # as_posix(): the assertion must not depend on the host's
            # path separator (windows-latest str() yields backslashes)
            writers.append(path.relative_to(REPO_ROOT).as_posix())
    assert all(w.startswith("src/jobscraper/pipeline/") for w in writers), writers


def test_playwright_stays_in_the_browser_worker():
    for path in SRC.rglob("*.py"):
        if "browser_worker" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        assert "from playwright" not in text and "import playwright" not in text, path
        assert "sync_playwright" not in text, path


def _route_decorators(node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Yield (http_method, path) for each @app.get/post/... decorator."""
    for deco in node.decorator_list:
        if (
            isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Attribute)
            and isinstance(deco.func.value, ast.Name)
            and deco.func.value.id == "app"
            and deco.args
            and isinstance(deco.args[0], ast.Constant)
            and isinstance(deco.args[0].value, str)
        ):
            yield deco.func.attr, deco.args[0].value


def _is_gated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if a parameter default is Depends(require_session|require_mutation)."""
    defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d]
    for default in defaults:
        if (
            isinstance(default, ast.Call)
            and isinstance(default.func, ast.Name)
            and default.func.id == "Depends"
            and default.args
            and isinstance(default.args[0], ast.Name)
            and default.args[0].id in ("require_session", "require_mutation")
        ):
            return True
    return False


def test_public_route_set_unchanged_and_s1_routes_gated():
    app_src = (SRC / "service" / "app.py").read_text(encoding="utf-8")
    match = re.search(r"_PUBLIC = \{(.*?)\}", app_src, re.DOTALL)
    assert match, "_PUBLIC route set missing from service/app.py"
    assert set(re.findall(r'\("(GET|POST)", "([^"]+)"\)', match.group(1))) == {
        ("GET", "/health/live"),
        ("GET", "/"),
    }
    # unknown methods still fail closed in classify_request
    assert "unknown: fail closed" in app_src

    tree = _parse(SRC / "service" / "s1_routes.py")
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = list(_route_decorators(node))
        if not routes:
            continue
        for method, path in routes:
            assert (method, path) in EXPECTED_S1_ROUTES, (method, path)
            seen.add((method, path))
        assert _is_gated(node), f"ungated Slice-1 route handler: {node.name}"
    assert seen == EXPECTED_S1_ROUTES, seen ^ EXPECTED_S1_ROUTES


def test_migration_discipline_forward_only_sequential_pinned():
    import hashlib

    from jobscraper.db.schema_sql import (
        LATEST_SCHEMA_VERSION,
        MIGRATION_STEPS,
        REBUILD_STEPS,
    )

    versions = [v for v, _name, _sql in MIGRATION_STEPS]
    assert versions == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert REBUILD_STEPS <= set(versions)
    # every Slice-1 released step is pinned in the S1.1 test file.  Steps
    # appended by later slices are pinned by their own slice's schema test
    # (Slice 2: tests/integration/test_schema_slice2.py), which is what keeps
    # "forward-only, never edited" enforceable across slices.
    pins = (REPO_ROOT / "tests/integration/test_schema_slice1.py").read_text(
        encoding="utf-8"
    )
    for version, _name, sql in MIGRATION_STEPS:
        if version > 10:
            continue
        digest = hashlib.sha256(sql.encode()).hexdigest()
        assert f'{version}: "{digest}"' in pins, f"migration step {version} not pinned"


def test_no_new_runtime_dependencies():
    lock = (REPO_ROOT / "requirements/production.lock.txt").read_text(
        encoding="utf-8"
    )
    locked = set()
    for line in lock.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==")[0].split("[")[0].lower().replace("_", "-")
        locked.add(name)
    assert locked == SLICE0_LOCKED_SET, locked ^ SLICE0_LOCKED_SET

    # import roots provided by locked distributions (pywin32 ships the
    # win32* / win32crypt modules rather than a "pywin32" module)
    import_roots = set(SLICE0_LOCKED_SET)
    import_roots |= {
        "win32api", "win32con", "win32process", "win32event",
        "win32crypt", "win32cryptcon", "win32security", "win32file",
        "winerror",
    }

    # POSIX-only stdlib modules that appear in the non-Windows fallback
    # branches of the launcher (single-instance advisory lock).  They are
    # absent from sys.stdlib_module_names on Windows, so the check must
    # not be platform-naive.
    platform_fallback_stdlib = {"fcntl"}

    # every import inside the package is stdlib, jobscraper, or locked
    for path in SRC.rglob("*.py"):
        tree = _parse(path)
        roots: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.append(node.module.split(".")[0])
        for root in roots:
            assert (
                root == "jobscraper"
                or root in sys.stdlib_module_names
                or root in platform_fallback_stdlib
                or root.replace("_", "-") in import_roots
            ), f"{path.relative_to(REPO_ROOT)}: unexpected import {root!r}"


def test_service_never_blocks_event_loop_on_network_in_transaction():
    # the fetch runs with no transaction held (03 §50). Since S3.3 the
    # service-owned dispatch seam (runtime/dispatch.py) owns the single
    # execute_request call site; the pipeline driver keeps the fenced commit.
    # The marker must appear verbatim at the call site, wherever it lives.
    driver = (SRC / "pipeline" / "driver.py").read_text(encoding="utf-8")
    dispatch = (SRC / "runtime" / "dispatch.py").read_text(encoding="utf-8")
    marker = "execute_request(envelope, policy)  # NO transaction held"
    assert marker in driver or marker in dispatch
    assert "with fenced_commit(" in driver

    # the executor takes no connection/transaction argument: network waits
    # cannot happen while a DB write transaction is held.
    tree = _parse(SRC / "acquisition" / "httpexec.py")
    signatures = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "execute_request"
    ]
    assert signatures, "execute_request not found in acquisition/httpexec.py"
    for node in signatures:
        params = [a.arg for a in node.args.args + node.args.kwonlyargs]
        forbidden = {"conn", "connection", "db", "database", "transaction", "tx"}
        assert not forbidden & set(params), params
