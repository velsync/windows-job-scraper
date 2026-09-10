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
6. Slice 2 execution/corrective authority on the active implementation lineage:
   - `docs/plans/slice-2-worker-implementation-plan-v0313.md`
   - `docs/reviews/slice-2-s2.2-corrective-review-2026-09-08.md`
   - `docs/reviews/slice-2-s2.3-corrective-review-2026-09-08.md`
   - `docs/reviews/slice-2-s2.4-s2.5-corrective-review-2026-09-09.md`
   - `docs/reviews/slice-2-s2.6-corrective-review-2026-09-09.md`
   - `docs/reviews/slice-2-s2.7-corrective-review-2026-09-09.md`
   - `docs/reviews/slice-2-s2.8-s2.9-audit-corrective-review-2026-09-10.md` (historical automated audit through F1–F4)
   - later F5–F7 corrective commits on the reconciled Slice-2 lineage, culminating in behavior-bearing head `62915d637cd116bc9157dddc53f5039ebed9d752`.

Later authority controls where an earlier plan/status note conflicts with an explicit corrective or promotion record. In particular, old Slice-2 records that say a later S2.x package is blocked or has not started are historical snapshots, not current execution authority. Do not rewrite sealed historical review records to erase those earlier states; update living authority documents instead.

Do not redesign the architecture unless a genuine contradiction or implementation blocker is proven and explicitly adjudicated by the architecture reviewer.

## Current execution state

- Slice 0 is **PROMOTED**: implementation, automated gate, packaged Windows verification, and W0-01…W0-18 native acceptance are complete.
- Slice 1 is **ACCEPTED**: implementation, automated acceptance, and W1-01…W1-07 packaged native Windows acceptance are complete.
- Accepted Slice 0/1 package build: `onewise-0883c62196a1a4bf`; evidence is committed under `artifacts/slice0/onewise-0883c62196a1a4bf/`.
- On the active Slice-2 implementation lineage, **S2.0–S2.9 are implemented** under the approved v0.3.1.3 Slice-2 plan.
- The S2.8/S2.9 audit record documents F1–F4. Subsequent corrective work closed F5–F7: post-fence discovery terminal-state atomicity, durable caller/durable discovery-identity binding, and atomic discovery run/request creation.
- Latest behavior-bearing Slice-2 corrective head before this docs-only governance cleanup: `62915d637cd116bc9157dddc53f5039ebed9d752`.
- GitHub Actions run `34458023114` at that behavior-bearing head passed on Ubuntu and Windows. Ubuntu: 1330 passed / 5 skipped. Windows: 1335 passed, dependency consistency clean, pywin32 primitives importable, Chromium smoke PASS.
- **Slice 2 is not yet promoted as a whole. Packaged/native Windows Slice-2 acceptance and a controlling final promotion closure remain pending.** Ordinary CI or automated acceptance-readiness does not constitute Slice-2 promotion.
- The next Slice-2 action is the required packaged/native Windows acceptance/promotion closure. **Do not begin Slice 3 unless it is explicitly approved after the applicable Slice-2 promotion decision.**
- Do not import, cherry-pick, or count divergent experimental-branch future-slice code as accepted implementation authority unless it is explicitly reviewed and reconciled.

## Worker boundary

- Implement or correct one explicitly approved bounded work package at a time.
- Read the owning v0.3.1.3 normative module(s) and the applicable slice plan/status/corrective record before editing.
- Use tests first for behavior-bearing code.
- Do not begin a later slice/package without explicit review/authorization.
- Do not make unrelated cleanup/refactors.
- Do not add future-slice product/crawler/adapters as placeholders.
- If a normative ambiguity affects implementation, STOP and report it rather than guessing.
- Never weaken a security/recovery invariant to make a test pass.
- Never claim PASS without exact command/test evidence.
- Do not infer a new promotion from ordinary CI alone. Any behavior-bearing or packaging change must receive the automated/package/native revalidation required by its owning authority before a new promotion claim.
- Treat docs-only governance commits as non-behavior-bearing; they may update living status/authority pointers but must not silently alter historical acceptance evidence.

## Accepted Slice 0/1 identifiers

- Packaged source commit: `2bb4f8319a2dfd312db21a19010cb0720359063e`
- Acceptance-harness commit: `92995d990c224715a3dea18cbf5e7d59f0126053`
- Native evidence commit: `7def768f75214b692d98efaa0f559e3a22f4f7af`
- Build ID: `onewise-0883c62196a1a4bf`
- Build SHA-256: `0883c62196a1a4bf39b3092a1a5068d0cf50a143159823daf3a312e7f54f4dff`
- Native acceptance: W0 18/18 PASS; W1 7/7 PASS; zero FAIL; zero NOT_RUN

These identifiers apply to the accepted Slice 0/1 packaged/native candidate only. They do not prove Slice-2 packaged/native acceptance.

## Branch/review discipline

Use a dedicated branch/worktree for each approved package or corrective. Keep commits small enough to review. The architecture reviewer compares the actual diff/tests to the worker report before promotion.

Work only from the accepted Slice 0/1 implementation lineage plus explicitly reviewed/reconciled Slice-2 implementation commits. Do not treat divergent experimental branches as implementation authority unless they are explicitly reconciled and promoted.

## Non-goals

No automatic applications, CAPTCHA solving, access-control bypass, anti-bot evasion, public proxy harvesting, arbitrary remote code/plugin execution, LAN-hosted multi-user service, or cloud dependency is part of the architecture.
