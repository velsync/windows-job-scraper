# Slice 2 pre-S2.7 Host Invariant Hardening — 2026-09-09

## Verdict

**Two bounded correctives applied, both red-before-green through the real
driver and durable pipeline. Repository sealed in a safer state before S2.7.
S2.7 (Ashby) has not begun.**

## Authority and scope

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3**
- Branch: `arena/01a08691-windows-job-scraper`
- **Base SHA:** `40cfe710ebf1a75765730d75d13577209ee357ec` (S2.6 corrective
  acceptance; CI `34383135549` SUCCESS Ubuntu + Windows / 3.12)
- **Final SHA:** `ab1e58f3e2198ea9fd77de0d2e48735d9c2d002f` (corrective commit; this seal note is the only follow-up commit)
- Scope: exactly the two findings carried forward by
  `docs/reviews/slice-2-s2.6-corrective-review-2026-09-09.md` §"Carried
  forward". No S2.7, no general S2.9, no adapter-API/queue/coverage
  redesign, no migration, no Lever behaviour change.

Pre-conditions verified before editing: branch and HEAD exactly
`40cfe710…`, remote ref identical, working tree clean, migration blob
`161704673ea3ac50e93a7e68c9f4bc593b096bf4`.

## Authoritative requirements relied upon

- **02 ACQ-03** — PARTIAL: "the intended enumeration/parse unit did not
  complete sufficiently to claim authoritative coverage … enumeration
  coverage is recorded as PARTIAL; absence inference is forbidden for that
  coverage record"; PARTIAL is "neither equivalent to SUCCESS_EMPTY nor a
  blanket FAILURE" — good observations persist.
- **03 §40** — absence evidence only when `completion_state = COMPLETE`,
  authority is absence-capable, **and `absence_inference_allowed = true`**;
  each `enumeration_coverage` row is one immutable generation.
- **03 RUN-13** — "A PARTIAL … enumeration MUST NOT generate absence
  evidence."
- **03 §18 / RUN-07** — request-owned outputs commit only under the fence.
- **02 ACQ-09** — outcomes reference the durable evidence supporting them.
- **02 ACQ-02 / ARC-04** — host owns authority; adapters plan/parse only.

## Corrective A — host-level `PARTIAL ⇒ coverage degraded`

### Reproduced pre-fix behaviour (red)

A minimal host-owned adversarial adapter (`_ProbeAdapter` in
`tests/integration/test_host_partial_coverage_invariant.py`, registered
only for the test via `monkeypatch.setitem` on `BUILTIN_ADAPTERS`) emits a
scripted `PARTIAL + cursor → clean short page` sequence through the real
driver, fence, SQLite and coverage pipeline. Against `40cfe710…`:

```
execute_run → "SUCCEEDED"
enumeration_coverage: completion_state=COMPLETE, terminal_enumeration_proven=1,
                      absence_inference_allowed=1
run_source_plans.group_outcome = SATISFIED
```

i.e. a generation containing a membership-degrading PARTIAL unit was
finalized as absence-authoritative. Six of seven new driver tests failed
on the base (the seventh is the clean-pagination control and passes both
before and after).

### Root cause

`_execute_plan` set `coverage_degraded = True` only from the per-page
`signal["value"] == "PARTIAL"` branch. When the adapter also proposed a
cursor, the earlier `CONTINUE` branch returned first, so the flag was never
set. Degradation was also purely in-memory: a process restart between the
PARTIAL page and its continuation started a fresh pass with
`coverage_degraded = False`.

### Corrective behaviour

The existing durable column `enumeration_coverage.absence_inference_allowed`
is used as the degradation mechanism — no new column, table or migration.

1. **`coverage.degrade_coverage(conn, coverage_id, reason, commit)`** (new,
   `pipeline/coverage.py`): sets `absence_inference_allowed = 0` on an
   *open* generation; one-way (`1 → 0` only), idempotent, leaves finalized
   generations untouched; records the reason in `stop_reason` if none set.
2. **`coverage.is_coverage_degraded(conn, coverage_id)`** (new): durable
   read used by a resumed pass.
3. **Driver, inside the fenced `mutate`** for every parsed page: if
   `outcome.kind is PARTIAL` and the unit contributes to coverage
   (`is_enumeration or not listing_identity_sufficient`) and the generation
   is not already finalized, `degrade_coverage(..., commit=False)` runs
   **before** `_dispatch_child_tasks`, `next_cursor`, and the
   `CONTINUE/PARTIAL/TERMINAL` decision. The degradation therefore commits
   atomically with the PARTIAL page's observations under the same fence.
4. **Driver, after the commit**: `signal["degraded"]` forces
   `coverage_degraded = run_degraded = True` regardless of which signal path
   returned.
5. **Driver, pass start**: `coverage_degraded = is_coverage_degraded(...)`
   and `run_degraded = coverage_degraded` for an open (unfinalized)
   generation, so a resumed pass inherits the durable state.
6. **Driver, finalization**: `terminal_proven = terminal and not
   coverage_degraded`; a degraded generation finalizes `PARTIAL` with
   `stop_reason = "degraded by PARTIAL acquisition unit"` and
   `terminal_enumeration_proven = 0` even when its last page was short or
   empty.
7. **`finalize_coverage` barrier** (belt-and-braces, host-owned): refuses
   `COMPLETE` for an absence-capable generation whose
   `absence_inference_allowed` is 0, with `CoverageFinalizationError`. This
   holds even if a future driver change bypasses (3)–(6).

PARTIAL is **not** turned into FAILURE: good observations, seen identities,
child tasks and continuation all still commit; the run ends
`SATISFIED_PARTIAL`.

### Tests added (Corrective A)

`tests/integration/test_host_partial_coverage_invariant.py` (7, real driver):

| Test | Proves |
|---|---|
| `test_partial_with_cursor_cannot_be_laundered_into_complete_by_a_clean_terminal_page` | **key red test**: (2) no COMPLETE, (3) `absence_inference_allowed=0`, (4) `terminal_enumeration_proven=0`, no `last_absence_coverage_id` points at it, (5) all 3 observations + seen identities persisted, `SATISFIED_PARTIAL` |
| `test_the_degradation_is_durable_the_moment_the_partial_page_commits` | (1) crash injected before page 2 is claimed: open generation already has `absence_inference_allowed=0` |
| `test_restart_between_the_partial_page_and_the_continuation_cannot_erase_degradation` | (6) resumed pass finishes the *same* generation PARTIAL, `SATISFIED_PARTIAL` |
| `test_a_partial_page_followed_by_a_recognized_empty_page_is_still_not_complete` | the `EMPTY` terminal route cannot launder either |
| `test_a_partial_page_in_the_middle_of_a_walk_degrades_the_whole_generation` | degradation is generation-wide, not page-local |
| `test_a_clean_paginated_generation_still_completes_with_absence_authority` | (7) control: 3-page clean walk → COMPLETE, `absence_inference_allowed=1`, `SATISFIED` |
| `test_a_clean_generation_after_a_degraded_one_regains_absence_authority` | degradation is per generation; the *next* COMPLETE generation is what judges `p-2` UNCERTAIN, with `last_absence_coverage_id` naming it |

`tests/integration/test_pipeline_ingest.py` (3, coverage primitive):
`…refuses_complete_for_a_degraded_generation` (barrier + idempotence +
PARTIAL still finalizable, no absence applied),
`…leaves_a_finalized_generation_untouched`,
`…is_a_no_op_for_non_absence_authorities`.

## Corrective B — Greenhouse FAILURE `evidence_refs`

### Reproduced pre-fix behaviour (red)

`GreenhouseAdapter._failure` built `ParseOutcome(kind=FAILURE, …)` without
`evidence_refs`; all five new tests failed on the base with
`evidence_refs=()` (changed template, all-members-rejected, malformed body,
three detail failure bodies, health-probe failure).

### Corrective behaviour

Identical shape to the S2.6 Lever F3 fix: `_failure` takes the
`ValidatedResult`, builds the `FailureRecord` from `result.envelope` as
before, and attaches `evidence_refs=_evidence_refs(result)` (envelope ref +
validity-evidence ref, deduplicated). Four call sites updated
(`parse` unusable body, `_parse_probe`, `_parse_list` items-marker-missing,
`_parse_list` all-rejected). The two `_parse_detail` FAILURE constructions
already carried `refs`. No other Greenhouse behaviour touched.

### Tests added (Corrective B)

`tests/unit/test_greenhouse_adapter.py::TestFailureTraceability` (5).

## Red-before-green evidence

With **only** `greenhouse.py`, `coverage.py`, `driver.py` stashed (i.e.
production at `40cfe710…`, new tests present):

```
11 failed, 107 passed   ← exactly the 11 new behavioural tests
tests/integration/test_pipeline_ingest.py: ImportError: cannot import name
  'degrade_coverage'   ← the 3 primitive tests cannot even import on the base
```

With the fixes restored: all green (below).

## Verification (Python 3.11.2 sandbox; CI on 3.12 authoritative)

| Command | Result |
|---|---|
| `pytest tests/integration/test_host_partial_coverage_invariant.py` | 7 passed |
| `pytest tests/integration/test_pipeline_ingest.py -k degrad` | 3 passed |
| `pytest tests/unit/test_greenhouse_adapter.py -k TestFailureTraceability` | 5 passed |
| Focused: driver, ingest, Greenhouse unit+e2e, S2.5 runtime correctives, Lever unit+e2e, contract | 412 passed |
| `python -m pytest tests/unit tests/contract tests/integration -q` | **1078 passed, 1 skipped** (base: 1063 / 1; +15) |
| `python scripts/verify_slice0.py` | `SLICE 0 AUTOMATED GATE: PASS` — `{"tests":"PASS","pip_check":"PASS","doctor":"PASS"}` |
| `python scripts/verify_slice1.py` | `SLICE 1 AUTOMATED GATE: PASS` — `{"contract":"PASS","acceptance_e2e":"PASS","tests":"PASS","pip_check":"PASS","doctor":"PASS"}` |

S2.4/S2.5/S2.6 suites unchanged and green. **Lever behaviour unchanged**:
no Lever file modified; `test_lever_e2e.py` (24) and
`test_lever_adapter.py` (159) pass as-is, including the S2.6 F1 test
(adapter-side stop) which now sits behind the host barrier as well.

## Migration state

`src/jobscraper/db/migrations.py` blob SHA
`161704673ea3ac50e93a7e68c9f4bc593b096bf4` — **unchanged**. No new
migration; the durable mechanism is the pre-existing
`absence_inference_allowed` column.

## Files changed

- `src/jobscraper/pipeline/coverage.py` — `degrade_coverage`,
  `is_coverage_degraded`, COMPLETE barrier for degraded generations
- `src/jobscraper/pipeline/driver.py` — durable degrade under the fence
  before continuation; resumed-pass inheritance; `terminal_proven`
- `src/jobscraper/adapters/greenhouse.py` — `_failure` carries
  `evidence_refs`
- `tests/integration/test_host_partial_coverage_invariant.py` — new (7)
- `tests/integration/test_pipeline_ingest.py` — +3
- `tests/unit/test_greenhouse_adapter.py` — +5
- `docs/reviews/slice-2-pre-s2.7-host-invariant-hardening-2026-09-09.md`

## CI

Run **`34386835053`** on `ab1e58f3…` — **SUCCESS**:
`Automated test gate (Python 3.12, ubuntu-latest): success`,
`Automated test gate (Python 3.12, windows-latest): success`.

## Intentionally deferred (recorded, not expanded into)

- **`stop_reason` overloading.** `degrade_coverage` records its reason in
  `stop_reason` only when that column is still NULL; the driver's own
  finalization reason wins otherwise. A dedicated `degradation_reason`
  column would be cleaner but requires a migration — out of scope by
  instruction; the durable *flag* is unambiguous regardless.
- **`_outcome_from_durable_state`** (restart landing after all requests
  closed but before `set_group_outcome`) derives SATISFIED from any
  finalized COMPLETE generation. After this corrective a degraded
  generation can never be COMPLETE, so it is consistent; no change needed.
- **FeedApi adapter `_failure`** has the same evidence-refs shape as
  pre-fix Greenhouse. Not in the instructed scope (Greenhouse only);
  recommend folding into S2.9.

## Seal

- Corrective commit: `ab1e58f3e2198ea9fd77de0d2e48735d9c2d002f`
- CI run `34386835053`: SUCCESS (ubuntu-latest + windows-latest, Python 3.12)
- Sealed by this documentation-only commit; no production or test file
  differs from `ab1e58f3…`.
