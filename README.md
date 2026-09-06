# Windows Job Scraper

Private authoritative development workspace for the Windows Job Scraper project.

## Current authority

- **Canonical architecture/specification:** v0.3.1.3 (`docs/spec/v0.3.1.3/`)
- **Current implementation slice:** Slice 0 — Foundation / Safety Shell
- **Implementation authority:** the v0.3.1.3 modular specification set plus the accepted Slice 0 plan and acceptance strategy in this repository
- **Architecture rule:** do not redesign the architecture unless a genuine contradiction or implementation blocker is proven

## Development workflow

Implementation proceeds in small, review-gated work packages. Local worker models may implement and test bounded packages, but they must not resolve specification ambiguity by changing architecture. They either implement the explicit contract or stop and report the ambiguity for architectural review.

Every Slice 0 package must be independently testable and reviewable. Slice 1 must not begin until Slice 0 passes its automated and native Windows acceptance gates.

## Key documents

- `docs/spec/v0.3.1.3/README.md` — canonical modular specification entry point
- `docs/plans/slice-0-worker-implementation-plan-v0313.md` — worker-oriented Slice 0 execution plan
- `docs/plans/slice-0-windows-acceptance-strategy-v0313.md` — Slice 0 Windows acceptance strategy
- `docs/plans/slice-0-artifact-manifest-v0313.json` — Slice 0 planning artifact manifest

Application implementation has intentionally not started in this repository yet.
