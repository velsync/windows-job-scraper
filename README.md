# Windows Job Scraper

Private authoritative development workspace for the Windows Job Scraper project.

## Current authority

- **Canonical architecture/specification:** v0.3.1.3 (`docs/spec/v0.3.1.3/`)
- **Current implementation slice:** Slice 0 — Foundation / Safety Shell
- **Implementation authority:** v0.3.1.3 plus the accepted Slice 0 plan and Windows acceptance strategy in this repository
- **Architecture rule:** do not redesign the architecture unless a genuine contradiction or implementation blocker is proven

## Development workflow

Implementation proceeds in small review-gated packages S0.0 through S0.13. Local worker models such as GLM 5.3 or Muse Spark may implement and test one bounded package, but may not resolve specification ambiguity by changing architecture. They either implement the explicit contract or stop and report the ambiguity for architectural review.

Every work package is independently reviewed. Slice 1 must not be decomposed or implemented until Slice 0 passes both its automated and native Windows acceptance gates.

## Implementation status

**Slice 0 (Foundation / Safety Shell): IMPLEMENTATION COMPLETE — NATIVE PROMOTION PENDING.**

- All worker packages S0.0–S0.13 are implemented and committed; the automated
  gate (`scripts/verify_slice0.py`: unit + contract + integration tests,
  `pip check`, initialized-root Doctor) passes.
- The native Windows acceptance harness (`scripts/native_acceptance.py`,
  checks W0-01…W0-18) is written and self-validated in dev mode
  (17 PASS / 1 NOT_RUN for the missing browser runtime on the dev host).
- The native Windows run against the packaged `--onedir` build and CI
  activation remain outstanding; see
  `docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md` for the
  full status, W0 matrix and blockers. Slice 0 must not be marked PROMOTED
  until that native run passes.

## Running locally (development)

```bash
python -m venv .venv && . .venv/bin/activate      # Python 3.12+ required
pip install -r requirements/prod.lock.txt --no-deps
pip install -r requirements/dev.lock.txt --no-deps
pip install --no-deps -e .
python -m pytest tests -q                          # full suite
python -m jobscraper --doctor --data-root <root>   # diagnostics
python scripts/verify_slice0.py                    # Slice 0 automated gate
```

Windows additionally requires `pip install pywin32==312` (already pinned in
the lock files) for DPAPI, named-mutex single instance and ACL hardening.

## CI

CI is parked at `build/ci/ci.yml` (test matrix, pywin32 proof, pinned
Chromium install with inert browser smoke, and the packaged build +
`scripts/verify_packaged_build.py` verification). Activating it requires one
manual step by a credential with workflow permissions — see
`build/ci/README.md`.

## Key documents

- `docs/spec/v0.3.1.3/00_architecture_overview_and_authority.md` — canonical authority entry point
- `docs/spec/v0.3.1.3/07_implementation_roadmap.md` — normative slice order
- `docs/plans/slice-0-worker-implementation-plan-v0313.md` — worker-oriented Slice 0 execution plan
- `docs/plans/slice-0-windows-acceptance-strategy-v0313.md` — Slice 0 Windows acceptance strategy
- `docs/plans/slice-0-artifact-manifest-v0313.json` — Slice 0 planning artifact manifest
- `docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md` — Slice 0 corrective review + final status
- `AGENTS.md` — repository worker boundary

