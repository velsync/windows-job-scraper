"""Slice-2 contract tests (S2.0 skeleton; ROAD-03 boundary + architecture discipline).

Repository-level invariants for Slice 2. These assert *structural* rules that
must hold for every Slice-2 package, so the boundary cannot drift while
adapters land one at a time:

* acquisition breadth never opens a second write path — adapters stay pure
  (no database, no network I/O) and canonical state stays owned by the
  fenced pipeline;
* every built-in adapter is manifest-validated, registered, and reachable
  only through the registry (no dynamic import);
* the fingerprint classifier and strategy router perform no I/O and cannot
  construct or widen a destination grant;
* migration discipline — Slice 2 may only append steps after the accepted
  Slice-1 v10, and every released step stays byte-pinned;
* dependency discipline — Slice 2 adds no runtime dependency;
* FTS5 is capability-gated and only ``jobscraper.search`` may create it;
* Slice-3+/5/6/7/8 objects stay uncreated.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC = REPO_ROOT / "src" / "jobscraper"

# The exact built-in adapter set as of the current Slice-2 package. Each
# Slice-2 adapter package appends to this set consciously; nothing else may
# register an adapter. S2.5 graduated Greenhouse, S2.6 Lever, S2.7 Ashby; the
# S2.8 corrective adds the bounded generic-discovery first-probe planner that
# 02 §12.1 requires before any discovery network I/O.
EXPECTED_BUILTIN_ADAPTERS = {
    "json_api_feed", "generic_discovery", "greenhouse", "lever", "ashby"
}

# Slice 2 supports the HTTP execution class only (02 §14; ROAD-07 defers
# browser acquisition).  The router must report a browser-class candidate as
# unsupported instead of executing it or silently substituting another one.
SLICE2_SUPPORTED_EXECUTION_CLASSES = {"HTTP"}

# Dependencies pinned by the accepted Slice-0 production lock.  Slice 2 must
# not extend it (05 dependency-locking discipline).
LOCKED_SET = {
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

# Tables/objects owned by later slices (ROAD-04/05/06/07/08).  Slice-2
# acquisition code may neither create nor reference them.
LATER_SLICE_TABLES = (
    "recipes",
    "recipe_versions",
    "navigation_plans",
    "navigation_plan_versions",
    "source_fixtures",
    "locator_health",
    "job_merges",
    "contacts",
    "fx_rates",
    "user_feedback",
    "snapshots",
    "egress_profiles",
)


#: import roots provided by the locked pywin32 distribution (the module names
#: are not ``pywin32``); mirrors the accepted Slice-1 contract allowance.
_WIN32_PRIMITIVES = frozenset(
    {
        "win32api",
        "win32con",
        "win32process",
        "win32event",
        "win32crypt",
        "win32cryptcon",
        "win32security",
        "win32file",
        "winerror",
    }
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _adapter_files():
    yield from (SRC / "adapters").rglob("*.py")


# --------------------------------------------------- pure-adapter discipline

#: adapters may only reach these host contract surfaces (02 ACQ-02/ACQ-09)
#:
#: ``jobscraper.timeutil`` was added by S2.5: a provider adapter must emit
#: durable UTC RFC 3339 timestamps (03 §50) and must not reimplement that
#: format.  timeutil is pure formatting/parsing — no I/O, no database, no
#: destination policy, no grant authority — so it widens no boundary that this
#: suite guards.
_ALLOWED_ADAPTER_IMPORTS = {
    "jobscraper.acquisition.failures",
    "jobscraper.acquisition.pagevalidity",
    "jobscraper.acquisition.result",
    "jobscraper.acquisition.envelope",
    "jobscraper.acquisition.origin",
    "jobscraper.acquisition.atsendpoints",
    "jobscraper.net.urlnorm",
    "jobscraper.timeutil",
}


def test_adapters_perform_no_network_io_and_touch_no_database():
    forbidden_imports = {
        "socket",
        "ssl",
        "http",
        "httpx",
        "urllib",
        "sqlite3",
        "subprocess",
        "shutil",
        "multiprocessing",
        "asyncio",
        "playwright",
    }
    for path in _adapter_files():
        tree = _parse(path)
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert not (roots & forbidden_imports), (path, sorted(roots & forbidden_imports))
        text = path.read_text(encoding="utf-8")
        for marker in ("INSERT INTO", "UPDATE ", "DELETE FROM", "PRAGMA", "jobscraper.db"):
            assert marker not in text, (path, marker)


def test_adapters_only_import_host_contract_surfaces():
    for path in _adapter_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
                "jobscraper"
            ):
                allowed = node.module in _ALLOWED_ADAPTER_IMPORTS or node.module.startswith(
                    "jobscraper.adapters"
                )
                assert allowed, (path.relative_to(REPO_ROOT).as_posix(), node.module)


def test_builtin_adapter_registry_is_exact_and_manifest_validated():
    from jobscraper.adapters.contract import validate_manifest
    from jobscraper.adapters.registry import BUILTIN_ADAPTERS, get_adapter

    assert set(BUILTIN_ADAPTERS) == EXPECTED_BUILTIN_ADAPTERS
    for adapter_id, adapter_cls in BUILTIN_ADAPTERS.items():
        manifest = validate_manifest(asdict(adapter_cls.manifest))
        assert manifest.id == adapter_id
        assert set(manifest.supported_execution_classes) <= SLICE2_SUPPORTED_EXECUTION_CLASSES
        with pytest.raises(KeyError):
            get_adapter(f"not-registered-{adapter_id}")


def test_acq02_request_task_mapping_is_complete_and_exact():
    """02 ACQ-02: the durable request-type↔task mapping is explicit data.

    Every acquisition request type the runtime can enqueue maps to exactly one
    adapter task kind, and every host-native pipeline type maps to none — so
    the vocabulary cannot drift between ``runtime.requests`` and the adapter
    contract.
    """
    from jobscraper.adapters.contract import (
        ACQ02_REQUEST_TASK_MAP,
        HOST_NATIVE_REQUEST_TYPE_NAMES,
        AdapterTaskKind,
        task_kind_for_request_type,
    )
    from jobscraper.runtime.requests import (
        ACQUISITION_REQUEST_TYPES,
        HOST_NATIVE_REQUEST_TYPES,
        REQUEST_TYPES,
    )

    assert set(ACQ02_REQUEST_TASK_MAP) == set(ACQUISITION_REQUEST_TYPES)
    assert HOST_NATIVE_REQUEST_TYPE_NAMES == set(HOST_NATIVE_REQUEST_TYPES)
    assert set(ACQ02_REQUEST_TASK_MAP) | HOST_NATIVE_REQUEST_TYPE_NAMES == set(REQUEST_TYPES)
    assert set(ACQ02_REQUEST_TASK_MAP.values()) <= set(AdapterTaskKind)
    assert ACQ02_REQUEST_TASK_MAP["LIST_FETCH"] is AdapterTaskKind.ENUMERATE
    assert ACQ02_REQUEST_TASK_MAP["DETAIL_FETCH"] is AdapterTaskKind.DETAIL
    for request_type in HOST_NATIVE_REQUEST_TYPES:
        assert task_kind_for_request_type(request_type) is None
    with pytest.raises(ValueError):
        task_kind_for_request_type("NOT_A_REQUEST_TYPE")


def test_driver_selects_adapters_only_through_the_registry():
    """ARC-04.3/ACQ-08: no adapter identity is hardcoded in the host driver.

    The driver builds whatever the pinned binding revision names through the
    registry; it may not import an adapter class directly (that would make the
    registry bypassable and re-introduce the S2.4 single-adapter special case).
    """
    driver = (SRC / "pipeline" / "driver.py").read_text(encoding="utf-8")
    assert "build_adapter(" in driver
    for marker in ("FeedApiAdapter", "GreenhouseAdapter", "adapters.feed_api",
                   "adapters.greenhouse"):
        assert marker not in driver, marker
    assert '!= "json_api_feed"' not in driver


def test_provider_endpoint_shapes_come_from_the_versioned_table():
    """02 §12.1/§32: one owner of "known ATS endpoint patterns".

    A provider adapter must derive its URL shapes from
    ``acquisition.atsendpoints`` instead of hardcoding provider hosts, so the
    fingerprint classifier, the origin resolver and the adapter can never
    disagree about what a Greenhouse URL is.
    """
    from jobscraper.acquisition.atsendpoints import ATS_ENDPOINT_SPECS

    provider_hosts = {host for spec in ATS_ENDPOINT_SPECS for host in spec.hosts()}
    for path in _adapter_files():
        if path.name in {"atsendpoints.py", "registry.py", "__init__.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for host in provider_hosts:
            assert f'"{host}"' not in text and f"'{host}'" not in text, (path.name, host)
    greenhouse = (SRC / "adapters" / "greenhouse.py").read_text(encoding="utf-8")
    assert "spec_for_provider" in greenhouse


def test_registry_has_no_dynamic_import_path():
    text = (SRC / "adapters" / "registry.py").read_text(encoding="utf-8")
    for marker in ("importlib", "__import__", "exec(", "eval("):
        assert marker not in text, marker


# ------------------------------------------------- second-write-path guards


# S3.7 (§40) narrow exception: a compatible-304 verification may advance
# verification timestamps without touching canonical state. A non-pipeline
# module may UPDATE job_sources only if every such statement assigns
# exclusively to the verification-timestamp columns (monotonic CASE-guarded)
# and the module never INSERTs into or DELETEs from job_sources.
_VERIFICATION_TIMESTAMP_ONLY_COLUMNS = frozenset({"last_verified_at", "updated_at"})
# Schema migrations execute under the migration gate, not as an independent
# runtime writer. They may backfill newly-added projection columns. The exact
# v20 backfill target set is separately pinned in test_schema_slice3.py.
_SCHEMA_MIGRATION_OWNER = "src/jobscraper/db/schema_sql.py"


def _job_sources_writes_are_verification_timestamp_only(text: str) -> bool:
    if re.search(r"INSERT INTO job_sources\b", text):
        return False
    if re.search(r"DELETE FROM job_sources\b", text):
        return False
    found = False
    for match in re.finditer(r"UPDATE job_sources\b(.*?)(?:\bWHERE\b|;|$)", text, re.S):
        found = True
        set_match = re.search(r"\bSET\b", match.group(1), re.I)
        clause = match.group(1)[set_match.end():] if set_match else match.group(1)
        # Strip CASE..END blocks so column references inside guards do not
        # count as assignment targets; only top-level SET targets remain.
        clause = re.sub(r"\bCASE\b.*?\bEND\b", "", clause, flags=re.S | re.I)
        targets = set(re.findall(r"(\w+)\s*=(?!=)", clause))
        if not targets or not targets <= _VERIFICATION_TIMESTAMP_ONLY_COLUMNS:
            return False
    return found


@pytest.mark.parametrize(
    "table", ["jobs", "job_sources", "job_locations", "companies", "job_observations"]
)
def test_only_pipeline_writes_canonical_and_observation_state(table: str):
    """Single writer for canonical/observation state — including deletion.

    Slice 2's location projection rewrites ``job_locations`` rows, so the scan
    covers ``DELETE FROM`` as well: a second place that can *erase* canonical
    state is exactly as dangerous as a second place that can write it.
    """
    writers = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        patterns = (
            rf"INSERT INTO {table}\b",
            rf"UPDATE {table}\b",
            rf"DELETE FROM {table}\b",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            writers.append(path.relative_to(REPO_ROOT).as_posix())
    for w in writers:
        if w.startswith("src/jobscraper/pipeline/"):
            continue
        if table == "job_sources" and w == _SCHEMA_MIGRATION_OWNER:
            continue
        assert table == "job_sources" and _job_sources_writes_are_verification_timestamp_only(
            (REPO_ROOT / w).read_text(encoding="utf-8")
        ), (table, w)


@pytest.mark.parametrize(
    "table",
    [
        "job_observations",
        "field_evidence",
        "fetch_attempts",
        "parse_attempts",
        "acquisition_evidence",
        "ats_fingerprints",
        "source_route_decisions",
    ],
)
def test_immutable_evidence_is_never_deleted(table: str):
    """Provenance and evidence rows are append-only (03 §30, RUN-12/21).

    Canonical *projections* may be rewritten (they are derived), but the
    evidence they were derived from must survive: no DELETE path may exist for
    an observation, its field evidence, or the fetch/parse/result-evidence
    records behind it.
    """
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(rf"DELETE FROM {table}\b", text), (path, table)


def test_observation_ingest_only_ever_runs_inside_the_fence():
    """RUN-08: request-owned observation writes stay inside ``fenced_commit``.

    Structural proof: every ``ingest_observation`` call in the driver lives in
    the ``mutate`` callback handed to the fence, never in the driver's
    un-fenced section.
    """
    tree = _parse(SRC / "pipeline" / "driver.py")
    fenced = [
        (node.lineno, node.end_lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "mutate"
    ]
    calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ingest_observation"
    ]
    assert calls, "no observation ingestion in the driver"
    assert fenced, "the driver defines no fenced mutate callback"
    for line in calls:
        assert any(start <= line <= end for start, end in fenced), (
            f"ingest_observation at driver.py:{line} is outside the fence"
        )


def test_no_new_runtime_dependencies():
    lock = (REPO_ROOT / "requirements" / "production.lock.txt").read_text(encoding="utf-8")
    names = set()
    for line in lock.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(line.split("==")[0].split("[")[0].lower().replace("_", "-"))
    assert names == LOCKED_SET, names ^ LOCKED_SET


def test_every_package_import_root_is_stdlib_or_locked():
    for path in SRC.rglob("*.py"):
        for node in ast.walk(_parse(path)):
            root: str | None = None
            if isinstance(node, ast.Import) and node.names:
                root = node.names[0].name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".")[0]
            if root is None or root == "jobscraper":
                continue
            ok = (
                root in sys.stdlib_module_names
                or root.replace("_", "-") in LOCKED_SET
                or root in _WIN32_PRIMITIVES  # shipped by the locked pywin32
                or root == "fcntl"  # documented POSIX launcher fallback
            )
            assert ok, (path.relative_to(REPO_ROOT).as_posix(), root)


# ----------------------------------------------------- migration discipline


def test_slice2_migrations_only_append_after_the_accepted_slice1_schema():
    from jobscraper.db.schema_sql import LATEST_SCHEMA_VERSION, MIGRATION_STEPS

    versions = [v for v, _name, _sql in MIGRATION_STEPS]
    assert versions == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert LATEST_SCHEMA_VERSION >= 10
    names = {version: name for version, name, _sql in MIGRATION_STEPS}
    assert names[10] == "s1_1_corrective_identity_and_referential_integrity"


def test_schema_version_constants_agree():
    from jobscraper.db.schema_sql import LATEST_SCHEMA_VERSION
    from jobscraper.version import SCHEMA_VERSION

    assert SCHEMA_VERSION == LATEST_SCHEMA_VERSION


# ----------------------------------------------------- security boundaries


def test_no_module_can_construct_an_internal_grant_except_the_host_driver():
    """04 §5.1: the only loopback grant rule stays in the host driver."""
    for path in SRC.rglob("*.py"):
        if path.name in {"driver.py", "destination.py"}:
            continue
        assert "InternalGrant(" not in path.read_text(encoding="utf-8"), path


def test_destination_policy_still_derives_only_from_the_source_entry_host():
    driver = (SRC / "pipeline" / "driver.py").read_text(encoding="utf-8")
    assert 'source_row["entry_url"]' in driver
    assert "allowed_hosts=frozenset({host})" in driver


def test_route_surface_stays_gated_and_public_set_unchanged():
    app_src = (SRC / "service" / "app.py").read_text(encoding="utf-8")
    match = re.search(r"_PUBLIC = \{(.*?)\}", app_src, re.DOTALL)
    assert match
    assert set(re.findall(r'\("(GET|POST)", "([^"]+)"\)', match.group(1))) == {
        ("GET", "/health/live"),
        ("GET", "/"),
    }
    s2 = SRC / "service" / "s2_routes.py"
    if not s2.exists():
        return
    source = s2.read_text(encoding="utf-8")
    routes = re.findall(r'@app\.(get|post|patch|put|delete)\("([^"]+)"\)', source)
    gated = re.findall(
        r"session=Depends\((?:require_session|require_mutation)\)", source
    )
    assert routes, "Slice-2 route module registers no routes"
    assert len(gated) >= len(routes), (routes, len(gated))


# ------------------------------------------------------------- FTS5 gating


def test_fts5_is_capability_gated_and_owned_by_the_search_package():
    # db/connection.py owns the *capability probe* only (Slice 0); the search
    # package owns the real index and may only build it when the probe says
    # FTS5 exists (01 §45: never claim FTS/BM25 when it is not active).
    probe = (SRC / "db" / "connection.py").as_posix()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "CREATE VIRTUAL TABLE" not in text:
            continue
        rel = path.as_posix()
        if rel == probe:
            assert "temp.__fts5_probe" in text, rel
            continue
        assert rel.startswith((SRC / "search").as_posix()), rel
        assert "fts5_available" in text, rel


def test_search_reports_its_active_mode_honestly():
    search_pkg = SRC / "search"
    if not search_pkg.exists():
        return
    text = "\n".join(p.read_text(encoding="utf-8") for p in search_pkg.rglob("*.py"))
    assert "SUBSTRING_FALLBACK" in text
    assert "FTS5_ACTIVE" in text


# ------------------------------------------------- later-slice boundary


def test_no_later_slice_packages_yet():
    for forbidden in ("adapter_lab", "scheduler", "crawler", "merge", "contacts", "exports"):
        assert not (SRC / forbidden).exists(), forbidden


def test_acquisition_code_does_not_touch_later_slice_tables():
    files = list(_adapter_files()) + list((SRC / "acquisition").rglob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        for table in LATER_SLICE_TABLES:
            assert not re.search(rf"\b(?:INSERT INTO|UPDATE|FROM|JOIN) {table}\b", text), (
                path,
                table,
            )


def test_browser_execution_classes_are_not_dispatched():
    """Slice 2 has no browser acquisition path: HTTP executor stays alone."""
    from jobscraper.adapters.registry import BUILTIN_ADAPTERS

    for adapter_cls in BUILTIN_ADAPTERS.values():
        for supported in adapter_cls.manifest.supported_execution_classes:
            assert supported in SLICE2_SUPPORTED_EXECUTION_CLASSES
    assert not (SRC / "acquisition" / "browserexec.py").exists()
