# Windows Job Scraper v0.3.1.3 — Architecture Overview & Authority

**Version:** Windows Job Scraper v0.3.1.3 — Final Corrected Modular Specification Set  
**Date:** 2026-09-05  
**Status:** **NORMATIVE — implementation specification**  
**Target:** Windows 11 · local-first · single-user  
**Core stack:** Python 3.12+ · FastAPI · SQLite + FTS5 · Playwright/Chromium

> This file is one member of the canonical modular specification set. Normative ownership is defined by `00_architecture_overview_and_authority.md`. If duplicated explanatory text conflicts with the normative owner, the normative owner wins.


## ARC-00. Purpose

This document is the **entry point and authority map** for the Windows Job Scraper architecture.

The previous 3,819-line consolidated v0.3.1.3 Markdown served its purpose as a reconciliation artifact, but it is superseded as the day-to-day implementation reference by this modular set.

The corrective review dated 2026-09-05 is also superseded **as an addendum**: its accepted corrections are integrated directly into the normative files below.

A builder should begin here, then use the owning specification for each subsystem.

## ARC-01. Normative document set

| File | Normative ownership |
|---|---|
| `00_architecture_overview_and_authority.md` | system boundaries, authority, canonical flow, invariants, cross-spec rules |
| `01_product_and_workflow.md` | profiles, Inbox, disposition, scoring, eligibility, salary, applications, companies, UI/API behavior, exports |
| `02_acquisition_adapters_and_crawler.md` | Source/Adapter/Binding, fingerprints, strategies, plans, parsers, recipes, Adapter Lab, crawler semantics |
| `03_durable_runtime_and_persistence.md` | run snapshots, queue, leases, attempts, SQLite persistence, provenance presence, revalidation, scheduler |
| `04_security_and_authentication.md` | localhost security, SSRF, browser network controls, auth state, imports, side-effect boundaries |
| `05_windows_packaging_and_operations.md` | launcher, process topology, browser runtime, packaging, dependency locking, backup/upgrade, Doctor |
| `06_verification_and_acceptance.md` | tests, Windows acceptance, security acceptance, resource acceptance, final product acceptance |
| `07_implementation_roadmap.md` | vertical slices, dependency order, definition of done, deferrals |

### Ownership rule

Each architectural rule has one normative home.

Other documents may summarize a rule but should link conceptually to its owner rather than redefining it.

Examples:

- lease/fencing semantics → `03`;
- adapter planning/parsing → `02`;
- localhost/browser security → `04`;
- application state → `01`;
- packaging/browser installation → `05`;
- test proof → `06`;
- build order → `07`.

## ARC-02. Normative language

The words **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

Example counts, timeouts, concurrency values, page budgets and resource ceilings are policy defaults or measurement targets unless explicitly marked as invariants.

## ARC-03. Product definition

Windows Job Scraper is a local job-search operating system whose stable host survives changing source adapters.

The canonical user/data loop is:

```text
discover source
→ classify/fingerprint source
→ acquire through an approved strategy
→ validate the acquired result
→ extract
→ persist observation/evidence
→ normalize
→ reconcile/deduplicate
→ evaluate eligibility
→ score
→ triage in Inbox
→ open exact direct application link
→ track application
→ follow up
→ revalidate and learn
```

This corrects the old shorthand that placed generic “validate” before acquisition.

The product is not complete when it merely scrapes pages.

## ARC-04. Five non-negotiable foundations

1. **Windows-local product.** No PostgreSQL, Redis, Node runtime, cloud service or distributed crawler is required.
2. **Host-owned policy.** Scheduling, I/O, browser execution, auth scope, rate policy, storage, retries and health are enforced by the host.
3. **Replaceable source logic.** Adapters are versioned and expected to break.
4. **Observation before canonicalization.** No collector writes directly to canonical jobs.
5. **User workflow is first-class.** The first useful build reaches Inbox → direct apply link → application tracking.

## ARC-05. Corrected canonical runtime flow

The canonical control flow is:

```text
Run / Durable Frontier
        │
        ▼
claim fenced work + immutable RunSourcePlan
        │
        ▼
Adapter planner
        │
        ▼
host validates ExecutionPlanEnvelope + class-specific payload
(binding revision · hosts · auth · strategy · class · rate · budgets · policy)
        │
        ├───────────────┐
        ▼               ▼
HTTP executor      Browser supervisor
        │               │
        └───────┬───────┘
                ▼
          ResultEnvelope
                │
                ▼
       Page/Result Classifier
                │
      valid? ───┴── no → typed outcome/health/retry
                │ yes
                ▼
Adapter parser / ExtractionRecipe
                │
                ▼
JobObservation + evidence + child-work/cursor proposal
                │
                ▼
fenced persistence / request completion
                │
                ▼
normalization → entity resolution / canonical job identity
                │
                ▼
job-source presence update → canonical provenance/presentation
                │
                ▼
eligibility → scoring → Inbox → application workflow
```

For a previously unknown URL, discovery is still durable work. The host first creates a provisional `Source` and binds the probe to a built-in, immutable **generic discovery binding/revision** (or an equivalent typed `DiscoveryPlan`) before any I/O:

```text
unknown URL
→ provisional Source
→ durable SOURCE_DISCOVERY request + generic discovery binding/plan
→ bounded host-owned discovery probe
→ fingerprint/classify
→ candidate specialized bindings
→ strategy router
→ immutable RunSourcePlan(s)
```

Specialized routing never retroactively rewrites the probe's identity or provenance. The provisional discovery authority is limited to the user-supplied target plus host-approved redirects and normal security policy.

### Flow invariants

- The durable runtime owns work **before** an executor performs I/O.
- A parser MUST NOT emit normal observations from a result that has not passed the validity gate.
- `NavigationPlan` is part of a browser execution plan; it is not a post-fetch extraction stage.
- Adapters plan and parse. Executors perform I/O.
- A stale worker cannot commit.
- Result/observation persistence is fenced.
- Absence evidence is created only from an enumeration explicitly recorded as complete and absence-authoritative for the relevant source/scope.
- Immutable run plans preserve reproducibility, but current cancellation, quarantine, auth revocation, or security-policy denial may invalidate future network execution/commit.
- Every accepted immutable observation atomically creates or satisfies a durable host-native downstream processing obligation; cancellation cannot orphan accepted evidence.
- Mandatory network/security boundaries fail closed: if a selected execution backend cannot enforce a required boundary, that capability is unsupported for that plan.
- Current mutable projections are evidence-ordered/version-checked so older independently valid work cannot overwrite newer authoritative state.

## ARC-06. Stable process topology

```text
JobScraper.exe
  └── local service
      ├── FastAPI / Jinja / HTMX / SSE
      ├── scheduler
      ├── durable claim/capacity coordinator
      ├── SQLite persistence coordinator
      ├── HTTP executor pool
      ├── isolated adapter workers when required
      └── browser worker
          └── Playwright / Chromium process tree
```

Chromium MUST NOT run inside the FastAPI/service process.

The service process is the authoritative durable work/capacity coordinator unless a later architecture explicitly replaces this contract.

## ARC-07. Domain state separation

Three planes remain independent:

```text
Listing lifecycle — system derived
ACTIVE | UNCERTAIN | EXPIRED | CLOSED | WITHDRAWN

User disposition — per (job, profile)
NONE | SHORTLISTED | DISMISSED | SNOOZED | ARCHIVED

Application lifecycle — per application record
PREPARING | APPLIED | SCREENING | INTERVIEWING | OFFER |
ACCEPTED | REJECTED | WITHDRAWN | GHOSTED | CLOSED
```

Eligibility is a **profile-relative evidence verdict**, not a workflow status.

## ARC-08. Acquisition strategy order

```text
PROVIDER_NATIVE
→ FEED_OR_PUBLIC_STRUCTURED_ENDPOINT
→ STRUCTURED_PAGE
→ HTTP_HTML
→ PLAYWRIGHT_PUBLIC
→ PLAYWRIGHT_AUTHENTICATED
→ MANUAL_UNSUPPORTED
```

The cheapest legitimate reliable strategy wins.

`MANUAL_UNSUPPORTED` never means “try an unspecified bypass.”

## ARC-09. Safety boundary

The product does not implement:

- CAPTCHA solving/defeating;
- paywall or access-control bypass;
- anti-bot evasion;
- stolen cookie/session acquisition;
- credential extraction from unrelated browser profiles;
- public proxy harvesting;
- proxy/TLS/browser-identity rotation intended to evade blocking;
- arbitrary remote adapter code loading;
- automatic job application submission in this architecture generation;
- mass outreach or guessed recruiter addresses.

A challenge/block produces backoff, lower load, supported fallback, user re-authentication/interaction where legitimate, or an unavailable/challenged state.

## ARC-10. Versioning and reproducibility invariant

Every source execution is bound to an immutable `RunSourcePlan` that records or references reconstructible immutable state for the exact:

- source identity **and source configuration revision/snapshot**;
- query identity/revision;
- binding + binding revision/config snapshot;
- adapter ID/version/API version;
- strategy and execution class;
- cursor schema version;
- recipe version;
- NavigationPlan version;
- crawl/rate/security policy snapshot;
- profile/rule revision/snapshot;
- normalization/entity-resolution/evaluation version relevant to derived outputs;
- application build identifier for processing that can change interpretation.

A version/hash is sufficient only when the referenced immutable content is retained and resolvable. Promotion affects future plans only. Historical evaluations remain associated with the exact content/profile/rule/build revision they evaluated; they do not silently become current projections after a later profile, parser, or rules change.

## ARC-11. Provenance model in one sentence

```text
FetchAttempt
→ ResultEnvelope
→ ParseAttempt
→ FieldEvidence
→ immutable JobObservation
→ normalization/entity-resolution events
→ create-or-select canonical Job identity
→ mutable job-source presence/provenance
→ canonical presentation/projection
```

No dedup or source promotion may destroy original provenance.

## ARC-12. Corrected implementation sequencing principle

The first user-visible MVP MUST already use the minimal final architecture:

```text
Source
→ AdapterDefinition
→ SourceAdapterBinding
→ RunSourcePlan
→ JobObservation
→ normalization/entity resolution
→ canonical Job identity
→ job_source presence/provenance
→ SearchProfile
→ job_profile_state
→ Inbox
→ direct apply
→ Application
```

Slice 1 is intentionally small, but it is **not** allowed to create a throwaway direct-scraper-to-`jobs` path.

## ARC-13. Cross-spec precedence

When a conflict appears:

1. explicit invariant in this file;
2. subsystem rule in its normative owner file;
3. verification requirement in `06`;
4. roadmap sequencing in `07`;
5. explanatory examples.

A roadmap may defer a feature, but it may not violate a core invariant.

## ARC-14. Final architectural invariants

The implementation is wrong if any of these become false:

1. Every network/browser action is host-policy controlled.
2. Every source execution pins immutable source/binding/adapter/config identity.
3. An expired or stale worker cannot renew ownership or commit.
4. An immutable observation exists before canonical reconciliation.
5. Current source presence is stored separately from immutable observations.
6. Dedup never destroys provenance and must defend against source-native ID reuse.
7. One scrape miss never proves a job closed.
8. Conflicting source availability is resolved by explicit provenance-aware policy.
9. One failed binding may coexist with another healthy binding.
10. Desired state, operational health and quarantine are separate.
11. Listing state, per-profile disposition and application state are separate.
12. Authentication problems cannot become selector-repair problems.
13. Challenges produce backoff/manual handling, not bypass escalation.
14. Adapter Lab promotion is versioned, fixture-gated and reversible.
15. Raw untrusted source content cannot execute in the local dashboard.
16. Browser-side requests cannot silently reach local/private resources outside an explicitly approved feature.
17. Acquisition automation cannot submit a job application or another business transaction.
18. The packaged Windows product is exercised throughout development.
19. The first useful slice reaches Inbox → direct apply → application tracking.
20. The user can explain where a job came from, why it matched, and what changed over time.
21. Absence requires authoritative coverage; partial/query-limited/budget-exhausted runs cannot by themselves age unseen jobs toward expiry.
22. Security revocation outranks reproducibility; a pinned historical plan never authorizes continued execution after current cancellation/quarantine/auth/security revocation.

## ARC-15. Superseded artifacts

The original monolithic v0.3.1.1 file, the v0.3.1.1 modular set, and their audit/corrective-review files remain useful as audit history.

They are **not** required to implement the system once this modular set is accepted.

Do not copy future changes back into the monolith. Update the single owning modular specification instead.
