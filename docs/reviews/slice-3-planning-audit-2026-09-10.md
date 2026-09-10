# Slice 3 Planning Audit — v0.3.1.3

Date: 2026-09-10  
Repository: `velsync/windows-job-scraper`  
Plan audited: `docs/plans/slice-3-worker-implementation-plan-v0313.md`  
Acceptance strategy audited: `docs/plans/slice-3-windows-acceptance-strategy-v0313.md`  
Planning base: `c9cd5e6b426fc40874f9421c05de9f58beca3c03`  
Status: **PASS WITH FOUR EXPLICIT NON-STRUCTURAL CLARIFICATIONS**

This is a planning/governance review only. No production code is authorized or changed. The four clarifications below do not change the 14-package decomposition or sequencing; they make already-normative v0.3.1.3 obligations explicit for worker handoff. Until folded into a later consolidated plan revision, workers must read this audit with the Slice-3 worker plan.

## 1. Audit method

The plan was checked against ROAD-04 line by line and then against the owning requirements in modules 00–06, especially RUN-01/02/04–10/11/13/14/14A/19/21, acquisition §§19–21 and ACQ-02/03/04/07/09, VER-01/02/04/13/14/15, SEC current-authorization override, and Windows §50/§67 process/SQLite acceptance rules.

The current promoted implementation scaffold was also checked so the plan does not create parallel ownership surfaces. Existing `runtime/requests.py`, `claims.py`, `fence.py`, `runs.py`, `recovery.py`, `pipeline/coverage.py`, `pipeline/obligations.py`, `acquisition/envelope.py`, `pagevalidity.py` and the v5/v6 durable schema are to be extended, not replaced.

## 2. Clarification C1 — page-validity ownership in S3.5

ROAD-04 explicitly includes page validity. The worker plan preserves the existing `acquisition/pagevalidity.py` gate globally, but S3.5 must make the crawler integration explicit:

- every crawler HTTP result is classified before adapter parsing;
- only `VALID_LIST`, `VALID_JOB`, and the specifically recognized `EMPTY` path may enter normal parse semantics;
- `LOGIN_REQUIRED`, `AUTH_EXPIRED`, `RATE_LIMITED`, `CHALLENGE_PAGE`, `JS_SHELL`, `NOT_FOUND`, `JOB_CLOSED`, `UNEXPECTED_REDIRECT`, `UNEXPECTED_CONTENT`, and `UNKNOWN` remain typed validity/failure/closure evidence and cannot fabricate normal observations or authoritative empty coverage;
- an ordinary security-valid redirect is transport metadata and must not be mislabeled `UNEXPECTED_REDIRECT` merely because a redirect occurred;
- invalid/challenge/auth content cannot create or restore absence authority.

**Package owner:** S3.5, with regression in S3.13.  
**Sequence change:** none.

## 3. Clarification C2 — per-egress capacity/rate key when configured

S3.3 lists global/class/per-source/per-host capacity. v0.3.1.3 also defines a per-egress control dimension. Slice 3 does **not** need to implement proxy setup or optional egress-profile product UX, but the coordinator/rate key must be capable of including an already-host-approved egress identity when one exists.

- no imported adapter/source config may manufacture an egress identity or permission;
- direct connection remains the default;
- absence of configured egress does not create a synthetic proxy dimension;
- this is accounting/policy plumbing only, not Slice-11 optional-integration work.

**Package owner:** S3.3/S3.4.  
**Sequence change:** none.

## 4. Clarification C3 — cursor-loop quarantine/result semantics

S3.5 already requires repeated cursor/URL/hash/job-set/cycle/no-progress detection. VER §60 also requires cursor-loop quarantine behavior to be proven. Make the terminal handling explicit:

- a proven pagination/cursor trap stops further work for that run-source-plan/binding revision under a typed `PAGINATION_LOOP`/equivalent failure;
- it cannot be converted to COMPLETE coverage or absence authority;
- it must not automatically globally quarantine the whole source unless separate current policy warrants that action;
- restart cannot resume the same proven bad cursor into an infinite loop;
- diagnostics preserve the loop evidence and pinned cursor/binding identity.

**Package owner:** S3.5; crash/restart proof reinforced by S3.12.  
**Sequence change:** none.

## 5. Clarification C4 — concurrent same-identity current-entity creation

S3.10 must explicitly include VER-13's concurrent same-identity creation case. In addition to reverse-order current-projection tests:

- two valid requests resolving the same source/canonical identity concurrently must use conditional uniqueness/entity resolution;
- exactly one current canonical/source-presence identity is produced;
- both immutable observations/provenance records remain retained;
- a loser of the creation race re-resolves/attaches rather than producing a duplicate current entity;
- source-native ID reuse guards still split/review genuinely incompatible reuse rather than forcing the uniqueness path to merge unrelated postings.

**Package owner:** S3.10.  
**Sequence change:** none.

## 6. Completeness result

After applying C1–C4 as controlling handoff clarifications, every ROAD-04 item has an implementation owner:

| ROAD-04 obligation | Owner |
|---|---|
| durable frontier / request uniqueness | S3.1, S3.5 |
| service capacity coordinator | S3.3 |
| atomic claim / lease / heartbeat / expiry / attempts | S3.1 |
| fenced commit / current authority / common envelope | S3.2 |
| retry budgets / Retry-After / durable circuit-cooldown / cancellation | S3.4 |
| HTTP crawler / scope / budgets / page validity | S3.5 + C1 |
| cursor/checkpoint / pagination traps / loop quarantine | S3.5 + C3 |
| sitemap | S3.6 |
| cache/revalidation/304 membership | S3.7 |
| coverage union/finalization/PARTIAL/exactly-once absence | S3.8 |
| fallback-rank activation / group truth / run aggregate | S3.9 |
| canonical-presence timing / multi-source availability / stale projection | S3.10 + C4 |
| cancellation-safe local processing / stale evaluation rejection | S3.11 |
| crash/restart recovery | S3.12 |
| automated/migration/package/native readiness | S3.13 |

## 7. Sequencing result

**PASS.** The dependency order is correct:

```text
contracts/schema
→ ownership/fence/capacity/retry
→ crawler/sitemap/revalidation
→ coverage/absence
→ fallback/run truth
→ temporal presence/current projection
→ local obligations
→ cross-cut recovery
→ final acceptance
```

Moving coverage ahead of crawler/revalidation, or availability inference ahead of coverage, would be incorrect. The current plan does neither.

## 8. Future-slice leakage result

**PASS.** No implementation package requires:

- Slice 4 profile/rules product expansion;
- Slice 5 Adapter Lab/recipes;
- Slice 6 browser/authenticated acquisition;
- Slice 7 contacts/application workflow expansion;
- Slice 8 scheduler;
- Slice 9 merge/undo UI;
- Slice 10 export/backup product expansion;
- Slice 11 optional integrations.

References to browser execution classes, auth revocation hooks, egress identity and existing browser/foundation tests are contract/policy compatibility or regression requirements only. They do not authorize those future product features.

## 9. Authority contradictions

**None found.** The current implementation's partial runtime/coverage/recovery behavior is an expected pre-Slice-3 scaffold, not a contradiction with v0.3.1.3. Where the scaffold is weaker than final ROAD-04 semantics (for example source-wide absence scanning, flat listing-state precedence, unconditional current evaluation upserts, fixed retry delay), Slice 3 owns the corrective completion.

## 10. Final planning verdict

**Slice-3 plan: IMPLEMENTATION-READY after reading this audit with the worker plan.**

- package count remains **14 (S3.0–S3.13)**;
- first package remains **S3.0**;
- N3-A remains after **S3.4**;
- full packaged/native W3 remains after **S3.13**;
- no production implementation is authorized by this review itself.