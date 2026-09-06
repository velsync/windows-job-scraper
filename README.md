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

## Key documents

- `docs/spec/v0.3.1.3/00_architecture_overview_and_authority.md` — canonical authority entry point
- `docs/spec/v0.3.1.3/07_implementation_roadmap.md` — normative slice order
- `docs/plans/slice-0-worker-implementation-plan-v0313.md` — worker-oriented Slice 0 execution plan
- `docs/plans/slice-0-windows-acceptance-strategy-v0313.md` — Slice 0 Windows acceptance strategy
- `docs/plans/slice-0-artifact-manifest-v0313.json` — Slice 0 planning artifact manifest
- `AGENTS.md` — repository worker boundary

Application implementation has intentionally not started yet.
