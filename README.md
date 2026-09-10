# Windows Job Scraper

Private authoritative development workspace for the Windows Job Scraper project.

## Current authority

- **Canonical architecture/specification:** v0.3.1.3 (`docs/spec/v0.3.1.3/`)
- **Current implementation state:** Slice 0 is promoted; Slice 1 implementation, automated acceptance, and packaged native Windows acceptance are complete; Slice 2 is **PROMOTED** after implementation through S2.9, corrective closure, packaged Windows verification, 31/31 native acceptance, and full Ubuntu + Windows CI on the exact frozen candidate
- **Final Slice 0/1 promotion record:** `docs/reviews/slice-0-1-native-promotion-closure-2026-09-08.md`
- **Slice 2 execution plan:** `docs/plans/slice-2-worker-implementation-plan-v0313.md`
- **Slice 2 Windows acceptance strategy:** `docs/plans/slice-2-windows-acceptance-strategy-v0313.md`
- **Final Slice 2 promotion record:** `docs/reviews/slice-2-native-promotion-closure-2026-09-10.md`
- **Latest historical Slice 2 audit record:** `docs/reviews/slice-2-s2.8-s2.9-audit-corrective-review-2026-09-10.md` (through F1–F4; later F5–F7 correctives are integrated on the promoted lineage)
- **Implementation authority:** v0.3.1.3 plus the accepted Slice 0/1 plans and promotion record, the approved Slice 2 plan/acceptance strategy, later explicit Slice-2 corrective records/commits, and the final Slice-2 promotion closure
- **Architecture rule:** do not redesign the architecture unless a genuine contradiction or implementation blocker is proven

Where an older Slice 0 or Slice 1 status report says packaged native Windows promotion is pending, the later Slice 0/1 promotion closure controls. Where an older Slice-2 review/status record says a later S2.x package is blocked, not started, or packaged/native promotion is pending, that statement is historical; the later reconciled implementation/corrective lineage and final Slice-2 promotion closure control current status. Historical review records remain immutable and are not rewritten to reflect later progress.

## Development workflow

Implementation proceeds in small review-gated packages. Local worker models such as GLM 5.3 or Muse Spark may implement and test one bounded approved package or corrective at a time, but may not resolve specification ambiguity by changing architecture. They either implement the explicit contract or stop and report the ambiguity for architectural review.

Slice 0 is promoted, Slice 1 is accepted with native Windows acceptance complete, and Slice 2 is promoted on the authoritative implementation lineage. **Slice 3 has not been started or authorized.** Do not begin Slice 3 without a new explicit approval against the v0.3.1.3 roadmap and applicable implementation plan.

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
- Slice 2 subsequently proceeded through its approved v0.3.1.3 work-package plan and final promotion gate.

### Slice 2 — Structured acquisition breadth

**PROMOTED — IMPLEMENTATION, AUTOMATED/CORRECTIVE GATES, PACKAGED WINDOWS VERIFICATION, AND NATIVE ACCEPTANCE COMPLETE.**

- Approved work packages S2.0–S2.9 from `docs/plans/slice-2-worker-implementation-plan-v0313.md` are implemented on the promoted lineage.
- Provider breadth includes the Greenhouse, Lever, and Ashby structured adapters plus ATS fingerprinting/routing, richer evidence/origin handling, companies/multi-location/provenance, deterministic content cleaning/search, and the cross-provider Slice-2 acceptance gate.
- The S2.8/S2.9 audit record documents F1–F4. Subsequent corrective work closed F5–F7: post-fence terminal-state atomicity, durable discovery-identity binding, and atomic discovery run/request creation.
- Latest behavior-bearing corrective head before native-prep/docs-only work: `62915d637cd116bc9157dddc53f5039ebed9d752`.
- Exact frozen/promoted candidate: `897adae8eb3f8d6f951590f6f43083f50da63339`.
- Exact package: build ID `onewise-3cd6f8dac1b3e757`, SHA-256 `3cd6f8dac1b3e75761fa6d118940bb5cb09f82e3a9b9455dabfb400369c1e666`, 1,015 files.
- Packaged Windows verification: **PASS**.
- Native packaged acceptance: **W0 18/18 + W1 7/7 + W2 6/6 = 31/31 PASS; zero FAIL; zero NOT_RUN**.
- GitHub Actions run `34474624862` at the exact promoted candidate passed on Ubuntu and Windows. Ubuntu: 1331 passed / 5 skipped; Windows: 1336 passed, dependency consistency clean, pywin32 primitives importable, Chromium installation PASS, inert browser smoke PASS.
- See `docs/reviews/slice-2-native-promotion-closure-2026-09-10.md` for the controlling final promotion record.
- **Slice 3 is not authorized by Slice-2 promotion.**

## Accepted package and evidence

### Slice 0/1 accepted package

- **Packaged source commit:** `2bb4f8319a2dfd312db21a19010cb0720359063e`
- **Acceptance-harness commit:** `92995d990c224715a3dea18cbf5e7d59f0126053`
- **Native evidence commit:** `7def768f75214b692d98efaa0f559e3a22f4f7af`
- **Build ID:** `onewise-0883c62196a1a4bf`
- **Build SHA-256:** `0883c62196a1a4bf39b3092a1a5068d0cf50a143159823daf3a312e7f54f4dff`
- **Package verification:** PASS, 1,015 files
- **Native matrix:** W0 18/18 PASS; W1 7/7 PASS

### Slice 2 promoted package

- **Frozen candidate commit:** `897adae8eb3f8d6f951590f6f43083f50da63339`
- **Build ID:** `onewise-3cd6f8dac1b3e757`
- **Build SHA-256:** `3cd6f8dac1b3e75761fa6d118940bb5cb09f82e3a9b9455dabfb400369c1e666`
- **Package verification:** PASS, 1,015 files
- **Native matrix:** W0 18/18 PASS; W1 7/7 PASS; W2 6/6 PASS; 31/31 total
- **Local preserved package/evidence root:** `E:\Local AI\wjs-s2-package-897adae8`
- **Controlling record:** `docs/reviews/slice-2-native-promotion-closure-2026-09-10.md`

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

CI is not a substitute for packaged native acceptance. Slice 0/1 passed their separate packaged native gate, and Slice 2 has now also passed its separate packaged/native Windows promotion gate against frozen candidate `897adae8eb3f8d6f951590f6f43083f50da63339`. Any later behavior-bearing or packaging change must be assessed for the appropriate automated/package/native revalidation before a new promotion claim.

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
- `docs/plans/slice-2-windows-acceptance-strategy-v0313.md` — approved Slice 2 packaged/native Windows acceptance strategy
- `docs/reviews/slice-2-s2.4-s2.5-corrective-review-2026-09-09.md` — accepted S2.4/S2.5 corrective record
- `docs/reviews/slice-2-s2.6-corrective-review-2026-09-09.md` — S2.6 corrective record
- `docs/reviews/slice-2-s2.7-corrective-review-2026-09-09.md` — S2.7 corrective record
- `docs/reviews/slice-2-s2.8-s2.9-audit-corrective-review-2026-09-10.md` — historical S2.8/S2.9 automated audit through F1–F4; later F5–F7 commits supersede its remaining-finding status
- `docs/reviews/slice-2-native-promotion-closure-2026-09-10.md` — controlling final Slice 2 packaged/native promotion record
- `AGENTS.md` — current repository worker boundary and authority rules
