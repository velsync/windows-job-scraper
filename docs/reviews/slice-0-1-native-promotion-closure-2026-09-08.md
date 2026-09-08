# Slice 0 + Slice 1 Native Promotion Closure — v0.3.1.3

Date: 2026-09-08
Repository: `velsync/windows-job-scraper`
Accepted lineage: `arena/01a07cce-windows-job-scraper`
Status: **FINAL PROMOTION RECORD — CONTROLLING FOR SLICE 0/1 NATIVE STATUS**

This record supersedes only the earlier *pending native-promotion status statements* in:

- `docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md`
- `docs/reviews/slice-1-status-report-2026-09-08.md`

Those documents remain authoritative historical implementation/corrective-review records. Where they say the packaged Windows run is still pending, this later closure record controls.

---

## 1. Final verdict

### Slice 0 — Foundation / Safety Shell

**PROMOTED.**

All S0.0–S0.13 implementation work is complete, the automated gate has passed, the real PyInstaller `--onedir` package has been verified on Windows, and packaged native acceptance W0-01 through W0-18 all passed with no `FAIL` and no `NOT_RUN`.

### Slice 1 — Minimal final-architecture product MVP

**ACCEPTED — NATIVE WINDOWS ACCEPTANCE COMPLETE.**

All S1.0–S1.12 implementation work is complete, Slice-1 automated acceptance has passed, and packaged native acceptance W1-01 through W1-07 all passed with no `FAIL` and no `NOT_RUN`. The same final run also re-proved the complete W0 foundation matrix against the accepted candidate.

**Slice 2 is no longer blocked by Slice 0/1 native promotion.** It has not started on the accepted lineage and must begin only as an explicitly approved bounded v0.3.1.3 work package/plan.

---

## 2. Exact accepted identities

| Item | Accepted value |
|---|---|
| Packaged application source commit | `2bb4f8319a2dfd312db21a19010cb0720359063e` |
| Acceptance-harness commit | `92995d990c224715a3dea18cbf5e7d59f0126053` |
| Native evidence commit | `7def768f75214b692d98efaa0f559e3a22f4f7af` |
| Build ID | `onewise-0883c62196a1a4bf` |
| Build SHA-256 | `0883c62196a1a4bf39b3092a1a5068d0cf50a143159823daf3a312e7f54f4dff` |
| Package kind | PyInstaller `--onedir` |
| Package file count | 1,015 |
| Python | 3.12.14 |
| PyInstaller | 6.22.2 |
| Playwright | 1.57.0 |
| tzdata | 2026.2 |

The packaged application source commit is intentionally distinct from the later acceptance-harness commit: the W0-14 corrective changed the acceptance oracle/test only, not packaged application code. The final matrix therefore exercised the already-built package against the corrected harness.

---

## 3. Packaged verification

Committed record:

`artifacts/slice0/onewise-0883c62196a1a4bf/package-verification.json`

Result: **PASS**.

Measured package facts include:

- build ID `onewise-0883c62196a1a4bf`;
- SHA-256 `0883c62196a1a4bf39b3092a1a5068d0cf50a143159823daf3a312e7f54f4dff`;
- 1,015 packaged files;
- packaged Doctor exit code 0 with zero failed checks;
- loopback-only bootstrap path verified;
- bootstrap ticket kept out of the query string;
- private route denied without session;
- local dashboard assets present;
- no CDN dependency in the packaged dashboard resource check;
- required resource manifest complete.

The initial packaged-build attempt exposed a harness-only PyInstaller invocation error (`--specpath` used with an existing `.spec` file). That was corrected before the accepted package was built; no environment rebuild or architecture change was required.

---

## 4. Final packaged native matrix

Committed record:

`artifacts/slice0/onewise-0883c62196a1a4bf/native-acceptance.json`

Raw successful harness record:

`artifacts/slice0/onewise-0883c62196a1a4bf/native-acceptance.raw.json`

### W0

| Check | Result |
|---|---|
| W0-01 packaged launch / health / dashboard URL | PASS |
| W0-02 loopback-only binding | PASS |
| W0-03 collision-safe OS-assigned port | PASS |
| W0-04 second launch attaches to existing instance | PASS |
| W0-05 stale descriptor recovery | PASS |
| W0-06 old-port impersonation rejected | PASS |
| W0-07 protected install secret / ACL / restart stability | PASS |
| W0-08 bootstrap succeeds; private route denied without session | PASS |
| W0-09 bootstrap ticket absent from request-path evidence | PASS |
| W0-10 packaged SQLite settings | PASS |
| W0-11 backup restores into clean isolated root | PASS |
| W0-12 packaged timezone proof | PASS |
| W0-13 isolated browser worker + Chromium launch | PASS |
| W0-14 no orphan candidate browser/worker processes | PASS |
| W0-15 browser-worker crash recovery | PASS |
| W0-16 forced service termination recovery | PASS |
| W0-17 clean-environment package independence | PASS |
| W0-18 resource observations captured | PASS |

**W0 total: 18/18 PASS.**

### W1

| Check | Result |
|---|---|
| W1-01 packaged vertical slice | PASS |
| W1-02 restart persistence | PASS |
| W1-03 idempotent resume / no duplicates | PASS |
| W1-04 SSRF private/test destination denied fail-closed | PASS |
| W1-05 unauthorized redirect hop denied | PASS |
| W1-06 unsafe source links never surfaced clickable | PASS |
| W1-07 Doctor healthy after Slice-1 workload | PASS |

**W1 total: 7/7 PASS.**

Final native result: **25/25 PASS; zero FAIL; zero NOT_RUN.**

---

## 5. W0-14 corrective history

The first full packaged native attempt produced 24/25 PASS with only W0-14 failing.

Investigation found an acceptance-oracle defect: on Windows the harness compared raw `tasklist /FO CSV` rows, whose memory-usage field is volatile. Existing Chrome processes could therefore be falsely classified as new residual processes when only their memory counters changed.

The first corrective changed W0-14 to compare stable `(image name, PID)` identities and added a regression test. A subsequent diagnostic showed that the user's earlier failed evidence had actually been produced before pulling that corrective. After updating to acceptance-harness commit `92995d990c224715a3dea18cbf5e7d59f0126053`:

- focused W0-14 diagnostic: PASS;
- residual count: 0;
- residual process identities: none;
- final full W0+W1 packaged matrix: 25/25 PASS.

No packaged application code changed during this W0-14 correction, so the accepted package build remained the same exact candidate.

---

## 6. CI verification

GitHub Actions run `34231974630` for evidence commit `7def768f75214b692d98efaa0f559e3a22f4f7af` completed successfully on both:

- `ubuntu-latest` / Python 3.12: success;
- `windows-latest` / Python 3.12: success.

The Windows job also successfully completed the pywin32 primitive proof, pinned Chromium installation, and inert browser-worker Chromium smoke.

CI remains a separate layer from packaged native acceptance; this closure records that **both** layers are green for the accepted state.

---

## 7. Evidence inventory

Committed under:

`artifacts/slice0/onewise-0883c62196a1a4bf/`

Files:

- `package-verification.json`
- `native-acceptance.json`
- `native-acceptance.raw.json`
- `doctor.json`
- `process-inventory-before.txt`
- `process-inventory-during.txt`
- `process-inventory-after.txt`
- `resource-measurements.json`

The finalized `native-acceptance.json` identifies the exact packaged source commit, acceptance-harness commit, build ID, automated gate PASS, native gate PASS, zero unresolved critical findings, and `promotion: PASS`.

---

## 8. Promotion-rule disposition

For the accepted candidate:

- exact source commit identified: **YES**;
- exact package build identified: **YES**;
- automated gate: **PASS**;
- packaged verification: **PASS**;
- W0-01…W0-18: **ALL PASS**;
- W1-01…W1-07: **ALL PASS**;
- unresolved critical security/recovery findings: **0**;
- final native evidence committed: **YES**;
- CI at evidence commit: **PASS on Ubuntu and Windows**.

Therefore:

- **SLICE 0 — PROMOTED**
- **SLICE 1 — ACCEPTED / NATIVE WINDOWS ACCEPTANCE COMPLETE**
- **SLICE 2 — ELIGIBLE TO BEGIN WHEN EXPLICITLY AUTHORIZED; NOT YET STARTED ON THE ACCEPTED LINEAGE**

Any later behavior-bearing or packaging change must be assessed against the owning v0.3.1.3 authority for the appropriate automated/package/native revalidation before a new promotion claim is made.
