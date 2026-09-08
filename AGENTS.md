# Agent Instructions — Windows Job Scraper

This repository is the authoritative implementation workspace for Windows Job Scraper.

## Authority

1. Canonical architecture: `docs/spec/v0.3.1.3/`.
2. Normative slice order: `docs/spec/v0.3.1.3/07_implementation_roadmap.md`.
3. Slice 0 execution/acceptance authority:
   - `docs/plans/slice-0-worker-implementation-plan-v0313.md`
   - `docs/plans/slice-0-windows-acceptance-strategy-v0313.md`
   - `docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md` (historical corrective/pre-promotion record)
4. Slice 1 execution/status authority:
   - `docs/plans/slice-1-worker-implementation-plan-v0313.md`
   - `docs/reviews/slice-1-status-report-2026-09-08.md` (historical implementation/corrective/pre-promotion record)
5. Final Slice 0/1 packaged/native promotion authority:
   - `docs/reviews/slice-0-1-native-promotion-closure-2026-09-08.md`

Later authority controls where an earlier status record says native promotion is still pending.

Do not redesign the architecture unless a genuine contradiction or implementation blocker is proven and explicitly adjudicated by the architecture reviewer.

## Current execution state

- Slice 0 is **PROMOTED**: implementation, automated gate, packaged Windows verification, and W0-01…W0-18 native acceptance are complete.
- Slice 1 is **ACCEPTED**: implementation, automated acceptance, and W1-01…W1-07 packaged native Windows acceptance are complete.
- Accepted package build: `onewise-0883c62196a1a4bf`; evidence is committed under `artifacts/slice0/onewise-0883c62196a1a4bf/`.
- Slice 2 has not started on the accepted lineage. It is no longer blocked by Slice 0/1 native promotion, but work must begin only when explicitly authorized as a bounded v0.3.1.3 package/plan.
- Do not import, cherry-pick, or count divergent experimental-branch future-slice code as accepted Slice 2 work unless it is explicitly reviewed and reconciled.

## Worker boundary

- Implement or correct one explicitly approved bounded work package at a time.
- Read the owning v0.3.1.3 normative module(s) and the applicable slice plan/status record before editing.
- Use tests first for behavior-bearing code.
- Do not begin the next package without review.
- Do not make unrelated cleanup/refactors.
- Do not add future-slice product/crawler/adapters as placeholders.
- If a normative ambiguity affects implementation, STOP and report it rather than guessing.
- Never weaken a security/recovery invariant to make a test pass.
- Never claim PASS without exact command/test evidence.
- Do not infer a new promotion from ordinary CI alone. Any later behavior-bearing or packaging change must receive the automated/package/native revalidation required by its owning authority before a new promotion claim.

## Accepted Slice 0/1 identifiers

- Packaged source commit: `2bb4f8319a2dfd312db21a19010cb0720359063e`
- Acceptance-harness commit: `92995d990c224715a3dea18cbf5e7d59f0126053`
- Native evidence commit: `7def768f75214b692d98efaa0f559e3a22f4f7af`
- Build ID: `onewise-0883c62196a1a4bf`
- Build SHA-256: `0883c62196a1a4bf39b3092a1a5068d0cf50a143159823daf3a312e7f54f4dff`
- Native acceptance: W0 18/18 PASS; W1 7/7 PASS; zero FAIL; zero NOT_RUN

## Branch/review discipline

Use a dedicated branch/worktree for each approved package or corrective. Keep commits small enough to review. The architecture reviewer compares the actual diff/tests to the worker report before promotion.

Work only from the accepted Slice 0/1 implementation lineage. Do not treat divergent experimental branches as implementation authority unless they are explicitly reconciled and promoted.

## Non-goals

No automatic applications, CAPTCHA solving, access-control bypass, anti-bot evasion, public proxy harvesting, arbitrary remote code/plugin execution, LAN-hosted multi-user service, or cloud dependency is part of the architecture.
