#!/usr/bin/env python3
"""Slice 2 automated acceptance gate (S2.9).

Aggregates the Slice-2 verification evidence the plan requires — no test
logic is duplicated here beyond what the gate itself owns:

    1. contract suite       tests/contract (slice boundary, security
                            shell, migration discipline, locks)
    2. Slice-2 E2E          tests/integration/test_slice2_acceptance.py
                            — the cross-provider acceptance suite that is the
                            slice's E2E gate (fingerprint → route → provision
                            → acquire → observation → company/location/
                            provenance → cleaned/indexed searchable result)
       + discovery boundary tests/integration/test_slice2_discovery_durability.py
                            and test_slice2_discovery_boundary_audit.py — the
                            02 §12.1 durable first-probe boundary the E2E chain
                            now exercises (durable identity before I/O,
                            restart resume, hostile/invalid refusal, budget
                            denial evidence, probe-history preservation).
    3. full regression      tests/unit tests/contract tests/integration
                            (includes the Slice 0/1 acceptance suites and the
                            Slice-2 E2E gate above)
    4. pip check            dependency closure intact
    5. doctor               Slice-0 operational gate on an initialized
                            isolated root
    6. migration v10→latest The released v1..v10 (accepted Slice-1 schema)
                            bytes are pinned and immutable (RUN-17); build a
                            database at schema v10, seed representative
                            Slice-1 domain data, migrate forward to
                            ``LATEST_SCHEMA_VERSION`` through the released
                            steps (v11..v14), and prove every seeded row
                            survives unchanged with clean integrity /
                            foreign-key checks.  This is read-only over the
                            released steps — no new migration is applied or
                            invented (the blob digest is asserted unchanged).

Exit code 0 only when every gate passes.

Packaged Windows/native acceptance is **not** part of this gate: the sandbox
cannot execute a packaged Windows build, so native/packaging evidence is
recorded as **pending** in the review/status records, never claimed here.
Slice 2 is never claimed PROMOTED from Linux/dev/ordinary-CI evidence alone.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# The in-process migration gate imports jobscraper, so src/ must be on the
# path for this script (it is not installed; tests do this via conftest).
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: Pinned in the past so any driver/e2e timestamp the real UTC clock stamps
#: later still sorts after the seeded instants (sibling-suites convention;
#: a future-dated seed would scramble created_at ordering).
NOW = "2026-09-09T12:00:00.000000Z"

#: Released migration blob must stay byte-identical (RUN-17; 03 §50).
MIGRATIONS_BLOB = "161704673ea3ac50e93a7e68c9f4bc593b096bf4"


def log(message: str) -> None:
    print(f"[slice2-gate] {message}", flush=True)


def run_gate(name: str, command: list[str], *, env: dict | None = None) -> bool:
    log(f"{name}: {' '.join(command)}")
    proc = subprocess.run(command, cwd=str(REPO_ROOT), env=env)
    status = "PASS" if proc.returncode == 0 else "FAIL"
    log(f"{name}: {status} (exit {proc.returncode})")
    return proc.returncode == 0


def initialize_root(data_root: Path) -> None:
    """One real launcher run so the root has DB + secret (then stopped)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["WJS_SUPPRESS_BROWSER_OPEN"] = "1"
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        # Windows has no cooperative SIGTERM via Popen. Create a dedicated
        # process group so CTRL_BREAK_EVENT reaches the launcher, which can
        # then stop its owned service and release the single-instance mutex.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    launcher = subprocess.Popen(
        [sys.executable, "-m", "jobscraper", "--data-root", str(data_root), "--print-url"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        **popen_kwargs,
    )
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            line = launcher.stdout.readline() if launcher.stdout else ""
            if line.startswith("Dashboard:"):
                return
            if launcher.poll() is not None:
                return
    finally:
        if launcher.poll() is None:
            if os.name == "nt":
                launcher.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                launcher.terminate()
            try:
                launcher.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                launcher.kill()
                launcher.wait()


# ---------------------------------------------------------------------------
# Migration verification v10 -> LATEST (RUN-17)
# ---------------------------------------------------------------------------

#: Representative Slice-1 domain tables (the schema as released at v10) that
#: v11-v14 touch with ALTER/CREATE; rows here must survive the forward steps.
_SEEDED_TABLES: tuple[str, ...] = (
    "sources", "source_adapter_bindings", "scrape_runs", "scrape_requests",
    "request_attempts", "fetch_attempts", "parse_attempts", "companies",
    "jobs", "job_observations", "field_evidence", "job_sources",
    "job_locations",
)
_PK_BY_TABLE: dict[str, str] = {
    "sources": "id", "source_adapter_bindings": "id", "scrape_runs": "id",
    "scrape_requests": "id", "request_attempts": "attempt_id",
    "fetch_attempts": "id", "parse_attempts": "id", "companies": "id",
    "jobs": "id", "job_observations": "id", "field_evidence": "id",
    "job_sources": "id", "job_locations": "id",
}


def _seed_slice1_at_v10(conn: sqlite3.Connection) -> None:
    """Populate representative, FK-valid accepted Slice-1 (v10) domain rows."""
    now = NOW
    conn.execute(
        "INSERT INTO sources(id,display_name,source_family,entry_url,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?)",
        ("src-1", "Acme Careers", "ats_board", "https://boards.greenhouse.io/acme", now, now),
    )
    conn.execute(
        "INSERT INTO sources(id,display_name,source_family,entry_url,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?)",
        ("src-2", "Acme Feed", "json_feed", "https://feed.example.com/acme", now, now),
    )
    conn.execute(
        "INSERT INTO source_adapter_bindings(id,source_id,display_name,created_at)"
        " VALUES(?,?,?,?)",
        ("bnd-1", "src-1", "acme greenhouse", now),
    )
    conn.execute(
        "INSERT INTO source_adapter_bindings(id,source_id,display_name,created_at)"
        " VALUES(?,?,?,?)",
        ("bnd-2", "src-2", "acme feed", now),
    )
    conn.execute(
        "INSERT INTO scrape_runs(id,status,created_at,started_at,finished_at,jobs_discovered,jobs_saved)"
        " VALUES(?,?,?,?,?,?,?)",
        ("run-1", "SUCCEEDED", now, now, now, 2, 2),
    )
    for rid, sid, bid, key in (("req-1", "src-1", "bnd-1", "reqkey-1"),
                               ("req-2", "src-2", "bnd-2", "reqkey-2")):
        conn.execute(
            "INSERT INTO scrape_requests(id,run_id,source_id,binding_id,request_type,"
            " request_unique_key,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, "run-1", sid, bid, "LIST_FETCH", key, "SUCCEEDED", now, now),
        )
    for aid, rid in (("att-1", "req-1"), ("att-2", "req-2")):
        conn.execute(
            "INSERT INTO request_attempts(attempt_id,request_id,started_at,finished_at,outcome,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (aid, rid, now, now, "SUCCEEDED", now),
        )
    conn.execute(
        "INSERT INTO fetch_attempts(id,attempt_id,request_id,requested_url,final_url,"
        " status_code,content_type,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
        ("feth-1", "att-1", "req-1", "https://boards.greenhouse.io/acme",
         "https://boards.greenhouse.io/acme", 200, "text/html", now),
    )
    conn.execute(
        "INSERT INTO fetch_attempts(id,attempt_id,request_id,requested_url,final_url,"
        " status_code,content_type,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
        ("feth-2", "att-2", "req-2", "https://feed.example.com/acme/jobs",
         "https://feed.example.com/acme/jobs", 200, "application/json", now),
    )
    conn.execute(
        "INSERT INTO parse_attempts(id,attempt_id,request_id,parser_kind,parser_version,"
        " outcome_kind,observation_count,parsed_at) VALUES(?,?,?,?,?,?,?,?)",
        ("parse-1", "att-1", "req-1", "greenhouse", "1.0.0", "SUCCESS_WITH_JOBS", 2, now),
    )
    conn.execute(
        "INSERT INTO parse_attempts(id,attempt_id,request_id,parser_kind,parser_version,"
        " outcome_kind,observation_count,parsed_at) VALUES(?,?,?,?,?,?,?,?)",
        ("parse-2", "att-2", "req-2", "json_api_feed", "1.0.0", "SUCCESS_WITH_JOBS", 2, now),
    )
    conn.execute(
        "INSERT INTO companies(id,name,normalized_name,domain,careers_url,ats_provider,"
        " ats_board,first_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("co-1", "Acme Corp", "acme corp", "acme.example",
         "https://boards.greenhouse.io/acme", "greenhouse", "acme", now, now, now),
    )
    conn.execute(
        "INSERT INTO jobs(id,company_id,title,normalized_title,description_md,"
        " description_text,salary_original_text,salary_min,salary_max,salary_currency,"
        " salary_period,remote_mode,employment_type,experience_level,posted_at,"
        " discovered_at,first_seen_at,last_seen_at,listing_status,fingerprint,"
        " created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("job-1", "co-1", "Software Engineer", "software engineer",
         "# Software Engineer", "Software Engineer", "$150K - $210K",
         150000, 210000, "USD", "YEAR", "REMOTE", "FULL_TIME", "SENIOR",
         "2026-09-01T00:00:00Z", now, now, now, "ACTIVE", "fp-1", now, now),
    )
    conn.execute(
        "INSERT INTO jobs(id,company_id,title,normalized_title,description_md,"
        " description_text,discovered_at,first_seen_at,last_seen_at,listing_status,"
        " created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("job-2", "co-1", "Data Analyst", "data analyst", None, None,
         now, now, now, "ACTIVE", now, now),
    )
    obs = [
        ("obs-1", "req-1", "att-1", "src-1", "bnd-1", "greenhouse", "PROVIDER_NATIVE",
         "job-1", "ockey-1"),
        ("obs-2", "req-2", "att-2", "src-2", "bnd-2", "json_api_feed", "FEED",
         "job-1", "ockey-2"),
    ]
    for oid, req, att, sid, bid, adapter, strategy, sjob, key in obs:
        conn.execute(
            "INSERT INTO job_observations(id,run_id,request_id,attempt_id,source_id,"
            " binding_id,adapter_id,adapter_version,strategy,execution_class,"
            " source_job_id,source_rank_or_order,observed_at,observation_unique_key)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, "run-1", req, att, sid, bid, adapter, "1.0.0", strategy, "HTTP",
             sjob, 0, now, key),
        )
    conn.execute(
        "INSERT INTO field_evidence(id,observation_id,field_name,locator_kind,"
        " locator_value,value_hash,excerpt,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("fe-1", "obs-1", "title", "json_path", "$.title", "h1", "Software Engineer", now),
    )
    conn.execute(
        "INSERT INTO field_evidence(id,observation_id,field_name,locator_kind,"
        " locator_value,value_hash,excerpt,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("fe-2", "obs-1", "salary", "json_path", "$.compensation", "h2", "$150K - $210K", now),
    )
    pres = [
        ("js-1", "job-1", "src-1", "bnd-1", "https://boards.greenhouse.io/acme",
         "https://boards.greenhouse.io/acme/jobs/1", "obs-1", 0),
        ("js-2", "job-1", "src-2", "bnd-2", "https://feed.example.com/acme",
         "https://feed.example.com/acme/jobs/1", "obs-2", 1),
    ]
    for jsid, job, sid, bid, disc, raw, obs_id, rank in pres:
        conn.execute(
            "INSERT INTO job_sources(id,job_id,source_id,binding_id,source_job_id,"
            " discovery_url,raw_source_url,first_seen_at,last_seen_at,presence_state,"
            " content_revision,last_observation_id,source_rank,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jsid, job, sid, bid, "job-1", disc, raw, now, now, "ACTIVE", 1,
             obs_id, rank, now, now),
        )
    conn.execute(
        "INSERT INTO job_locations(id,job_id,raw_text,country,city,remote)"
        " VALUES(?,?,?,?,?,?)",
        ("jl-1", "job-1", "Remote", "", "", 1),
    )
    conn.execute(
        "INSERT INTO job_locations(id,job_id,raw_text,country,region,city,remote)"
        " VALUES(?,?,?,?,?,?,?)",
        ("jl-2", "job-2", "Bucharest, Romania", "RO", "B", "Bucharest", 0),
    )
    conn.commit()


def _snapshot(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    snap: dict[str, list[dict]] = {}
    for table in _SEEDED_TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        rows = [dict(zip(cols, r)) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        snap[table] = rows
    return snap


def _verify_preservation(conn: sqlite3.Connection, before: dict[str, list[dict]]) -> list[str]:
    problems: list[str] = []
    for table in _SEEDED_TABLES:
        pk = _PK_BY_TABLE[table]
        rows = before[table]
        if not rows:
            continue
        orig_cols = list(rows[0].keys())
        for row in rows:
            cur = conn.execute(
                f"SELECT {','.join(orig_cols)} FROM {table} WHERE {pk}=?",
                (row[pk],),
            ).fetchone()
            if cur is None:
                problems.append(f"{table}:{row[pk]} row missing after migration")
                continue
            curd = dict(zip(orig_cols, cur))
            for col in orig_cols:
                if curd[col] != row[col]:
                    problems.append(f"{table}:{row[pk]}.{col} changed "
                                    f"({row[col]!r} -> {curd[col]!r})")
    return problems


def verify_migration_from_v10(tmp: Path) -> tuple[bool, dict[str, object]]:
    """Build a v10 DB, seed Slice-1 data, migrate to LATEST, prove preservation."""
    from jobscraper.db.connection import Database
    from jobscraper.db.migrations import (
        LATEST_SCHEMA_VERSION,
        current_schema_version,
        migrate_schema,
        run_database_checks,
    )

    report: dict[str, object] = {"ok": False, "from": 10, "to": LATEST_SCHEMA_VERSION}
    db = Database(tmp / "migration-v10.db")
    try:
        migrate_schema(db.conn, 10)
        report["seeded_version"] = current_schema_version(db.conn)
        _seed_slice1_at_v10(db.conn)
        pre = run_database_checks(db.conn)
        report["pre_checks_ok"] = bool(pre["ok"])
        if not pre["ok"]:
            report["problems"] = [f"pre-seed checks failed: {pre['problems']}"]
            return False, report

        before = _snapshot(db.conn)
        report["seeded_rows"] = {t: len(v) for t, v in before.items()}

        applied = migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
        expected = list(range(11, LATEST_SCHEMA_VERSION + 1))
        report["applied"] = applied
        if applied != expected:
            report["problems"] = [f"expected steps {expected}, applied {applied}"]
            return False, report

        post = run_database_checks(db.conn)
        report["post_checks_ok"] = bool(post["ok"])
        problems: list[str] = []
        if not post["ok"]:
            problems.append(f"post-migration checks failed: {post['problems']}")
        if post["foreign_key_check"] != 0:
            problems.append(f"foreign_key_check violations: {post['foreign_key_check']}")
        if post["integrity_check"] != "ok":
            problems.append(f"integrity_check: {post['integrity_check']}")
        report["foreign_key_check"] = post["foreign_key_check"]
        report["integrity_check"] = post["integrity_check"]
        report["final_version"] = current_schema_version(db.conn)

        problems.extend(_verify_preservation(db.conn, before))
        report["preservation_problems"] = problems
        if problems:
            report["problems"] = problems
            return False, report
        report["ok"] = True
        report["problems"] = []
        return True, report
    finally:
        db.close()


def _verify_migration_blob() -> tuple[bool, str]:
    """The released migration module blob is byte-identical (RUN-17)."""
    import hashlib
    import subprocess as _sp

    path = SRC / "jobscraper" / "db" / "migrations.py"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # the project pins blob identity via `git hash-object` (blob SHA-1)
    proc = _sp.run(["git", "hash-object", str(path)], capture_output=True, text=True)
    blob = proc.stdout.strip()
    return blob == MIGRATIONS_BLOB, blob


def run_migration_gate(env: dict) -> bool:
    """Run the migration verification in-process and report."""
    # import path for jobscraper
    from jobscraper.db.connection import Database  # noqa: F401  (smoke import)

    ok_blob, blob = _verify_migration_blob()
    log(f"migration-blob: {blob} (expect {MIGRATIONS_BLOB})")
    if not ok_blob:
        log("migration-blob: FAIL — released migrations.py digest changed")
        return False
    with tempfile.TemporaryDirectory(prefix="wjs-slice2-migration-") as root_name:
        tmp = Path(root_name)
        ok, report = verify_migration_from_v10(tmp)
        log(f"migration-v10-to-latest: {'PASS' if ok else 'FAIL'} "
            f"(applied {report.get('applied')}, rows {report.get('seeded_rows')})")
        if not ok:
            log(f"migration problems: {report.get('problems')}")
        return ok


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    # All subprocess-driven tests inherit this internal flag so real launcher
    # flows are exercised without creating user-visible browser tabs.
    env["WJS_SUPPRESS_BROWSER_OPEN"] = "1"

    results = {}
    results["contract"] = run_gate(
        "contract-suite",
        [sys.executable, "-m", "pytest", "tests/contract", "-q"],
        env=env,
    )
    results["slice2_e2e"] = run_gate(
        "slice2-e2e-acceptance",
        [
            sys.executable, "-m", "pytest",
            "tests/integration/test_slice2_acceptance.py",
            "tests/integration/test_slice2_discovery_durability.py",
            "tests/integration/test_slice2_discovery_boundary_audit.py",
            "-q",
        ],
        env=env,
    )
    results["tests"] = run_gate(
        "full-regression",
        [
            sys.executable, "-m", "pytest",
            "tests/unit", "tests/contract", "tests/integration", "-q",
        ],
        env=env,
    )
    results["pip_check"] = run_gate("pip-check", [sys.executable, "-m", "pip", "check"], env=env)
    results["migration_v10_to_latest"] = run_migration_gate(env)

    with tempfile.TemporaryDirectory(prefix="wjs-slice2-gate-") as root_name:
        root = Path(root_name)
        log("initializing isolated doctor root with one launcher run")
        initialize_root(root)
        results["doctor"] = run_gate(
            "doctor",
            [sys.executable, "-m", "jobscraper", "--doctor", "--data-root", str(root)],
            env=env,
        )

    aggregate = all(results.values())
    log(
        "gate summary: "
        + json.dumps({k: ("PASS" if v else "FAIL") for k, v in results.items()})
    )
    log("SLICE 2 AUTOMATED GATE: " + ("PASS" if aggregate else "FAIL"))
    return 0 if aggregate else 1


if __name__ == "__main__":
    raise SystemExit(main())
