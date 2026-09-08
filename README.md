# Windows Job Scraper

Private authoritative development workspace for the Windows Job Scraper project.

## Current authority

- **Canonical architecture/specification:** v0.3.1.3 (`docs/spec/v0.3.1.3/`)
- **Current implementation state:** Slice 0 and Slice 1 implementation are complete; both remain pending packaged native Windows promotion
- **Implementation authority:** v0.3.1.3 plus the accepted Slice 0 and Slice 1 plans/status records in this repository
- **Architecture rule:** do not redesign the architecture unless a genuine contradiction or implementation blocker is proven

## Development workflow

Implementation proceeds in small review-gated packages. Local worker models such as GLM 5.3 or Muse Spark may implement and test one bounded approved package or corrective at a time, but may not resolve specification ambiguity by changing architecture. They either implement the explicit contract or stop and report the ambiguity for architectural review.

Slice 0 and Slice 1 implementation and automated acceptance are complete on the accepted implementation lineage. Do not begin Slice 2 implementation until the remaining packaged native Windows acceptance/promotion gates are closed and the architecture reviewer explicitly authorizes the next slice.

## Implementation status

### Slice 0 — Foundation / Safety Shell

**IMPLEMENTATION COMPLETE — AUTOMATED GATE COMPLETE — NATIVE PROMOTION PENDING.**

- All worker packages S0.0–S0.13 are implemented and committed.
- The automated gate (`scripts/verify_slice0.py`) passes.
- The native Windows acceptance harness (`scripts/native_acceptance.py`, W0-01…W0-18) is implemented and self-validated in development mode.
- The packaged Windows `--onedir` acceptance run and committed native evidence remain outstanding.
- See `docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md` for the detailed status and W0 matrix.

### Slice 1 — Minimal final-architecture product MVP

**IMPLEMENTATION COMPLETE — AUTOMATED ACCEPTANCE COMPLETE — NATIVE PROMOTION PENDING.**

- Slice 1 work packages S1.0–S1.12 are implemented and committed on the accepted lineage.
- `scripts/verify_slice1.py` passes its contract suite, Slice-1 E2E acceptance, full regression, dependency consistency, and Doctor gates.
- The W1 native matrix is implemented in `scripts/native_acceptance.py` and passes in development-target self-validation.
- GitHub Actions is green on Python 3.12 for both Ubuntu and Windows, including pywin32 proof and the inert Playwright browser smoke.
- The remaining formal gate is the packaged Windows `--target exe` native acceptance run with W0/W1 evidence committed to the repository.
- See `docs/reviews/slice-1-status-report-2026-09-08.md` for the package ledger, corrective review, automated evidence, and exact NOT_RUN items.

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

A green CI run is strong automated Windows evidence but is **not** the formal packaged native promotion gate. The repository must still build and validate the PyInstaller `--onedir` candidate through the native W0/W1 acceptance process and commit the resulting evidence before Slice 0 or Slice 1 is declared native-promoted.

## Key documents

- `docs/spec/v0.3.1.3/00_architecture_overview_and_authority.md` — canonical authority entry point
- `docs/spec/v0.3.1.3/07_implementation_roadmap.md` — normative slice order
- `docs/plans/slice-0-worker-implementation-plan-v0313.md` — Slice 0 worker execution plan
- `docs/plans/slice-0-windows-acceptance-strategy-v0313.md` — Slice 0 Windows acceptance strategy
- `docs/plans/slice-0-artifact-manifest-v0313.json` — Slice 0 planning artifact manifest
- `docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md` — Slice 0 corrective review and status
- `docs/plans/slice-1-worker-implementation-plan-v0313.md` — Slice 1 worker execution plan
- `docs/reviews/slice-1-status-report-2026-09-08.md` — Slice 1 implementation, corrective-review, automated-acceptance, and native-promotion status
- `AGENTS.md` — current repository worker boundary and authority rules
