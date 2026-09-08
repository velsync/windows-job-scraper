# Windows Job Scraper

Private authoritative development workspace for the Windows Job Scraper project.

## Current authority

- **Canonical architecture/specification:** v0.3.1.3 (`docs/spec/v0.3.1.3/`)
- **Current implementation state:** Slice 0 is promoted; Slice 1 implementation, automated acceptance, and packaged native Windows acceptance are complete; Slice 2 has not started
- **Final Slice 0/1 promotion record:** `docs/reviews/slice-0-1-native-promotion-closure-2026-09-08.md`
- **Implementation authority:** v0.3.1.3 plus the accepted Slice 0 and Slice 1 plans/review records and the final promotion closure record in this repository
- **Architecture rule:** do not redesign the architecture unless a genuine contradiction or implementation blocker is proven

Where an older Slice 0 or Slice 1 status report says packaged native Windows promotion is pending, the later final promotion closure record controls.

## Development workflow

Implementation proceeds in small review-gated packages. Local worker models such as GLM 5.3 or Muse Spark may implement and test one bounded approved package or corrective at a time, but may not resolve specification ambiguity by changing architecture. They either implement the explicit contract or stop and report the ambiguity for architectural review.

Slice 0 and Slice 1 are accepted on the implementation lineage. Slice 2 must still begin as an explicitly approved bounded slice/work-package sequence against v0.3.1.3; do not import or count divergent experimental-branch work as Slice 2 implementation authority.

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
- Slice 2 is now unblocked by native promotion, but must start only through an explicitly approved v0.3.1.3 work package/plan.

## Accepted package and evidence

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
```

On Windows, the committed lock files include the Windows-only `pywin32` dependency used for DPAPI, named-mutex single-instance enforcement, ACL hardening, and related native primitives.

## CI

Active GitHub Actions CI is defined in `.github/workflows/ci.yml`.

The matrix runs on Python 3.12 for both `ubuntu-latest` and `windows-latest`, executes the full automated test suite, checks installed dependency consistency, proves required pywin32 primitives on Windows, installs the pinned Playwright Chromium runtime, and runs the inert browser smoke.

CI is not a substitute for packaged native acceptance. For the accepted Slice 0/1 candidate, that separate packaged native gate has now also passed and its evidence is committed. Any later behavior-bearing or packaging change must be assessed for the appropriate automated/package/native revalidation before a new promotion claim.

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
- `AGENTS.md` — current repository worker boundary and authority rules
