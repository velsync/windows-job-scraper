# Slice 2 S2.9 Review — automated gate, migration verification, packaging/native prep, DF-1/DF-2 — 2026-09-10

## Verdict

**S2.9 (Slice-2 automated gate, migration verification from the accepted
Slice-1 schema (v10), packaging/native preparation, corrective review,
status record; plan v0313 §S2.9) COMPLETE, with packaged Windows/native
evidence honestly recorded as PENDING (never claimed).**

`scripts/verify_slice2.py` is the Slice-2 automated gate. It aggregates the
existing suites in the same style as `verify_slice0.py`/`verify_slice1.py`
(no test logic forked/duplicated) plus the in-process migration
verification it owns:

- **SLICE 2 AUTOMATED GATE: PASS**
  `{"contract": "PASS", "slice2_e2e": "PASS", "tests": "PASS",
  "pip_check": "PASS", "migration_v10_to_latest": "PASS", "doctor": "PASS"}`
- Contract suite: 73 passed. Slice-2 E2E acceptance: 12 passed.
  Full regression (`tests/unit tests/contract tests/integration`):
  **1298 passed, 1 skipped**. Full suite (`pytest tests`, incl. the
  Windows-native skips): **1298 passed, 5 skipped** (baseline 1280/5 at
  `169d8806…`; +18 tests; zero regressions).
- Slice 0 automated gate: **PASS**; Slice 1 automated gate: **PASS**.
- Migration state: **UNCHANGED** — `git hash-object
  src/jobscraper/db/migrations.py` = `161704673ea3ac50e93a7e68c9f4bc593b096bf4`;
  **no new migration step**; the v10→latest verification is read-only over
  the released steps (v11…v14).

Both deferred findings carried into S2.9 were fixed **red-first**, each with
its own regression test: **DF-1** (`parse_salary` first-number K-suffix
range collapse in the Slice-1 normalization authority) and **DF-2**
(`FeedApiAdapter._failure` missing ACQ-09 `evidence_refs`).

**Slice 2 is NOT claimed PROMOTED.** Packaged Windows/native acceptance
cannot be executed in this sandbox; `scripts/verify_packaged_build.py` /
`scripts/native_acceptance.py` Slice-2 execution and the packaged
Windows/native evidence are recorded as **pending** in the status record.

## Authority and scope

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3** (03 §50/RUN-17 migrations &
  config pinning; 01 §45 FTS honesty; 05 packaging/operations truthfulness;
  06 §72 final acceptance criteria mapping; 02 §22/§37/§40, ACQ-02/03/09
  for the DF-1/DF-2 fixes; §50 read-only released-steps discipline).
- Plan: `docs/plans/slice-2-worker-implementation-plan-v0313.md` §S2.9
  (gate, migration verification, packaging prep, corrective review, status),
  §0 non-negotiable rules, §4 deferrals, §Execution discipline.
- Accepted base: `169d88060e6972ed49e5325ce8ab68a13b2bd3d4` (S2.8 seal;
  docs-only on top of implementation `2378e10ca3f80fe93691f1afa2a34eb8e40fb9c6`;
  CI runs `34407844041` / `34407259447` SUCCESS Ubuntu + Windows, Python
  3.12). Verified with `git cat-file -t` after re-fetching
  `refs/heads/arena/01a087f1-windows-job-scraper` (the known shallow-clone
  hazard: the sandbox returned a single-commit clone of `df1879c4` and the
  fetch refspec covered only `main`; the specific ref was fetched, then
  `git reset --hard 169d8806…`).

### Branch lineage (disclosed per execution discipline)

Prior records name session branches `arena/01a08691-…`,
`arena/01a0877b-…`, `arena/01a087f1-…`. This execution session is
hard-bound by Arena to **`arena/01a0882c-windows-job-scraper`** — a fourth,
different branch id. Documented rather than fought: all work and the push
live on `arena/01a0882c-…` only; no other branch was created, moved or
force-pushed. Lineage: `01a08691` → `01a0877b` → `01a087f1` →
`01a0882c` (this package). The S2.8 branch (`arena/01a087f1-…`, tip =
accepted base `169d8806…`) can fast-forward to this package's commit when
wanted.

## What `scripts/verify_slice2.py` proves (gate ↔ claim map)

The gate aggregates the durable Slice-2 verification evidence with **no test
logic duplicated**:

| Gate | What it exercises | Role in the slice |
|---|---|---|
| `contract` (`tests/contract`) | slice-boundary pins, security shell, migration discipline, locks | Slice-0/1/2 architectural invariants |
| `slice2_e2e` (`tests/integration/test_slice2_acceptance.py`) | the cross-provider acceptance suite (12 scenarios) | the slice's E2E gate (the S2.8 deliverable) |
| `tests` (`tests/unit tests/contract tests/integration`) | full regression | every unit/contract/integration behaviour incl. S0/S1 acceptance + the Slice-2 E2E |
| `pip_check` | dependency closure intact | exact dev lock, no new packages |
| `migration_v10_to_latest` | build a **v10** (accepted Slice-1 schema) DB, seed representative Slice-1 data, migrate through released v11…v14, prove row preservation + clean integrity/FK | RUN-17 discipline over the pinned released bytes |
| `doctor` | launcher Doctor on an initialized isolated root | Slice-0 shell operational regression |

### Migration verification (RUN-17, read-only over released steps)

The gate builds a database migrated to **schema v10** — the accepted
Slice-1 schema (step `s1_1_corrective_identity_and_referential_integrity`,
the single rebuild step). It then seeds a representative, FK-valid Slice-1
domain dataset across 13 tables that the released Slice-2 steps (v11–v14)
touch with `ALTER TABLE … ADD COLUMN` and `CREATE TABLE`:

`sources`, `source_adapter_bindings`, `scrape_runs`, `scrape_requests`,
`request_attempts`, `fetch_attempts`, `parse_attempts`, `companies`,
`jobs`, `job_observations`, `field_evidence`, `job_sources`,
`job_locations` (26 rows total; every FK resolved, `foreign_key_check == 0`
before migrating).

It snapshots the rows' **original v10 columns**, migrates forward with
`migrate_schema` to `LATEST_SCHEMA_VERSION`, and proves:

- exactly the released steps applied: `[11, 12, 13, 14]`;
- every seeded row is still present and byte-identical in its original
  columns after the v11–v14 additions;
- `run_database_checks` clean afterwards: `integrity_check == "ok"`,
  `foreign_key_check == 0`, pragmas correct;
- the released migration module blob is byte-identical
  (`161704673ea3ac50e93a7e68c9f4bc593b096bf4`) — **no new migration**, the
  v10→latest verification is read-only over the released steps.

## Findings

### DF-1 — `parse_salary` collapsed the max of a first-number K-suffix range (severity: LOW — normalization correctness) — FIXED red-first

**Observed.** A provider-verbatim range whose first number carries a
K-suffix (``"$150K - $210K"``, e.g. Ashby's
`compensation.scrapeableCompensationSalarySummary`, and Lever/Greenhouse
salary shapes) collapsed to `min = max = 150000`. The accepted Slice-1
range pattern in the normalization authority
(`src/jobscraper/pipeline/normalize.py`) allowed a K-suffix only on the
*second* number and allowed no currency glyph before the second number, so
the whole range failed to match and the string degraded to the
single-number parse on the first value. `salary_original_text` was preserved
verbatim either way — the adapter never rewrote provider evidence.

**Fix (narrow, in the Slice-1 single-writer normalization authority).**
`_RANGE_RE` now parses each endpoint as `[currency glyph] number
[K-suffix]` independently, and `parse_salary` expands each endpoint's own
K-suffix (`group(2)` for low, `group(4)` for high). The `low <= high`
guard is unchanged; a lone value still falls through to `_SINGLE_RE`.
No other normalize behaviour changed (`git diff` on `normalize.py` is
exactly the regex + the two-line parse update).

**Tests.** Red-first `tests/unit/test_normalize_salary.py` (7 tests:
parametrized K-suffix ranges on both endpoints, on each endpoint alone,
comma ranges, lone-K unchanged, verbatim-evidence responsibility). All
failing before the fix, green after. The S2.7 e2e spine marker in
`tests/integration/test_ashby_e2e.py` had its DF-1 comment dropped and
`salary_max == 150000` flipped to `== 210000`, as the approved recipe
required; `salary_original_text == "$150K - $210K"` and
`salary_currency == "USD"` assertions are unchanged.

### DF-2 — `FeedApiAdapter._failure` dropped its ACQ-09 evidence refs (severity: LOW — traceability parity) — FIXED red-first

**Observed.** `FeedApiAdapter._failure` built every `FAILURE`
`ParseOutcome` without `evidence_refs`, unlike every S2.5+ provider
adapter (Greenhouse/Lever/Ashby), whose failures link the envelope +
validity evidence rows behind the outcome (ACQ-09 closure/missing-evidence
parity). A feed failure outcome therefore carried no reference to the
durable evidence that produced it.

**Fix (narrow).** `FeedApiAdapter._failure` now takes the
`ValidatedResult` and attaches `evidence_refs=_evidence_refs(result)`, the
same envelope-derived refs (`result_envelope_ref`, `validation_evidence_ref`)
the provider adapters emit. All three `_failure` call sites updated; no
parse semantics changed and **no migration / no new `acquisition_evidence`
CHECK value** (the failure refs point at existing `RESULT_ENVELOPE` /
validity rows already inside the released v11 CHECK set).

**Tests.** Red-first `tests/unit/test_feed_api_adapter.py` (11 tests:
manifest, success + empty paths, and `TestFailureTraceability` asserting a
malformed body, a missing-items marker, and an all-items-invalid failure all
carry both `result://` and `validity://` evidence refs; a failure is never
`SUCCESS_EMPTY`). Failing before the fix (`evidence_refs == ()`), green
after.

## Corrective review pass on this package

An adversarial re-read plus red-first discipline (each fix verified failing
against the pre-fix module, green after), and **sabotage probes** restoring
sealed surfaces byte-identical (sha256-checked):

| Probe (sealed surface) | Expected red | Result |
|---|---|---|
| `_RANGE_RE` reverted to single-trailing-K form | DF-1 salary unit tests + Ashby e2e spine | RED ✓ |
| `_failure` evidence_refs removed (feed) | DF-2 feed failure tests | RED ✓ |
| S2.8 F1 fix reverted (`family = ?` in `runtime/provisioning.py`) | generic-fallback acceptance + F1 unit test | RED ✓ (unchanged; not reverted) |

The S2.8 F1 fix (`record_route_decision` uses NULL-safe `family IS ?`) was
**not** reverted; `verify_slice0.py` / `verify_slice1.py` are byte-identical
to the base; no prior `docs/reviews/` record was edited.

## Packaging / native preparation (assessment, honest)

- **`build/jobscraper.spec` data-file check.** The PyInstaller spec bundles
  `jobscraper/web/templates`, `jobscraper/web/static` (+ vendored HTMX),
  pinned `tzdata`, and Playwright package data. Slice 2 adds only
  **pure-Python code** surfaces (the `ashby` adapter; `adapters/fingerprint.py`
  and `router.py`; `acquisition/atsendpoints.py` endpoint rules; the
  `search/*` FTS capability-gated provisioning) — all reachable through the
  existing `Analysis(pathex=[src], …)` import graph. Slice 2 introduces **no
  new external data file** that the spec's `datas` list would need to
  enumerate; the existing `datas` entry set remains correct for Slice 2. No
  spec change is required by Slice 2.
- **`scripts/verify_packaged_build.py` / `scripts/native_acceptance.py`
  Slice-2 assessment.** The packaged-build harness (S0.11 mechanics: build →
  doctor → authenticated launcher bootstrap → no-CDN dashboard → resource
  manifest) and `native_acceptance.py` (`--slice 0/1/all`, W1-01…W1-07 through
  the packaged public HTTP surface, `--target dev|exe`) are the correct
  vehicles for a future Slice-2 packaged/native pass. A Slice-2 native run
  would drive the three provider-native adapters and the cross-provider merge
  through the *packaged* application on Windows (a W2 analogue); the harness
  never fabricates results and records unexecutable checks as `NOT_RUN`.
- **Honest status.** This sandbox cannot build/execute a packaged Windows
  build, so `verify_packaged_build.py` / `native_acceptance.py` Slice-2
  execution, the packaged resource-manifest verification, and all native
  Windows evidence are recorded as **PENDING** in the status record. Slice 2
  is never claimed PROMOTED from Linux/dev/ordinary-CI evidence alone.

## Gate results (exact outputs, this session)

- `python scripts/verify_slice2.py` → **SLICE 2 AUTOMATED GATE: PASS**
  (gate summary `{"contract": "PASS", "slice2_e2e": "PASS", "tests": "PASS",
  "pip_check": "PASS", "migration_v10_to_latest": "PASS", "doctor": "PASS"}`;
  contract 73 passed; slice2 e2e 12 passed; full regression 1298 passed,
  1 skipped).
- `python -m pytest tests -q` → **1298 passed, 5 skipped** (baseline 1280/5
  at `169d8806…` re-verified as 1280/5; +18 = +7 DF-1 salary unit + +11 DF-2
  feed unit; zero regressions; the 5 skips are unchanged: 1 Linux path skip +
  4 Windows-native).
- `python scripts/verify_slice0.py` → **SLICE 0 AUTOMATED GATE: PASS**
  (`{"tests": "PASS", "pip_check": "PASS", "doctor": "PASS"}`).
- `python scripts/verify_slice1.py` → **SLICE 1 AUTOMATED GATE: PASS**
  (`{"contract": "PASS", "acceptance_e2e": "PASS", "tests": "PASS",
  "pip_check": "PASS", "doctor": "PASS"}`).
- Migration state: **UNCHANGED** — `git hash-object
  src/jobscraper/db/migrations.py` = `161704673ea3ac50e93a7e68c9f4bc593b096bf4`;
  migration gate applied exactly `[11, 12, 13, 14]`, seeded 26 rows across
  13 v10 tables preserved, `integrity_check ok`, `foreign_key_check 0`.
- Dev environment: `/home/user/.venv-311` recreated from
  `requirements/dev.lock.txt` (Python 3.11.2, exact lock, `pip check` clean,
  no new packages). CI on Python 3.12 is authoritative.

## Files in this package

New: `scripts/verify_slice2.py`; `tests/unit/test_normalize_salary.py`;
`tests/unit/test_feed_api_adapter.py`; `docs/reviews/slice-2-s2.9-review-2026-09-10.md`;
`docs/reviews/slice-2-status-report-2026-09-10.md`.
Modified: `src/jobscraper/pipeline/normalize.py` (DF-1: `_RANGE_RE` +
`parse_salary`); `src/jobscraper/adapters/feed_api.py` (DF-2:
`_evidence_refs` + `_failure` signature); `tests/integration/test_ashby_e2e.py`
(DF-1 spine marker: comment dropped, `salary_max` → `210000`).

`verify_slice0.py` / `verify_slice1.py` stay byte-identical; no spec-authority
edit; no sealed-adapter change beyond the two documented findings; the S2.8
acceptance suite's assertions are untouched except the DF-1-approved spine
line.

## Deferred findings

- **DF-1 and DF-2 are closed in this package** (each red-first, with
  regression tests).
- **New deferral from this package: none.**
- Packaged Windows/native Slice-2 evidence (`verify_packaged_build.py`,
  `native_acceptance.py` Slice-2 execution) stays **pending** — it cannot be
  executed here. The status record maps every Slice-2 item honestly and marks
  browser/Adapter Lab/merge-UI-undo/recipes-promotion/authenticated-session
  surfaces as later-slice (06 §72), so this is a truthful record, not a
  promotion claim.

**Stop.** S2.9 closed. Any Slice-3+ / further work requires a separate
explicit continuation decision.
