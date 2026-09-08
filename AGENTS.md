# Agent Instructions — Windows Job Scraper

This repository is the authoritative implementation workspace for Windows Job Scraper.

## Authority

1. Canonical architecture: `docs/spec/v0.3.1.3/`.
2. Normative slice order: `docs/spec/v0.3.1.3/07_implementation_roadmap.md`.
3. Slice 0 execution/acceptance authority:
   - `docs/plans/slice-0-worker-implementation-plan-v0313.md`
   - `docs/plans/slice-0-windows-acceptance-strategy-v0313.md`
   - `docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md`
4. Slice 1 execution/status authority:
   - `docs/plans/slice-1-worker-implementation-plan-v0313.md`
   - `docs/reviews/slice-1-status-report-2026-09-08.md`

Do not redesign the architecture unless a genuine contradiction or implementation blocker is proven and explicitly adjudicated by the architecture reviewer.

## Current execution state

- Slice 0 implementation and automated verification are complete; packaged native Windows promotion is still pending.
- Slice 1 implementation and automated acceptance are complete; packaged native Windows promotion is still pending.
- The current allowed work is bounded corrective, packaging, native Windows acceptance, evidence capture, and authority/governance reconciliation for the accepted Slice 0/1 lineage.
- **Do not begin Slice 2 implementation** until the remaining native promotion gate is closed and the architecture reviewer explicitly authorizes Slice 2 work.

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
- Do not claim native promotion from development-mode or ordinary CI tests alone; packaged Windows W0/W1 acceptance evidence is required.

## Branch/review discipline

Use a dedicated branch/worktree for each approved package or corrective. Keep commits small enough to review. The architecture reviewer compares the actual diff/tests to the worker report before promotion.

Work only from the accepted Slice 0/1 implementation lineage. Do not treat divergent experimental branches as implementation authority unless they are explicitly reconciled and promoted.

## Non-goals

No automatic applications, CAPTCHA solving, access-control bypass, anti-bot evasion, public proxy harvesting, arbitrary remote code/plugin execution, LAN-hosted multi-user service, or cloud dependency is part of the architecture.
