# Agent Instructions — Windows Job Scraper

This repository is the authoritative implementation workspace for Windows Job Scraper.

## Authority

1. Canonical architecture: `docs/spec/v0.3.1.3/`.
2. Current execution plan: `docs/plans/slice-0-worker-implementation-plan-v0313.md`.
3. Current Windows acceptance: `docs/plans/slice-0-windows-acceptance-strategy-v0313.md`.

Do not redesign the architecture unless a genuine contradiction or implementation blocker is proven and explicitly adjudicated by the architecture reviewer.

## Worker boundary

- Implement one approved S0.x package at a time.
- Read the owning normative module(s) before editing.
- Use tests first for behavior-bearing code.
- Do not begin the next package without review.
- Do not make unrelated cleanup/refactors.
- Do not add future-slice product/crawler/adapters as placeholders.
- If a normative ambiguity affects implementation, STOP and report it rather than guessing.
- Never weaken a security/recovery invariant to make a test pass.
- Never claim PASS without exact command/test evidence.

## Branch/review discipline

Use a dedicated branch/worktree for each approved package or corrective. Keep commits small enough to review. The architecture reviewer compares the actual diff/tests to the worker report before promotion.

## Non-goals

No automatic applications, CAPTCHA solving, access-control bypass, anti-bot evasion, public proxy harvesting, arbitrary remote code/plugin execution, LAN-hosted multi-user service, or cloud dependency is part of the architecture.
