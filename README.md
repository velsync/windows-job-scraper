# Windows Job Scraper

Private authoritative development workspace for the Windows Job Scraper project.

## Current authority

- **Canonical architecture/specification:** v0.3.1.3 (`docs/spec/v0.3.1.3/`)
- **Current implementation state:** Slice 0 is promoted; Slice 1 implementation, automated acceptance, and packaged native Windows acceptance are complete; Slice 2 work packages S2.0–S2.9 are implemented on the active lineage and the automated/corrective gates are green, but packaged/native Windows Slice-2 acceptance remains pending, so Slice 2 is **not yet promoted as a whole**
- **Final Slice 0/1 promotion record:** `docs/reviews/slice-0-1-native-promotion-closure-2026-09-08.md`
- **Slice 2 execution plan:** `docs/plans/slice-2-worker-implementation-plan-v0313.md`
- **Latest committed Slice 2 audit record:** `docs/reviews/slice-2-s2.8-s2.9-audit-corrective-review-2026-09-10.md` (historical through F1–F4; later F5–F7 correctives are integrated on the active lineage)
- **Implementation authority:** v0.3.1.3 plus the accepted Slice 0/1 plans and promotion record, the approved Slice 2 plan, and later explicit Slice-2 corrective records/commits on the active implementation lineage
- **Architecture rule:** do not redesign the architecture unless a genuine contradiction or implementation blocker is proven

Where an older Slice 0 or Slice 1 status report says packaged native Windows promotion is pending, the later final promotion closure record controls. Where an older Slice-2 review says a later S2.x package is blocked or has not started, that statement is historical; the later reconciled implementation/corrective lineage controls execution status. Historical review records remain immutable and are not rewritten to reflect later progress.

## Development workflow

Implementation proceeds in small review-gated packages. Local worker models such as GLM 5.3 or Muse Spark may implement and test one bounded approved package or corrective at a time, but may not resolve specification ambiguity by changing architecture. They either implement the explicit contract or stop and report the ambiguity for architectural review.

Slice 0 and Slice 1 are accepted on the implementation lineage. Slice 2 packages S2.0–S2.9 are implemented and have passed the current automated/corrective gate on the reconciled lineage. The remaining Slice-2 promotion work is packaged/native Windows acceptance plus the controlling final promotion record. Do not treat ordinary CI or this implementation state as authorization to begin Slice 3.

## Implementation status

### Slice 0 — Foundation / Safety Shell

**PROMOTED — IMPLEMENTATION, AUTOMATED GATE, PACKAGED WINDOWS VERIFICATION, AND NATIVE ACCEPTANCE COMPLETE.**

- All worker packages S0.0–S0.13 are implemented and committed.
- The automated gate (`scripts/verify_slice0.py`) passed.
- A real PyInstaller `--onedir` candidate was built and verified on Windows.
- Native packaged acceptance W0-01…W0-18 passed with no `FAIL` or `NOT_RUN`.
- Exact accepted package build: `onewise-0883c62196a1a4bf`.
- Evidence is committed under `artifacts/slice0/onewise-0883c62196a1a4bf/`.
- See `docs/reviews/slice-0-1-native-promotion-closure-2026-09-08.md` for the controlling promotion record.

### Slice 1 — Minimal final-architecture product MVP

**ACCEPTED — IMPLEMENTATION, AUTOMATED ACCEPTANCE, AND PACKAGED NATIVE WINDOWS ACCEPTANCE COMPLETE.**

- Slice 1 work packages S1.0–S1.12 are implemented and committed on the accepted lineage.
- `scripts/verify_slice1.py` passed its contract suite, Slice-1 E2E acceptance, full regression, dependency consistency, and Doctor gates.
- Native packaged acceptance W1-01…W1-07 passed with no `FAIL` or `NOT_RUN` against the accepted package candidate.
- The final packaged/native run also passed the complete W0 matrix, so the shared Windows foundation remained valid under the Slice-1 candidate.
- GitHub Actions is green on Python 3.12 for Ubuntu and Windows at the committed evidence state.
- Slice 2 was unblocked by native promotion and proceeded through the approved v0.3.1.3 work-package plan.

### Slice 2 — Structured acquisition breadth

**IMPLEMENTED THROUGH S2.9 — AUTOMATED/CORRECTIVE GATE GREEN; PACKAGED/NATIVE WINDOWS PROMOTION PENDING.**

- Approved work packages S2.0–S2.9 from `docs/plans/slice-2-worker-implementation-plan-v0313.md` are implemented on the reconciled active lineage.
- Provider breadth includes the Greenhouse, Lever, and Ashby structured adapters plus ATS fingerprinting/routing, richer evidence/origin handling, companies/multi-location/provenance, deterministic content cleaning/search, and the cross-provider Slice-2 acceptance gate.
- The S2.8/S2.9 audit record documents F1–F4. Subsequent corrective work closed the remaining F5–F7 findings: post-fence terminal-state atomicity, durable discovery-identity binding, and atomic discovery run/request creation.
- Latest behavior-bearing corrective head before this docs-only governance cleanup: `62915d637cd116bc9157dddc53f5039ebed9d752`.
- GitHub Actions run `34458023114` at that head passed on Ubuntu and Windows. Ubuntu: 1330 passed / 5 skipped; Windows: 1335 passed, dependency consistency clean, pywin32 primitives importable, Chromium smoke PASS.
- Slice 2 is **not promoted as a whole** until the required packaged/native Windows acceptance and final promotion closure are completed.
- Do not infer Slice-3 authorization from the automated Slice-2 state.

## Accepted package and evidence

The identifiers below are the controlling accepted Slice 0/1 packaged/native candidate. They do **not** constitute Slice-2 packaged/native acceptance.

- **Packaged source commit:** `2bb4f8319a2dfd312db21a19010cb0720359063e`
- **Acceptance-harness commit:** `92995d990c224715a3dea18cbf5e7d59f0126053`
- **Native evidence commit:** `7def768f75214b692d98efaa0f559e3a22f4f7af`
- **Build ID:** `onewise-0883c62196a1a4bf`
- **Build SHA-256:** `0883c62196a1a4bf39b3092a1a5068d0cf50a143159823daf3a312e7f54f4dff`
- **Package verification:** PASS, 1,015 files
- **Native matrix:** W0 18/18 PASS; W1 7/7 PASS

## Running locally (development)

```bash
python -m venv .venv && . .venv/bin/activate      # Python 3.12+ required
pip install -r requirements/production.lock.txt --no-deps
pip install -r requirements/dev.lock.txt --no-deps
pip install --no-deps -e .
python -m pytest tests -q                          # full suite
python -m jobscraper --doctor --data-root <root>   # diagnostics
python scripts/verify_slice0.py                    # Slice 0 automated gate
python scripts/verify_slice1.py                    # Slice 1 automated gate
python scripts/verify_slice2.py                    # Slice 2 automated gate
```

On Windows, the committed lock files include the Windows-only `pywin32` dependency used for DPAPI, named-mutex single-instance enforcement, ACL hardening, and related native primitives.

## CI

Active GitHub Actions CI is defined in `.github/workflows/ci.yml`.

The matrix runs on Python 3.12 for both `ubuntu-latest` and `windows-latest`, executes the full automated test suite, checks installed dependency consistency, proves required pywin32 primitives on Windows, installs the pinned Playwright Chromium runtime, and runs the inert browser smoke.

CI is not a substitute for packaged native acceptance. For the accepted Slice 0/1 candidate, that separate packaged native gate passed and its evidence is committed. For Slice 2, automated CI is green but packaged/native Windows acceptance remains pending. Any later behavior-bearing or packaging change must be assessed for the appropriate automated/package/native revalidation before a new promotion claim.

## Key documents

- `docs/spec/v0.3.1.3/00_architecture_overview_and_authority.md` — canonical authority entry point
- `docs/spec/v0.3.1.3/07_implementation_roadmap.md` — normative slice order
- `docs/plans/slice-0-worker-implementation-plan-v0313.md` — Slice 0 worker execution plan
- `docs/plans/slice-0-windows-acceptance-strategy-v0313.md` — Slice 0 Windows acceptance strategy
- `docs/plans/slice-0-artifact-manifest-v0313.json` — Slice 0 planning artifact manifest
- `docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md` — historical Slice 0 corrective review/pre-promotion status record
- `docs/plans/slice-1-worker-implementation-plan-v0313.md` — Slice 1 worker execution plan
- `docs/reviews/slice-1-status-report-2026-09-08.md` — historical Slice 1 implementation/corrective/pre-promotion status record
- `docs/reviews/slice-0-1-native-promotion-closure-2026-09-08.md` — controlling final Slice 0/1 packaged/native promotion record
- `docs/plans/slice-2-worker-implementation-plan-v0313.md` — approved Slice 2 execution plan
- `docs/reviews/slice-2-s2.4-s2.5-corrective-review-2026-09-09.md` — accepted S2.4/S2.5 corrective record
- `docs/reviews/slice-2-s2.6-corrective-review-2026-09-09.md` — S2.6 corrective record
- `docs/reviews/slice-2-s2.7-corrective-review-2026-09-09.md` — S2.7 corrective record
- `docs/reviews/slice-2-s2.8-s2.9-audit-corrective-review-2026-09-10.md` — S2.8/S2.9 automated audit record through F1–F4; later F5–F7 commits supersede its remaining-finding status
- `AGENTS.md` — current repository worker boundary and authority rules
