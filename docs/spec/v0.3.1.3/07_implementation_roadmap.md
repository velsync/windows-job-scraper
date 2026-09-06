# Windows Job Scraper v0.3.1.3 — Implementation Roadmap

**Version:** Windows Job Scraper v0.3.1.3 — Final Corrected Modular Specification Set  
**Date:** 2026-09-05  
**Status:** **NORMATIVE — implementation specification**  
**Target:** Windows 11 · local-first · single-user  
**Core stack:** Python 3.12+ · FastAPI · SQLite + FTS5 · Playwright/Chromium

> This file is one member of the canonical modular specification set. Normative ownership is defined by `00_architecture_overview_and_authority.md`. If duplicated explanatory text conflicts with the normative owner, the normative owner wins.


## ROAD-00. Purpose

This file owns implementation order.

No calendar duration is normative.

Every slice produces a Windows-runnable build and respects the final architecture; no slice may create a throwaway path that violates a core invariant.

## ROAD-01. Slice 0 — Windows foundation and safety shell

Implement:

- repository/package skeleton;
- locked dependencies/build metadata;
- PyInstaller `--onedir`;
- launcher + single instance;
- FastAPI/Jinja/HTMX shell;
- SQLite connection/migration foundation;
- backup-before-migration;
- event log;
- localhost Host/Origin/mutation security;
- protected per-install token;
- basic Doctor;
- browser worker skeleton;
- Playwright/browser compatibility reporting;
- concrete launcher→service→dashboard bootstrap/session authentication contract;
- authenticated private reads/SSE/download boundary with minimal public liveness only;
- SQLite `foreign_keys`/WAL/durability settings verification;
- pinned Windows timezone-data packaging/Doctor smoke;
- application backup-generation manifest + stopped/isolated restore foundation.

**Ship condition:** packaged Windows application starts, stores safe local state, protects mutations and reports actionable health.

## ROAD-02. Slice 1 — minimal final-architecture product MVP

Implement the smallest end-to-end architecture that is **not disposable**:

```text
Source
AdapterDefinition
SourceAdapterBinding + immutable BindingRevision
RunSourcePlan with pinned fallback order
one simple reliable API/feed adapter
Query
one SearchProfile
one acquisition request path
JobObservation
canonical Job identity via entity resolution
job_source presence/provenance
job_profile_state + durable Inbox event
basic deterministic score
basic eligibility state
Inbox
shortlist/dismiss
direct source/application URL
Application record/status
restart persistence
```

Also include the minimum request/attempt identity required to persist provenance safely, even if the full high-concurrency lease runtime is not yet enabled.
 Slice 1 also includes the **minimum final safety prerequisites before its first source content is exposed**: URL/destination SSRF checks, page-validity gate, safe escaped/sanitized rendering, host-owned execution envelope, immutable observation identity, fenced/idempotent output persistence, atomic durable downstream-processing obligation, cancellation checks and projection ordering sufficient to prevent an older valid result overwriting a newer one. These mechanisms may be minimal in capacity/complexity but are not deferred to Slice 3/6.

**Ship condition:** complete packaged vertical slice passes:

```text
launch
→ create profile
→ collect
→ observation/provenance
→ canonical job
→ Inbox
→ shortlist/dismiss
→ direct apply
→ application tracking
→ restart
→ state preserved
```

### Prohibition

Slice 1 MUST NOT implement:

```text
adapter → directly write jobs
```

and promise to retrofit provenance later.

## ROAD-03. Slice 2 — structured acquisition breadth

Implement:

- Greenhouse;
- Lever;
- Ashby;
- ATS fingerprinting;
- strategy router;
- richer ResultEnvelope/evidence;
- origin resolver;
- companies;
- multi-location;
- content cleaning;
- FTS5;
- canonical provenance selection.

**Ship condition:** several browser-light structured sources run through the same Source/Binding/RunSourcePlan/Observation contracts.

## ROAD-04. Slice 3 — full durable runtime and revalidation

Implement:

- full durable frontier;
- service-owned capacity coordinator;
- atomic claims;
- leases/heartbeat;
- expired-lease rejection;
- attempt history/fencing;
- fenced terminal commits;
- retry budgets;
- `Retry-After`;
- durable circuit/cooldown;
- cancellation;
- cursor/checkpoint;
- request uniqueness;
- binding revisions + fallback-rank pinning;
- common `ExecutionPlanEnvelope`;
- current quarantine/auth/security revocation override over historical pinned plans;
- HTTP crawler;
- budgets/scope;
- sitemap;
- page validity;
- job-source presence;
- revalidation including explicit `304` cache-membership semantics;
- coverage-generation union/finalization barrier + exactly-once absence application;
- source-plan-group fallback activation and run aggregate truth table;
- `PARTIAL` parse idempotency semantics;
- job-source presence after canonical identity resolution;
- temporal/evidence ordering for independent requests and stale-projection rejection;
- cancellation-safe local downstream processing of accepted observations;
- multi-source availability resolution;
- crash/restart recovery.

**Ship condition:** collection survives worker/service/Windows crashes without stale commits or false job closure.

## ROAD-05. Slice 4 — understanding, scoring and dedup

Implement:

- fact extraction;
- evidence-backed eligibility;
- salary normalization;
- FX provenance/version;
- explainable profile scoring;
- reversible dedup;
- source-ID reuse guard;
- merge ledger + dependent user-state migration/alias/undo contract;
- change/repost logic;
- profile feedback.

**Ship condition:** Inbox quality is explainable and dedup remains reversible.

## ROAD-06. Slice 5 — Adapter Lab and repairability

Implement:

- ExtractionRecipe;
- recipe versions;
- fixture corpus;
- list auto-suggest;
- zero-click structured path;
- locator telemetry;
- deterministic repair;
- schema-validated safe import;
- fixture-gated candidate promotion;
- rollback;
- binding health dimensions/UI.

**Ship condition:** browser-independent/static/structured adapter and recipe work can be added/repaired without Python editing or unsafe capability escalation. Browser navigation recording and authenticated XHR teaching remain disabled until Slice 6 security gates pass.

## ROAD-07. Slice 6 — browser and authenticated sources

Implement:

- browser supervisor hardening;
- NavigationPlan execution + recorder under host-approved capabilities;
- authorized XHR/API teaching only after browser containment is active;
- browser SSRF/private-network controls including fail-closed service-worker/WebSocket/alternate-channel policy;
- resource interception with source opt-out;
- safe source-content rendering;
- storage-state-first authentication;
- IndexedDB capture where required/supported;
- dedicated persistent-profile fallback;
- auth expiry handling;
- cross-source session isolation;
- session candidate validation/promotion;
- redacted authenticated fixtures;
- browser acquisition side-effect guard.

**Ship condition:** a legitimately authorized source runs without credential leakage, local-network reach-through, source-content XSS or access-control bypass.

## ROAD-08. Slice 7 — workflow depth and operations

Implement:

- reminders with durable occurrence identity/catch-up semantics;
- Windows notifications;
- contacts;
- documents;
- application events;
- complete CSV/JSON export contract;
- diagnostics bundle;
- scheduler refinements;
- company watch/blocklist;
- outcome analytics;
- backup/restore UX.

**Ship condition:** discover → apply → follow-up loop is operational and export/diagnostics are complete.

## ROAD-09. Slice 8 — optional expansion

Candidates:

- more ATSs;
- company bulk import;
- JobSpy;
- Crawlee;
- Scrapling;
- embeddings;
- LLM assistance;
- alternate browser experiments.

Each candidate requires its own promotion gate and may not weaken the safety boundary.

## ROAD-10. Adapter architecture acceptance set

This is **not** the product MVP.

After the runtime/adapter architecture matures, prove:

1. one provider-native/API source;
2. one structured/static HTTP ATS;
3. one browser-required source;
4. one generic careers URL fingerprinted/routed successfully.

Do not chase adapter count before contracts are stable.

## ROAD-11. Definition of done for every slice

A slice is complete only when:

- relevant automated tests pass;
- migration from prior released slice is verified;
- packaged Windows build is produced;
- changed OS-facing behavior is tested on real Windows;
- resource measurements are captured where relevant;
- no unresolved critical security regression remains;
- diagnostics redaction passes;
- affected fixtures pass;
- recovery/rollback is tested where relevant;
- the user-visible feature actually works from the packaged build;
- architecture ownership docs are updated only in their normative files;
- every exposed capability's security/recovery prerequisites are implemented no later than its first consuming slice;
- relevant versioned cross-component contract fixtures pass before parallel component integration.

Core rule:

> State the invariant → implement the smallest enforcing mechanism → test the failure mode → verify on the correct layer → move forward.

## ROAD-12. Explicit deferrals

Do not delay the core build for:

- automatic job application submission;
- cloud sync;
- multi-user auth;
- mobile app;
- distributed crawling;
- universal ATS support;
- universal company import;
- untrusted plugin marketplace;
- remote arbitrary adapter code;
- mandatory embeddings/LLM extraction;
- autonomous scoring changes;
- autonomous recipe activation;
- destructive dedup;
- mass recruiter outreach;
- guessed contact emails;
- CAPTCHA solving;
- anti-bot/access-control bypass;
- public proxy harvesting;
- premature Chromium replacement.

## ROAD-13. Recommended immediate implementation starting point

After this modular architecture set is accepted:

1. freeze these documents as the architecture baseline;
2. create an implementation backlog for Slice 0 only;
3. break Slice 0 into small testable work packages;
4. implement/test/package Slice 0;
5. only then decompose Slice 1.

Do not generate one enormous implementation prompt for all nine slices.
