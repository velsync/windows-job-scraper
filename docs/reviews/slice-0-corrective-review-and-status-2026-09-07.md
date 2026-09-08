# Slice 0 Corrective Review and Final Status — v0.3.1.3

Date: 2026-09-07
Reviewer: implementation agent (self-review, per task §10 corrective review)
Scope: all Slice 0 work packages S0.0–S0.13 on branch `arena/01a07cce-windows-job-scraper`
Status of this document: authoritative record of the Slice 0 corrective review,
the automated-gate evidence, and the native-promotion state.

---

## 1. Final status

**Slice 0 (Foundation / Safety Shell): IMPLEMENTATION COMPLETE — NATIVE
PROMOTION PENDING.**

- All worker packages S0.0 through S0.13 are implemented, tested and committed.
- The **automated gate** passes: `scripts/verify_slice0.py` → tests
  (235 passed / 5 skipped, including 25 contract + 4 acceptance tests),
  `pip check`, and Doctor on an initialized temporary root. Exit 0,
  "SLICE 0 AUTOMATED GATE: PASS".
- The **native acceptance harness** exists and is self-validated in dev mode
  (`--target dev`): W0-01…W0-12, W0-14…W0-18 PASS; W0-13 NOT_RUN
  (Chromium unobtainable on the dev host — Playwright CDN blocked).
- The **native Windows run** (packaged `--onedir` build, `--target exe`) has
  **not** been executed. Per the acceptance strategy, Slice 0 is not PROMOTED
  until that run passes and its evidence is committed. No claim of promotion
  is made anywhere in this repository.

Status update (2026-09-08, docs-only correction): the former "CI activation
pending / workflow parked" statement below is stale. `.github/workflows/ci.yml`
is now active in the repository, and GitHub Actions is green on both Ubuntu
and Windows (Python 3.12) across the accepted Slice-0 foundation and the
Slice-1 lineage (including the S1.1 v10 corrective at `a0caae6`). The
remaining blocker is exclusively the packaged native-Windows
`--target exe` acceptance run; Slice 0 is therefore **not** claimed as fully
native-promoted.

Outstanding for promotion (blockers):

1. **Native Windows packaged execution**: on a Windows host (locally or via
   CI), build the onedir package with `build/jobscraper.spec` and run
   `scripts/native_acceptance.py --target exe --evidence-dir <dir>`.
   W0-13/W0-17 and the packaged-launch checks (W0-01 etc. against the exe)
   can only fully run there. Commit the evidence JSON (it contains no secret
   material by construction; verify before committing).
2. Dev-host constraints that forced NOT_RUN/local-only proof: PyInstaller
   cannot build locally (system Python is statically linked, no shared
   libpython, distro mirrors blocked) and Chromium cannot be downloaded
   (Playwright CDN blocked). Both are environment limits, not implementation
   gaps; both are covered by the active Windows CI.

## 2. Corrective review findings and fixes (this session's review pass)

The review actively looked for fail-open behavior, swallowed exceptions,
races, Windows-hostile code, dead code and safety hazards. Findings and
dispositions:

| # | Severity | Finding | Disposition |
|---|----------|---------|-------------|
| C1 | High (WIN-09 violation, fail-open mutation) | `launcher/doctor.py` `_check_auth_storage` used `load_or_create_install_secret`, which **rotates** a corrupt/undecryptable secret file. Doctor would silently replace user security material instead of reporting an actionable failure. | Fixed: added read-only `load_install_secret_strict` (`security/install_secret.py`) that raises instead of creating/rotating; Doctor uses it. Regression test `test_doctor_corrupt_secret_fails_closed_without_rotation` proves FAIL + byte-identical file. |
| C2 | High (safety hazard on native runs) | `native_acceptance.py` W0-15 killed **every chrome-named process** on the machine — it would have killed the operator's personal Chrome/Edge browsers. | Fixed: W0-15 now targets only the exact worker PID discovered as a direct child of the service PID (`child_pids` via Win32_Process CIM / `ps --ppid`), killed with `taskkill /T /F` (tree) on Windows / recursive SIGKILL on POSIX. No name-matched killing anywhere. Also now verifies the supervisor's bounded restart, service health **and Doctor health** (the oracle's third clause, previously missing). |
| C3 | Medium (Windows CI crash) | `tests/integration/test_launcher_lifecycle.py` used `signal.SIGKILL`, which does not exist on Windows → AttributeError on the Windows CI matrix. | Fixed: `_HARD_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)` (on Windows any non-CTRL os.kill value terminates unconditionally). Harness w05 gained the same guard via `_hard_kill`. |
| C4 | Low (dead code) | `validate_descriptor_freshness` in `runtime_descriptor.py` was never called; its semantics (age ceiling) would have produced false rejections of long-running healthy services and were fully subsumed by the real validation chain (HMAC + PID liveness + process start identity + health probe). | Removed. |
| C5 | Low (hygiene) | Unused imports (`http.client`, `DescriptorError`, `SessionRegistry`, `sys`, `protector_kind`, `utc_now_s`); awkward leftover expressions in the harness. | Cleaned. |

Re-verification after fixes: full suite 235 passed / 5 skipped; `pip check`
clean; `verify_slice0.py` gate PASS; harness dev run
17 PASS / 1 NOT_RUN with W0-15 now proving kill → bounded restart → service
healthy → doctor healthy (evidence: old worker PID killed, new worker PID
observed, `/health/live` 200, doctor `failed == 0`).

## 3. W0 matrix (current truth)

| Check | Dev-target run (2026-09-07) | Native Windows run |
|-------|------------------------------|--------------------|
| W0-01 packaged launch / health / dashboard URL | PASS (dev exe-equivalent launch) | NOT_RUN (pending) |
| W0-02 loopback-only binding | PASS | NOT_RUN |
| W0-03 OS-assigned port, collision-safe | PASS | NOT_RUN |
| W0-04 second launch attaches to existing instance | PASS | NOT_RUN |
| W0-05 stale descriptor rejected, recovery | PASS | NOT_RUN |
| W0-06 old-port impersonator rejected | PASS | NOT_RUN |
| W0-07 install secret survives restart, protected at rest | PASS (dev protector; DPAPI proof is Windows-native) | NOT_RUN |
| W0-08 bootstrap; private routes denied | PASS | NOT_RUN |
| W0-09 bootstrap ticket absent from request-path evidence | PASS | NOT_RUN |
| W0-10 SQLite settings verified | PASS | NOT_RUN |
| W0-11 backup restores into clean isolated root | PASS | NOT_RUN |
| W0-12 pinned tzdata + named zone | PASS | NOT_RUN |
| W0-13 browser isolation smoke | NOT_RUN (CHROMIUM_NOT_INSTALLED on dev host) | NOT_RUN |
| W0-14 no orphan processes after clean shutdown | PASS | NOT_RUN |
| W0-15 browser crash recovery | PASS (post-corrective C2) | NOT_RUN |
| W0-16 service forced-kill recovery | PASS | NOT_RUN |
| W0-17 clean-shell package independence | PASS (dev) | NOT_RUN |
| W0-18 resource observations recorded | PASS | NOT_RUN |

NOT_RUN semantics: recorded with reason in `native-acceptance.json`; the
harness exit code is non-zero only for FAIL, and every NOT_RUN must be
explainable by the environment (here: browser runtime unobtainable /
packaged build deferred to Windows).

## 4. Corrective lineage (salvage map)

The authority documents reference repair lineages (`repair/slice0-v0313`).
The implementing session was constrained to branch
`arena/01a07cce-windows-job-scraper` (session-fixed). That branch **is** the
Slice 0 corrective lineage; `2738a81` is its head and is the commit to be
mapped to `repair/slice0-v0313` head when the lineage is reconciled:

```
fdae8a7  S0.0   repository + dependency skeleton
4a32d85  CI     Python 3.12 test gate (later parked at build/ci/)
987e336  S0.1   paths, config, timeutil
c4bf577  S0.2–S0.4  SQLite foundation, backup-before-migration, restore, events + redaction
c8edc2d  S0.5–S0.7  security primitives, service shell, pywin32 auth chain
ca41ce8  S0.8   launcher, single-instance, signed descriptor, lifecycle
2c69532  CI     workflow parked (token lacks workflows permission)
ddbc007  S0.9   browser worker process, protocol v1, supervisor
88bf09a  S0.10–S0.11  Doctor, timezone proof, onedir package + verify harness
2738a81  S0.12–S0.13  automated gate + native acceptance harness
+ corrective commit (this review: C1–C5)
```

## 5. Later-slice prerequisites created in Slice 0

**None.** The Slice 0 boundary was respected: no queue, no workers' delivery
machinery, no scheduler, no scraping, no LLM, no browser beyond the inert
protocol smoke. Facilities that later slices will reuse (SQLite foundation
with backup-before-migration, event log, redaction, security shell, launcher
lifecycle, browser-worker process isolation, Doctor) are all Slice-0 scope
per the plan. The only forward-looking artifacts are the CI workflow
(activated after this review at `.github/workflows/ci.yml`; it also contains
the Windows browser-runtime install and inert smoke) and the native
acceptance harness — both acceptance infrastructure, not product scope
creep.

## 6. Residual known limitations (explicit, not hidden)

- Browser runtime containment on POSIX dev hosts relies on process-tree
  kills; Windows Job-Object containment (WIN-05) is deliberately deferred
  to post-native-acceptance per the spec, and the supervisor already binds
  restarts to a bounded window (5 restarts / 300 s, backoff capped at 10 s).
- The service starts its browser worker eagerly at boot; the worker performs
  no work until a request arrives (no scrape routes exist in Slice 0).
- Duplicate-`Set-Cookie` handling in tests uses the http.client headers
  object (`.get_all`), since plain dicts collapse duplicates.
