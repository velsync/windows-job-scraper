# Slice 1 Status Report — v0.3.1.3

Date: 2026-09-08
Scope: all Slice 1 work packages S1.2–S1.12 on branch
`arena/01a07cce-windows-job-scraper`, starting from the accepted Slice-1
lineage head `a0caae6` (S1.0/S1.1 accepted, v10 schema).
Authority: docs/spec/v0.3.1.3 (normative), docs/plans/slice-1-worker-
implementation-plan-v0313.md (plan).
Status of this document: authoritative record of Slice-1 implementation,
the automated acceptance gate, the corrective review, and the
native-promotion state.

---

## 1. Final status

**Slice 1 (Worker): IMPLEMENTATION COMPLETE — AUTOMATED ACCEPTANCE
COMPLETE — NATIVE PROMOTION PENDING.**

- All worker packages S1.2 through S1.12 are implemented, tested and
  committed (one coherent commit per package plus separately committed
  correctives).
- The **automated gate** passes: `scripts/verify_slice1.py` → contract
  suite, Slice-1 E2E acceptance, full regression, `pip check`, and
  Doctor on an initialized isolated root. Exit 0,
  `SLICE 1 AUTOMATED GATE: PASS` (all five sub-gates PASS).
- The **native Windows harness** (`scripts/native_acceptance.py`) now
  carries the Slice-1 matrix (`--slice {0,1,all}`). Its logic is
  self-validated on the development host (`--target dev --slice 1`):
  W1-01…W1-07 all PASS.
- The **native Windows packaged run** (`--target exe`) has **not** been
  executed: this development sandbox is Linux, PyInstaller cannot build
  here (no shared libpython, distro mirrors blocked), and the Playwright
  CDN is blocked. Per the plan, Slice 1 is **not** native-promoted until
  that run passes on a Windows host and its evidence is committed. No
  claim of native promotion is made anywhere in this repository.
- GitHub Actions CI (Python 3.12) is green on **ubuntu-latest** across
  the whole slice. The windows-latest matrix initially failed on two
  platform-naive test assertions in the new S1.11 contract suite (never
  product code); both are fixed and the final state is recorded in §5.

## 2. Package ledger (S1.2 → S1.12)

| Package | Commit | Contents (files changed) | Focused tests |
|---------|--------|--------------------------|---------------|
| docs corrections (pre-S1.2) | `66e227f` | plan + slice-0 status doc (job_sources.job_id NOT NULL per RUN-11; CI-status correction) | — |
| S1.2 URL normalization / destination policy / safe links | `7c39a77` | `src/jobscraper/net/` (urlnorm.py, destination.py, safelinks.py) | 58 new (test_urlnorm 12, test_destination 18+, test_safelinks 5, +net integration live 6) |
| S1.3 durable run/request core | `59e65c7` | `src/jobscraper/runtime/` (clock, runs, requests, claims, fence, cancellation) | 15 (test_runtime_core: uniqueness, claim races, lease expiry/reclaim, fenced commit, §18 cancellation, RUN-01 truth table) |
| S1.4 host HTTP executor + envelope + page gate | `39f5178` | `src/jobscraper/acquisition/` (failures, envelope, result, httpexec, pagevalidity) | 20 (test_http_executor, live loopback server) |
| S1.5 adapter contract + JSON feed adapter | `33d5314` | `src/jobscraper/adapters/` (contract, feed_api, registry) | 13 (test_adapter_contract) |
| S1.6 observation → canonical pipeline | `143b7b7` | `src/jobscraper/pipeline/` (normalize, entity, canonical, ingest, coverage); `runtime/requests.py` commit=False | 14 (test_pipeline_ingest) |
| S1.7 obligations + eligibility/scoring | `61fd407` | `pipeline/` (eligibility, scoring, obligations, driver scaffolding via profiles/core), fence §18-awareness, claims types filter | 7 (test_obligations_evaluation) |
| S1.8 profile-relative Inbox | `5e6fee5` | `src/jobscraper/inbox/` (state, events) + obligations wiring | 9 (test_inbox) |
| S1.9 applications + direct apply | `845be09` | `src/jobscraper/applications/` (core, applylink) + listing-closed events | 7 (test_applications) |
| S1.10 service API + dashboard + run driver | `148f894` | `pipeline/driver.py`, `service/s1_routes.py`, dashboard (templates/dashboard.html, static/app.js), inbox_feed one-row-per-job, run counters | 10 (test_run_driver 3, test_s1_routes 7) |
| §18/restart-recovery corrective | `ebd38a7` | `runtime/recovery.py`, driver §18 mid-run handling, runner startup recovery, honest cancel route | +8 (test_recovery 4, driver 2, routes 2) |
| §42 inbox-trigger corrective | `a263a6b` | `inbox/events.py` (MEANINGFUL_CHANGE requires revision ≥ 2) | +1 |
| S1.11 contract + acceptance + gate | `ef048da` | `tests/contract/test_slice1_contract.py`, `tests/integration/test_slice1_acceptance.py`, `scripts/verify_slice1.py` | 14 (11 contract + 3 E2E acceptance) |
| S1.11 Windows stability fixes | `7950935` | contract test platform-stable (fcntl), E2E margins widened | — |
| absence-authority corrective | `72beffa` | `acquisition/pagevalidity.py` (failed fetch never classifies EMPTY) | +2 |
| S1.12 W1 native matrix | `6c11a4f` | `scripts/native_acceptance.py` (--slice, W1-01..W1-07) | dev-run validated (7 PASS) |
| CI failure annotations | `b4e9454` | `tests/conftest.py` (GitHub ::error annotations under Actions) | — |
| contract path-separator fix | `98e2270` | `tests/contract/test_slice1_contract.py` (as_posix) | — |

Full-suite progression (focused → full regression before every commit):
257/5 (baseline) → 315/5 (S1.2) → 330/5 → 350/5 → 363/5 → 377/5 → 384/5 →
393/5 → 400/5 (S1.9) → 410/5 (S1.10) → 428/5 (recovery corrective) →
432/5 (S1.11) → **434 passed / 5 skipped** (final, after the
absence-authority corrective; `pip check` clean at every step).

## 3. Schema and migrations

- Schema version **10** — unchanged for the whole slice. No migration
  steps were added and no released bytes were altered (contract test
  pins versions 1…10 sequential + SHA-256 of every step).
- No schema change was needed: the accepted S1.1 v10 schema (including
  the corrective) already covered every Slice-1 table.

## 4. Automated acceptance (S1.11)

`python scripts/verify_slice1.py` (exit 0):

```text
[slice1-gate] contract-suite:        PASS  (tests/contract — 36 tests:
      slice boundary, provenance-first, browser/service boundary,
      security shell route gating, migration discipline, dependency
      discipline, no-network-in-transaction)
[slice1-gate] slice1-e2e-acceptance: PASS  (3 tests, real launcher +
      service subprocesses: full vertical path + restart persistence +
      idempotent resume; durable mid-run cancellation through the live
      service; hard crash-kill → launcher restart → SERVICE_RECOVERY
      reclaim → cancel finalizes → resume with no duplicate observations)
[slice1-gate] full-regression:       PASS  (434 passed / 5 skipped)
[slice1-gate] pip-check:             PASS
[slice1-gate] doctor:                PASS  (initialized isolated root)
[slice1-gate] SLICE 1 AUTOMATED GATE: PASS
```

## 5. CI (GitHub Actions, Python 3.12, ubuntu + windows)

- ubuntu-latest: **green** for every push of the slice (S1.6…S1.9 pushes
  and all later pushes).
- windows-latest: green through S1.9. The S1.11/S1.12-era pushes
  (`ef048da`, `7950935`, `b4e9454`) failed on windows-latest only; all
  three failures were in the **new test code**, never in product code:
  1. `fcntl` (POSIX-only stdlib import in the launcher's non-Windows
     fallback) rejected by the platform-naive dependency contract check
     — fixed in `7950935`;
  2. `str(relative_path)` separator mismatch in the
     only-pipeline-writes-canonical-jobs contract check — fixed in
     `98e2270` (surfaced via the new CI failure annotations, since raw
     run-log downloads are unavailable from the review environment);
  3. no further failure annotations after the fixes (the E2E acceptance
     suite passed on windows-latest with the widened timing margins).
- Final CI state for the slice HEAD: recorded in §7 (updated after the
  last push settled).

## 6. Corrective review findings and fixes (whole-slice pass)

The dedicated review actively looked for fail-open security, claim/lease/
fence races, stale-owner commits, cancellation violations, non-idempotent
retries, migration corruption, missing FK/uniqueness, provenance gaps,
absence-authority mistakes, native-ID reuse, RUN-21 regressions,
duplicate inbox events, restart-persistence failures, unsafe links/SSRF
bypasses, browser/service boundary violations, Windows-specific failures
and stale docs. Genuine findings and their fixes:

| # | Severity | Finding | Fix (commit) |
|---|----------|---------|--------------|
| R1 | High (§18 violation) | Durable cancellation landing mid-run escaped `execute_run` as an uncaught `StaleOwnership` (HTTP 500) and left the run permanently non-terminal; the cancel route always answered `{"status": "CANCELLED"}` regardless of truth. | Driver observes the fence refusal, cooperatively abandons, aggregates CANCELLED; lease-loss without cancellation stops the plan cleanly and reclaims; cancel route is honest (finished runs keep their outcome; live runs report RUNNING); `runtime/recovery.py` + runner startup recovery reclaim restart-orphaned requests and finalize interrupted cancellations. (`ebd38a7`) |
| R2 | Medium (PROD-02/§42) | Every first sighting emitted BOTH `NEW_ELIGIBLE_APPEARANCE` and `MEANINGFUL_CHANGE` (trigger keyed to content revision 1) — the two trigger identities were not distinguishable. | `maybe_emit_inbox_event` declines MEANINGFUL_CHANGE below revision 2 (NOT_A_CHANGE). (`a263a6b`) |
| R3 | High (absence-authority corruption, §21/RUN-13) | A redirect hop denied by destination policy produced a `POLICY_REJECTED` envelope that still carried the hop's 302 status; `classify_page`'s transport-failure guard only recognized failures without a status code, so the empty body classified as `EMPTY` → `SUCCESS_EMPTY` → terminal enumeration → group SATISFIED → coverage COMPLETE with absence authority over a fetch that never reached the source. Found by the W1 native harness itself. | Any failed envelope that does not match an explicit status-driven state classifies `UNKNOWN` with typed failure evidence; regression tests at executor and driver level (run PARTIAL, coverage PARTIAL, `terminal_enumeration_proven=0`). (`72beffa`) |
| R4 | Medium (restart semantics gap) | Nothing reclaimed requests orphaned by a service restart (RUN-09 single-machine coordinator), so a crashed run stayed RUNNING forever and its cancelled sibling never finalized. | Recovery on the fresh service epoch reclaims orphans (ABANDONED/SERVICE_RESTART → RETRY_WAIT within budget / FAILED when exhausted) and finalizes interrupted cancellations; wired into `service/runner.py` with `SERVICE_RECOVERY` events. (`ebd38a7`) |
| R5 | Low (test-platform bugs, Windows CI) | Two platform-naive assertions in the new contract suite (`fcntl` stdlib check; path-separator comparison) failed windows-latest deterministically. | `7950935`, `98e2270`; plus CI failure annotations (`b4e9454`) so future failures are diagnosable without raw log access. |

Reviewed and confirmed clean (no action): claim single-winner and fence
verification under `BEGIN IMMEDIATE`; reclaim/recovery re-checks under
the write lock; request-scoped observation idempotency and cursor-based
resume (E2E-proven no duplicates); RUN-21 stale protection; native-ID
reuse guard; sticky dispositions across restart; safelink gating on
every surfaced URL (API and dashboard); adapter isolation from the DB
(contract-enforced); Playwright confined to the browser worker
(contract-enforced); loopback grants only for loopback entry-host
literals (scraped content can never construct a grant).

## 7. Final CI verification for the slice HEAD

- HEAD: `98e2270` (see §8 for the push-state caveat that applied earlier
  in the session).
- ubuntu-latest: **success** (run for `98e2270`).
- windows-latest: **success** (run for `98e2270`) — recorded after the
  run settled; see the repository Actions history for the machine
  evidence.

## 8. NOT_RUN items (exact reasons)

1. **Native Windows packaged acceptance (`--target exe`, W0 + W1)**:
   requires a Windows host with a PyInstaller onedir build. This
   development sandbox is Linux; PyInstaller cannot build here (host
   Python statically linked, distro mirrors blocked) and the Playwright
   CDN is blocked. The harness logic is dev-validated; the packaged run
   must be executed on Windows (locally or via CI) and its evidence
   committed before native promotion is claimed.
2. **Browser-worker W1 interactions**: Slice 1's driver executes HTTP
   feeds only; the browser worker is exercised by the Slice-0 harness
   (W0-13/14/15) and CI smoke, not by W1 (no browser strategy exists in
   Slice 1 — per ROAD-02/03 boundaries).
3. **In-repo evidence JSON from native runs**: none exists yet (follows
   from item 1).

## 9. Residual limitations (known, accepted)

- The minimal eligibility engine emits ELIGIBLE/LIKELY/UNCLEAR/UNLIKELY
  but never INELIGIBLE on Slice-1 evidence classes, so the §42
  `eligibility != INELIGIBLE` inbox exclusion is currently vacuous;
  clearly-mismatched jobs surface as UNLIKELY (per the §42 predicate,
  which excludes only INELIGIBLE) and dismissal is the sticky feedback
  channel. Extending evidence classes (work-authorization, EOR, JSON-LD)
  is later-slice scope.
- Runs execute synchronously inside `POST /api/runs` (single-user local
  product; bounded by the driver's stop policy); a background scheduler
  is Slice-3 scope.
- Single builtin adapter (`json_api_feed`); multi-adapter breadth,
  fingerprinting, strategy router, origin resolver are deferred
  (ROAD-02/03).
- No FTS5/search expansion in Slice 1 (ROAD-03; Doctor verifies the
  capability only).
- Windows-native execution of the whole slice is proven by CI (test
  matrix) but not yet by the packaged-exe acceptance run (§8.1).

## 10. Verdicts

- Slice 1 **IMPLEMENTATION COMPLETE**: yes (S1.2–S1.12 committed).
- Slice 1 **AUTOMATED ACCEPTANCE COMPLETE**: yes
  (`scripts/verify_slice1.py` PASS — §4).
- Slice 1 **NATIVE WINDOWS ACCEPTANCE COMPLETE**: **no** — pending the
  packaged `--target exe` run on a Windows host (§8.1).
- Slice 1 **ACCEPTED / READY FOR SLICE 2**: **implementation and
  automated acceptance only**; native promotion is the remaining gate
  the plan requires before declaring the slice fully accepted.
