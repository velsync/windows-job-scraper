# Slice 3 Worker Implementation Plan — v0.3.1.3

Status: **PLANNING COMPLETE — IMPLEMENTATION NOT YET AUTHORIZED BY THIS DOCUMENT**  
Date: 2026-09-10  
Repository: `velsync/windows-job-scraper`  
Planning base branch: `arena/01a0882c-windows-job-scraper`  
Planning base HEAD: `c9cd5e6b426fc40874f9421c05de9f58beca3c03`  
Precondition: Slice 2 **PROMOTED** under `docs/reviews/slice-2-native-promotion-closure-2026-09-10.md`.

Authority: v0.3.1.3 only, especially `00_architecture_overview_and_authority.md`, `01_product_and_workflow.md`, `02_acquisition_adapters_and_crawler.md`, `03_durable_runtime_and_persistence.md`, `04_security_and_authentication.md`, `05_windows_packaging_and_operations.md`, `06_verification_and_acceptance.md`, and `07_implementation_roadmap.md`.

This plan decomposes ROAD-04. It does **not** redesign the architecture. If implementation proves a real contradiction, stop that package and report it rather than inventing new authority.

## 0. Slice 3 mission

Slice 3 turns the existing Slice-1/Slice-2 durable-runtime scaffold into the complete ROAD-04 correctness subsystem:

```text
immutable RunSourcePlan
→ unique durable frontier work
→ service-owned claim/capacity decision
→ fenced attempt + ExecutionPlanEnvelope
→ bounded HTTP crawl / pagination / sitemap / revalidation
→ validated parse + idempotent observations/child work
→ durable coverage generation and finalization barrier
→ canonical identity → job_sources presence
→ evidence-ordered current projection + multi-source availability
→ deterministic fallback-group/run result
→ crash/restart recovery without stale commit or false closure
```

Ship condition from ROAD-04:

> Collection survives worker/service/Windows crashes without stale commits or false closure.

### Existing promoted scaffold that Slice 3 MUST extend rather than duplicate

At the planning base, the repository already contains:

- `runtime/requests.py` — logical request uniqueness and durable enqueue;
- `runtime/claims.py` — basic atomic claim, lease, heartbeat, expiry reclaim;
- `runtime/fence.py` — basic fenced terminal commit;
- `runtime/cancellation.py` — cancellation scaffold;
- `runtime/recovery.py` — service-restart reclaim scaffold;
- `runtime/runs.py` — immutable run plans and an early aggregate truth-table implementation;
- `pipeline/coverage.py` — early coverage-generation/absence scaffold;
- `pipeline/obligations.py` — cancellation-surviving local processing scaffold;
- `acquisition/envelope.py`, `httpexec.py`, `pagevalidity.py`, `result.py` — HTTP execution and validation surfaces;
- migration v5 durable run/request/attempt/cursor tables and v6 coverage/evidence tables; Slice 2 has advanced the released schema through v14.

Do not create a competing queue, second run model, second coverage model, or alternate canonical write path.

## 1. Non-negotiable Slice 3 invariants

1. **Service-owned durable work.** The service is the only durable claim/capacity coordinator. Executors/workers never claim hidden work behind it (RUN-09).
2. **Expired means lost.** `lease_until <= database_now` is already lost ownership; heartbeat cannot revive it before reclaim (RUN-07, VER-01).
3. **Fenced outputs.** Request-owned evidence, observations, deterministic child/local work, cursor/checkpoint updates and terminal transition are atomic or equivalently idempotent under the same ownership fence (RUN-08).
4. **Current authority beats historical pinning.** Cancellation, source/binding quarantine or disablement, permission revocation and applicable auth/security denial block future execution/commit despite immutable RunSourcePlan history (RUN-02 rule 9, SEC-06 current-authorization rule).
5. **No network wait inside DB transactions.** Claims/commit transactions are short; HTTP occurs outside SQLite write transactions (§50).
6. **Logical request idempotency.** Retries mint attempts, not duplicate requests; strategy/purpose/pagination/detail identity remains representable (RUN-05, ACQ-07).
7. **PARTIAL is useful but never absence-authoritative.** Accepted observations/children survive, are idempotent, and cannot authorize absence (ACQ-03, RUN-08/13).
8. **Coverage is generation-wide.** A COMPLETE authoritative generation requires terminal proof, all required contributors terminal, durable union of seen identities and no relevant continuation; one page is never disappearance authority (§40, VER-13).
9. **Scope is explicit.** Declared-scope absence uses explicit source-presence/scope membership. Never infer membership from canonical job fields (RUN-13).
10. **304 is never empty.** Authoritative 304 reuse requires a compatible retained representation **and its membership**; otherwise refetch unconditionally or remain non-authoritative (§40).
11. **Old evidence cannot win by finishing later.** All current mutable projections reject stale observation/coverage/evaluation updates using comparable evidence/input ordering (RUN-14A/RUN-21).
12. **Presence follows canonical identity.** `job_sources` creation/update occurs only after canonical identity is established; observations remain immutable (RUN-11).
13. **Multi-source availability is conservative.** Aggregator disappearance cannot close a job that a trusted employer/ATS still reports active (RUN-14).
14. **Fallback truth is group truth.** Unused fallback is `SKIPPED_NOT_NEEDED`, not failure; successful fallback satisfies the logical group; complete zero-job is success; dynamic work blocks premature run terminalization (RUN-01).
15. **Cancellation cannot orphan accepted evidence.** Host-native downstream obligations created with accepted observations keep draining without new source I/O (§18/RUN-08/VER-13).
16. **No security relaxation.** Existing SSRF/destination validation, host-owned I/O, page-validity gate, safe methods, redaction and no-bypass rules remain intact.
17. **Append-only migrations.** v1–v14 bytes remain pinned; Slice 3 begins at v15. Never edit a released migration.
18. **No future-slice creep.** Slice 3 does not implement Adapter Lab/recipes (Slice 5), authenticated/browser acquisition (Slice 6), profile/rule product expansion (Slice 4), contacts/app workflow expansion (Slice 7), scheduler (Slice 8), merge/undo UI (Slice 9), exports/backup expansion (Slice 10), or optional integrations (Slice 11).

## 2. Package map

| Pkg | Scope | Main surfaces | Migration owner | Native timing |
|---|---|---|---|---|
| S3.0 | Slice-3 contract/fixture lock + schema foundation | adapter/runtime contracts, Slice-3 contract test, schema v15 | v15 | automated only |
| S3.1 | Frontier ownership hardening: claim/lease/heartbeat/reclaim/service epoch | `runtime/requests.py`, `claims.py`, `clock.py`, `recovery.py` | v15 if needed only | batch to N3-A |
| S3.2 | Fenced commit + live authorization + common envelope identity | `runtime/fence.py`, `acquisition/envelope.py`, authorization helper | none expected | batch to N3-A |
| S3.3 | Service capacity coordinator + typed dispatch boundary | new `runtime/capacity.py`, coordinator integration | none expected | batch to N3-A |
| S3.4 | Retry budgets, Retry-After, durable cooldown/circuit, cancellation semantics | new `runtime/retry.py`, `runtime/rate.py`, cancellation | v15 | **N3-A after package** |
| S3.5 | Generic HTTP crawler frontier, scope/budgets, cursor/checkpoint, pagination traps | new `acquisition/crawler/` core | v16 if durable crawler fields needed | automated |
| S3.6 | Sitemap discovery/index/lastmod prioritization | `acquisition/crawler/sitemap.py` | v16 only if required | automated |
| S3.7 | Revalidation cache and authoritative 304-membership reuse | `acquisition/crawler/revalidation.py`, cache store | v16 | automated |
| S3.8 | Coverage union/finalization barrier, PARTIAL, explicit scope membership, exactly-once absence | `pipeline/coverage.py` + scope mapping | v17 | automated |
| S3.9 | Deterministic fallback activation + source-plan-group/run terminal truth | `runtime/runs.py`, group orchestration | v17 | automated |
| S3.10 | Presence/revalidation transitions + temporal stale-projection rejection + multi-source availability | canonical/presence/reconcile surfaces | v17 | automated |
| S3.11 | Cancellation-safe local-processing obligations + evaluation ordering | `pipeline/obligations.py`, ingest/driver | v17 if input-order columns needed | automated |
| S3.12 | Cross-cut crash/restart recovery and service integration | `runtime/recovery.py`, service lifespan/driver | none expected | final native batch |
| S3.13 | Slice-3 acceptance gate, migration proof, packaged/native preparation and corrective review | tests, `scripts/verify_slice3.py`, docs | none | **full W3 native gate** |

Packages are implementation boundaries, not permission to combine code changes. One bounded package at a time; review/commit it before the next. “Batch” below refers only to how many packages may share a later Windows/native acceptance checkpoint.

## 3. Package details

### S3.0 — contract lock and additive schema foundation

**Purpose.** Establish the versioned cross-component contract fixtures required by VER-14 before expanding runtime behavior, and add only the durable storage that later S3 packages provably require.

**Dependencies:** promoted Slice 2 only.

**Required code/docs/tests:**

- Add `tests/contract/test_slice3_contract.py` that pins the invariants in §1 and explicitly forbids new Slice-4+ modules/surfaces.
- Extend the existing adapter/runtime contract fixtures for `PlanningContext`, `ValidatedResultEnvelope`, `ParseContext`, `ParseOutcome`, `DiscoveredTask`, `coverage_proposal`, terminal-no-work and host-native non-network dispatch. Do not create a duplicate contract owner if `adapters/contract.py` already owns the types.
- Add `tests/integration/test_schema_slice3.py`; pin v1–v14 exactly and require sequential append-only Slice-3 versions.
- Migration **v15** owns only runtime foundation gaps needed by S3.1–S3.4. Expected logical additions:
  - durable service/clock epoch identity associated with attempts or equivalent auditable representation;
  - durable binding/host rate/circuit state: `circuit_state`, `cooldown_until`, failure/success counters, last `Retry-After`, last rate event/success;
  - if the existing per-plan `group_outcome` cannot represent both binding-attempt outcome and logical-group current state without ambiguity, an explicit group-state row keyed by `(run_id, source_plan_group_id)` with active fallback rank and group outcome. Prefer this explicit representation rather than overloading one fallback plan row.
- Keep `LATEST_SCHEMA_VERSION`/application schema metadata synchronized.

**Tests must first fail for:** released migration mutation; missing contract fields; duplicate schema versions; host-native task obtaining source-network authority; non-auditable service epoch/rate state.

**Acceptance:** contract and migration tests pass; previous released database migrates forward with backup/migration gates; Slice0/1/2 verification remains green; no production acquisition behavior changes beyond schema availability.

**Out of scope:** claim algorithm, crawler, retry engine, fallback activation behavior, presence changes.

---

### S3.1 — durable frontier ownership: claims, leases, epochs and reclaim

**Purpose.** Harden the existing runtime scaffold to the full RUN-04/05/06/07/20/§50 ownership contract.

**Dependencies:** S3.0.

**Required implementation:**

- Keep `runtime/requests.py` as the single logical enqueue owner. Validate request uniqueness across normalized target + purpose/strategy + logical page/detail/cursor identity.
- Harden `runtime/claims.py` so claim selection and transition are one atomic single-winner operation; due `RETRY_WAIT` is claimable only after durable retry time.
- Every claim mints fresh `attempt_id` and binds it to current service epoch.
- Heartbeat requires matching RUNNING request/current attempt/current epoch, `lease_until > database_now`, and live current authorization/cancellation checkpoint.
- Reclaim marks prior attempt `ABANDONED`; requeue only within retry budget; otherwise terminal failure.
- Implement wall-clock anomaly handling per §50: material anomaly stops new claims, advances/invalidates service epoch, invalidates in-memory ownership and reclaims/retries safely. Monotonic time may govern live elapsed heartbeat cadence; durable comparisons remain DB UTC.
- No I/O while holding the claim transaction.

**Required tests:** duplicate enqueue; concurrent claim race using separate SQLite connections; lease renewal; expired heartbeat rejection before reclaimer; orphan reclaim; attempt-history preservation; budget exhaustion; stale epoch rejection; forward/backward clock-anomaly response; process restart with outstanding RUNNING work.

**Acceptance:** VER-01 ownership tests pass; no stale/expired attempt can heartbeat/reclaim itself; SQLite uses consistent database UTC comparison.

**Windows/native:** no per-package full native gate. Carry into N3-A after S3.4, where real Windows SQLite/process restart is exercised.

**Out of scope:** capacity reservation, HTTP execution, coverage, presence.

---

### S3.2 — fenced commit, live authorization and common execution envelope

**Purpose.** Make ownership and current authorization the unavoidable boundary around request-owned output and executor dispatch.

**Dependencies:** S3.1.

**Required implementation:**

- Extend `runtime/fence.py` so terminal/partial commits re-check request/attempt/lease/service epoch and current run/source/binding/permission/auth/security authorization inside the fence.
- Centralize the live authorization predicate used by claim, pre-dispatch and terminal commit. Historical RunSourcePlan pins remain immutable evidence, not ongoing permission.
- Expand `ExecutionPlanEnvelope` only to the ROAD-04 fields already normative and needed for HTTP: exact request/attempt/run/plan/binding revision/adapter/strategy/class/policy/permission identity. Do not add browser execution behavior.
- Persist `execution_plan_id` on the request attempt before/with dispatch identity so ResultEnvelope can be tied back unambiguously.
- A failed current-authorization check yields typed policy/cancel/auth/quarantine handling; it must not be misreported as parser/source drift.
- `PARTIAL` fenced commits may persist accepted observations/child/local obligations but must not accidentally terminalize as success or restore coverage authority.

**Required tests:** stale attempt commits zero outputs; cancel/quarantine/disable/permission revocation between claim and dispatch blocks execution; same events between dispatch and commit block request-owned terminal commit; host-native local obligations remain allowed to drain after cancellation without source network; common envelope identity matches request/attempt/run plan; malformed envelope cannot change pinned binding/permission identity.

**Acceptance:** output + terminal transition is atomic/idempotent; current authority overrides historical pins at every bounded checkpoint; existing HTTP SSRF and safe-method tests stay green.

**Out of scope:** authenticated login/session implementation, browser plan execution.

---

### S3.3 — service-owned capacity coordinator and dispatcher

**Purpose.** Implement RUN-09 and the execution-class/per-source/per-host capacity seam without letting workers independently claim.

**Dependencies:** S3.2.

**Required implementation:**

- Add a small service-owned coordinator, preferably `runtime/capacity.py`, with explicit capacity classes `HTTP`, `BROWSER`, `BROWSER_INTERACTIVE` even though Slice 3 dispatches only HTTP.
- Track global/class and configured per-source/per-host reservations. Capacity is in-memory coordination over **durable service-owned claims**; do not invent distributed DB tokens.
- Claim only when an eligible reservation can be made, or reserve immediately with deterministic release on dispatch completion/failure/cancellation.
- The service constructs the envelope and invokes the existing executor; executor never sees a DB connection and never claims another request.
- Capacity release is exception-safe. Cancellation and worker failure cannot leak a slot permanently.
- Preserve the one-write-coordinator/process direction where practical; do not block FastAPI event loop with DB/network waits.

**Required tests:** class separation; per-source/per-host caps; no overcommit under concurrent dispatch; slot release on success/failure/cancel/exception; browser-class work is not accidentally sent through HTTP; executor cannot recursively claim; capacity exhaustion leaves request durable/unclaimed rather than failed.

**Acceptance:** service is demonstrably the only durable claim/capacity coordinator; HTTP requests can be dispatched through the new seam without changing adapter I/O authority.

**Out of scope:** browser worker implementation/supervision expansion; scheduler.

---

### S3.4 — retry policy, Retry-After, durable cooldown/circuit and cancellation

**Purpose.** Complete runtime rate/retry/cancel semantics and make restart preserve source-protection state.

**Dependencies:** S3.3.

**Required implementation:**

- Add `runtime/retry.py` for typed failure → retryability/delay/budget decisions. Exponential backoff uses bounded jitter; tests inject deterministic jitter/clock.
- Add `runtime/rate.py` (or one equivalent owner) for the v15 binding/host durable circuit state.
- Honor valid `Retry-After`; persist the effective cooldown so service restart cannot immediately hammer the source.
- Circuit transitions distinguish rate/challenge/auth/policy/source failures from local queue/worker failures; local infrastructure failure normally has no source-health impact.
- Cancellation prevents new acquisition claims and invalidates acquisition commit at the next fence; already accepted local obligations still drain.
- Define how a cancelled/failed attempt is recorded without erasing committed prior observations.

**Required tests:** Retry-After parse/clamp; restart persistence; retry budget; deterministic backoff bounds; local worker failure does not falsely poison source health; cancellation during retry wait; cancellation after observation commit; current cooldown gates dispatch; cooldown expiry re-enables eligible work; challenge never becomes evasion/identity rotation.

**Acceptance:** RUN-10/VER-01 retry/circuit/cancel matrix green; no restart loses active cooldown.

**Native checkpoint N3-A:** after S3.4, package/run a **development native checkpoint** on Windows, not a promotion gate. Prove at minimum: two-process/connection claim race, expired lease cannot revive, process/service kill → attempt abandoned/retry, cancellation blocks acquisition restart, durable Retry-After/cooldown survives service restart, and Doctor/SQLite PRAGMAs remain healthy. S3.0–S3.4 may be batched before this one checkpoint, but they still require separate code reviews/commits.

**Out of scope:** generic crawl breadth, sitemap, absence.

---

### S3.5 — generic HTTP crawler frontier, scope/budgets, cursor and pagination traps

**Purpose.** Add the ROAD-04 HTTP crawler using the now-correct frontier/dispatch substrate.

**Dependencies:** S3.4 and successful automated gates; N3-A should be green before investing in later packages.

**Required implementation:**

Create `src/jobscraper/acquisition/crawler/` with focused modules such as:

```text
scope.py        # allowed hosts/paths/deny patterns and budgets
cursor.py       # pinned cursor schema + compatible resume
pagination.py   # next-work validation and loop/no-progress detection
frontier.py     # typed durable child work only
canonicalize.py # crawl URL identity/tracking normalization reuse
```

- Enforce max depth/pages/requests/bytes/runtime/detail and execution-class budgets; no config may disable every safety bound.
- Persist/checkpoint cursor only under the owning request fence. Resume requires exact compatible adapter/binding/cursor schema or an explicit declared migration; otherwise refuse.
- Detect repeated cursor/URL/normalized page hash/job set, cyclic pagination, no-progress and tracking-only repeats.
- One empty page does not automatically prove terminal enumeration.
- Discovered links/tasks are typed proposals converted to durable requests by the host; discovery never equals I/O authority.
- Robots decision remains visible and recorded; do not invent a bypass path.

**Migration v16:** only if existing cursor/request columns cannot represent required durable checkpoint/budget/revalidation references. Add, never rewrite v15 or earlier.

**Required tests:** all stop policies; scope escape; redirects remain handled by existing destination boundary; duplicate frontier work; cursor resume/reject; crash after cursor proposal before commit; trap detection; budget exhaustion yields explicit incomplete state and no absence authority.

**Acceptance:** bounded generic HTTP enumeration survives retry/restart without duplicate work or unbounded crawl.

**Out of scope:** sitemap (S3.6), revalidation cache (S3.7), browser crawling, recipes.

---

### S3.6 — sitemap discovery and changed-URL prioritization

**Purpose.** Implement the sitemap portion of ACQ §20 as a separately testable crawler feature.

**Dependencies:** S3.5.

**Required implementation:**

- `acquisition/crawler/sitemap.py`: sitemap and sitemap-index parsing using safe XML settings; bounded bytes/item/nesting/count; external entities disabled.
- Preserve `<lastmod>` as advisory prioritization evidence, not proof of content equality or closure.
- Prioritize job/career-scope URLs while still applying normal host/path/scheme/destination and run budgets.
- Sitemap entries enqueue through the ordinary unique durable frontier; no direct fetch loop.
- Malformed/oversized/out-of-scope sitemap entries produce typed non-authoritative diagnostics, never expanded authority.

**Tests:** index nesting bounds; XXE disabled; duplicate URLs; lastmod ordering; out-of-scope entries; malformed XML; budget exhaustion; restart/idempotent enqueue.

**Acceptance:** deterministic offline sitemap fixtures drive only authorized bounded frontier work.

**Out of scope:** crawler UI, adaptive scheduler cadence.

---

### S3.7 — revalidation cache and 304 membership semantics

**Purpose.** Implement the explicit §40 cache representation needed for correct 304 handling.

**Dependencies:** S3.5; S3.6 independent but should normally precede it in sequence.

**Required implementation:**

- Migration **v16** owns the durable cache representation if not already added by S3.5. Logical identity must bind at least source, binding revision, auth-scope generation/reference, request variant, validated page class, content hash, parser/recipe compatibility and retained membership reference for authoritative lists.
- Add `acquisition/crawler/revalidation.py` with conditional-header planning and strict compatibility checks.
- A compatible 304 may advance verification time without content revision. For authoritative list reuse, restore/reuse the retained membership as seen identities for the new coverage generation.
- Missing/pruned/incompatible cache body or membership ⇒ unconditional refetch or non-authoritative outcome; never `SUCCESS_EMPTY`/authoritative-empty.
- Retention/pruning cannot remove a representation/membership while an in-progress accepted revalidation/backup references it; Slice 3 only supplies the reference/hold semantics needed here, not the full Slice-10 backup product expansion.

**Tests:** list A/B then 304 retains A/B; same after service restart; auth/binding/request-variant/parser mismatch refuses reuse; pruned membership forces refetch/non-authoritative; 304 does not increment content revision; normal 200 changed/unchanged hash behavior.

**Acceptance:** all VER-13 revalidation/cache cases green.

**Out of scope:** scheduler cadence/adaptive 304 tuning; full retention UI.

---

### S3.8 — coverage generation, finalization barrier, explicit scope and exactly-once absence

**Purpose.** Replace the current minimal coverage scaffold with the full §40/RUN-13 correctness semantics.

**Dependencies:** S3.5 and S3.7.

**Required implementation:**

- `pipeline/coverage.py` remains the single coverage owner; do not create a second model.
- Migration **v17** adds only missing semantics, especially explicit source-presence ↔ authoritative-scope membership when one presence may belong to multiple scopes, and an explicit idempotency/application marker if `applied_at` alone cannot safely prove exactly-once per affected presence.
- Every page/request contributing to a generation is durably linked; seen identities are the stable union across all pages and 304 membership reuse.
- `COMPLETE` only after terminal cursor/snapshot proof, all required contributing list requests accepted terminal, no continuation remains, seen union durable, and required detail work complete when the binding declares listing identity insufficient.
- Unstable pagination/order is non-authoritative unless the binding fixture proves a stable snapshot/cursor mechanism.
- `PARTIAL`, `BUDGET_EXHAUSTED`, cancelled, failed, challenge/auth/policy invalidation never produces absence transitions.
- Apply each coverage generation to each relevant source-presence/scope at most once. Re-finalization/replay is idempotent.
- **Correct current scaffold weakness:** declared-scope absence must not scan every presence for `source_id`; it must use explicit same-scope membership.
- Overlapping generations completing in reverse order cannot regress newer presence.

**Tests:** page1 A + page2 B union; terminal with detail-pending sufficiency variants; cancellation/failure/budget before barrier; duplicate finalization/application; overlapping generation reverse completion; unstable pagination denial; same-source two scopes where missing in scope A cannot age a presence belonging only to B; PARTIAL degradation survives restart.

**Acceptance:** VER-04 + VER-13 coverage/finalization matrix green; false closure/expiry from incomplete/wrong-scope runs is impossible.

**Out of scope:** canonical multi-source resolution policy itself (S3.10).

---

### S3.9 — fallback activation and run aggregate truth

**Purpose.** Complete deterministic `source_plan_group_id` execution semantics and prevent premature terminalization.

**Dependencies:** S3.4; coverage-aware terminal facts from S3.8.

**Required implementation:**

- `runtime/runs.py` remains run/group truth owner.
- Track current active `fallback_rank` deterministically; rank N+1 activates only after N reaches a declared fallback-eligible terminal result.
- Prior failed fallback remains diagnostic history; a later success makes the logical group `SATISFIED`; unused later ranks become `SKIPPED_NOT_NEEDED`.
- Complete valid zero-job result is `SATISFIED`.
- Accepted bounded partial is `SATISFIED_PARTIAL` and visibly incomplete.
- Run cannot terminalize while any relevant dynamically created enumeration/detail/continuation/local-processing obligation is pending.
- Restart between fallback failure and next activation reproduces the same next rank exactly once.
- Aggregate exactly follows RUN-01 truth table.

**Tests:** primary fail + fallback success; primary success unused fallback; zero-job success; all fail; policy denied mix; partial mix; restart between ranks; duplicate activation race; dynamic child/local obligation blocks terminal; cancellation preserves already committed usable data and produces CANCELLED.

**Acceptance:** all VER-13 run/fallback/recovery cases green.

**Out of scope:** adding new ATS/provider adapters; router redesign.

---

### S3.10 — presence transitions, temporal ordering and multi-source canonical availability

**Purpose.** Make mutable `job_sources` and canonical listing status evidence-ordered, conservative and source-quality aware.

**Dependencies:** S3.8 and S3.9.

**Required implementation:**

- Preserve immutable `JobObservation`; update/create `job_sources` only after canonical identity resolution.
- Define one comparable current-evidence order per presence (effective/observed source time plus local receipt/revision tie-breaker) and conditional update/recompute so an older valid request completing later cannot overwrite newer current presence.
- Seen in complete/current evidence → ACTIVE; one same-scope authoritative absence → UNCERTAIN; repeated policy evidence → EXPIRED candidate; explicit trusted provider/detail close → CLOSED; newer trusted active may reopen.
- Preserve source quality/provenance. Canonical `jobs.listing_status` resolution must use trusted/source-quality evidence and temporal precedence, not only a flat precedence over the set of current strings.
- Employer/origin trusted active defeats aggregator disappearance; conflicts remain visible/conservative.
- Source-native ID reuse generation guard stays intact.
- Record meaningful availability history (`JOB_CLOSED`, `JOB_REOPENED`, etc.) exactly once for the accepted revision.

**Migration v17:** add only the comparable projection/evidence-order fields that cannot be represented by existing `content_revision`, timestamps and coverage IDs. Avoid redundant history tables.

**Tests:** two independent requests complete reverse order; trusted close then newer reopen and reverse worker completion; newer trusted close supersedes older active; aggregator missing + employer active; repeated authoritative absence policy; failed/challenged run no presence aging; first presence happens only after entity resolution; duplicate/replay idempotency.

**Acceptance:** RUN-11/13/14/14A/21 and VER-04 temporal/presence cases green.

**Out of scope:** merge/undo workflow (Slice 9); Inbox policy changes beyond correct events caused by listing transitions.

---

### S3.11 — cancellation-safe local processing and stale evaluation rejection

**Purpose.** Close the other half of RUN-08/RUN-21: accepted observations must materialize downstream exactly once, and older local evaluations must not become current after profile/content revision advances.

**Dependencies:** S3.10.

**Required implementation:**

- Keep deterministic creation/satisfaction of host-native `RECONCILE`, `ELIGIBILITY`, `SCORE` obligations inside the acquisition fenced commit.
- On crash/cancel immediately after observation commit, local obligations remain claimable and drain without network I/O.
- Harden `pipeline/obligations.py` conditional writes so `job_eligibility`/`job_scores` current rows compare exact job-content/profile/rules/normalization/evaluator/scorer inputs; older evaluation cannot overwrite a newer revision merely by finishing later.
- Reconciliation uses S3.10 evidence-order resolver rather than a flat state-list precedence.
- Duplicate delivery/retry creates no duplicate observation, obligation, history or Inbox trigger.
- Host-native dispatcher remains unable to construct source-network authority.

**Tests:** crash after observation before normalize; cancel after observation; duplicate obligation delivery; reversed eligibility/score completion after profile/content revision; restart drains exactly once; source-network call attempted by host-native task is denied; run terminalization waits for required local obligations from S3.9.

**Acceptance:** accepted evidence cannot be orphaned; current evaluations reflect newest exact inputs; all historical evidence remains auditable.

**Out of scope:** new profile/rule UX or scoring semantics (Slice 4).

---

### S3.12 — cross-cut crash/restart recovery and service integration

**Purpose.** Prove the individual pieces behave as one recoverable runtime under real restart boundaries.

**Dependencies:** S3.1–S3.11.

**Required implementation:**

- `runtime/recovery.py` performs one deterministic startup reconciliation before new claims: abandon impossible prior-epoch ownership; preserve/restore retry/cooldown; continue unfinished coverage generations; activate exactly the correct fallback rank; leave finalized generations immutable; drain required local obligations; recompute/repair only current projections whose durable inputs prove it is safe.
- `service/runner.py`/lifespan starts coordinator/recovery in correct order: database/migrations → fresh service epoch/recovery → coordinator → listener/background processing. Do not let listener-driven source work race startup recovery.
- Succeeded requests are never replayed as network work.
- Compatible cursor resumes; incompatible pinned implementation/cursor refuses with explicit diagnostic rather than silently substituting a newer parser.
- Recovery remains idempotent if service crashes again during recovery.

**Required integration/crash tests:** crash after claim before dispatch; during HTTP I/O; after fetch before observation commit; after observation/local-obligation insert before terminal transition; during child enqueue; immediately before/after SUCCEEDED; during fallback activation; during coverage finalization; with active Retry-After; with cancellation; adapter promotion while old run remains resumable.

**Acceptance:** RUN-19 complete matrix passes and repeated recovery is idempotent; stale prior owner can never commit.

**Windows/native:** behavior is included in final W3 gate in S3.13. Platform-neutral fault injection must pass first.

**Out of scope:** browser-worker-death behavior beyond maintaining existing Slice0/2 regression; browser acquisition is Slice 6.

---

### S3.13 — Slice-3 acceptance gate, migration proof, packaged/native preparation and corrective review

**Purpose.** Freeze the finished automated definition of Slice 3 and prepare one exact candidate for native promotion testing. This package does not itself declare promotion.

**Dependencies:** S3.0–S3.12 complete.

**Required deliverables:**

- `tests/integration/test_slice3_acceptance.py` covering the coherent runtime vertical:

```text
multi-page/cursor source
→ unique durable requests
→ service claim/capacity/envelope
→ retry/cooldown as needed
→ idempotent parse/children
→ coverage union/finalization
→ presence/current availability
→ local processing
→ deterministic group/run result
→ restart/resume
```

- `scripts/verify_slice3.py` as the authoritative automated Slice-3 gate. It must include the queue/lease matrix, contract fixtures, crawler/sitemap/revalidation fixtures, coverage, fallback, projection-order, local-obligation and crash/recovery suites plus Slice0/1/2 regression gates.
- Migration test from the **actual promoted Slice-2 schema v14**; v1–v14 bytes remain unchanged; integrity/foreign-key/application consistency/effective PRAGMA checks pass.
- Update package verification only when a real new packaged resource/import requires it; do not add speculative hidden imports.
- Produce a Slice-3 corrective review/status record listing every finding and disposition.
- Produce/finalize `docs/plans/slice-3-windows-acceptance-strategy-v0313.md` before native execution.

**Acceptance before native:** focused suites + full test suite + `scripts/verify_slice3.py` + Ubuntu and Windows CI + packaged-build verification all green for one frozen candidate; zero unresolved blocking corrective findings.

**Out of scope:** declaring Slice 3 promoted without the separate packaged/native evidence and controlling promotion closure.

## 4. Windows/native acceptance strategy and batching

### N3-A — intermediate native risk checkpoint after S3.4

This is intentionally **not** a promotion gate. S3.0–S3.4 may share one native checkpoint because their individual semantics are mostly deterministic SQLite/runtime logic, but their combination has Windows process/locking/time/restart risk worth exposing early.

Required checkpoint scenarios:

1. separate Windows SQLite connections race one claim → one winner;
2. expired lease heartbeat/commit rejected even before reclaim;
3. terminate service with RUNNING work → prior attempt becomes abandoned/retry under fresh epoch;
4. cancellation prevents restarted acquisition work while accepted local work remains durable;
5. persisted Retry-After/cooldown survives process restart;
6. WAL/FK/synchronous/busy-timeout/Doctor remain correct.

Failure blocks S3.5 until corrected.

### Full packaged/native W3 promotion gate after S3.13

S3.5–S3.13 may be developed with automated/Windows-CI evidence before the final native gate. Do **not** run full native acceptance after every crawler/data-lifecycle package; most parser/cursor/rate/coverage logic belongs in deterministic offline tests per VER §67.

The final exact package must rerun W0 + W1 + W2 foundation regression and add a bounded W3 companion matrix focused only on product/OS integration that automated tests cannot substitute. The separate Windows strategy defines the exact W3 checks.

Recommended full native W3 emphasis:

- real packaged service kill/restart with active durable work;
- lease/fence/service-epoch recovery;
- persisted cooldown across restart;
- cursor/coverage resume across target-process restart;
- cancellation + local-obligation drain across restart;
- packaged generic HTTP fixture crawl/sitemap/304 membership path;
- reverse-order/revalidation state remains correct after restart;
- post-workload Doctor/SQLite health and resource evidence.

Do not claim Slice-3 promotion until one unchanged candidate/build passes automated gate, full CI, package verification, W0/W1/W2 regression, W3, and a controlling promotion-closure record.

## 5. Recommended implementation order and pause points

Strict order:

```text
S3.0 → S3.1 → S3.2 → S3.3 → S3.4 → N3-A
→ S3.5 → S3.6 → S3.7 → S3.8 → S3.9
→ S3.10 → S3.11 → S3.12 → S3.13 → full W3 native gate
```

Recommended human review pauses because a mistake here contaminates many later packages:

- after **S3.4 / N3-A** — ownership/capacity/retry substrate frozen;
- after **S3.8** — disappearance authority frozen;
- after **S3.10** — temporal/current availability semantics frozen;
- after **S3.13** — candidate freeze before native promotion.

## 6. Per-package coding-agent handoff template

Give the agent exactly one `S3.x` section plus this header:

```text
Work only on velsync/windows-job-scraper, authoritative Slice-3 implementation
lineage descended directly from the accepted prior package. v0.3.1.3 is
normative. Read AGENTS.md, the owning v0.3.1.3 modules, this Slice-3 worker
plan, the prior package commit/review, and relevant existing code/tests before
editing.

Implement ONLY S3.x. Use TDD: failing tests first, then the smallest code that
satisfies the frozen contract. Extend existing runtime/pipeline owners; do not
create parallel queue/coverage/canonical-write systems. Do not implement later
S3 packages or Slice 4+. Do not weaken security, fencing, migration pins or
recovery semantics to make tests pass.

Run focused tests, then the full currently-required regression set. Commit one
coherent package and report exact commit SHA, files changed, tests/results,
migration changes, remaining blockers and whether any architecture
contradiction was discovered. Do not start the next package.
```

## 7. Relative difficulty

Likely easiest:

- **S3.0** — contracts/schema lock, mostly bounded fixtures/migration discipline;
- **S3.6** — sitemap parsing/prioritization, highly fixture-driven and isolated;
- **S3.13** — once prior behavior is correct, mostly gate/harness/governance work (though broad verification can expose defects).

Medium/high:

- S3.1–S3.4 — correctness-sensitive concurrency/runtime work, but existing scaffolds reduce greenfield scope;
- S3.5/S3.7 — crawler/revalidation breadth with many failure modes;
- S3.9/S3.11 — orchestration/idempotency across existing subsystems.

Hardest:

- **S3.8** — coverage generation/finalization/exactly-once absence across partial/restart/overlap/scope;
- **S3.10** — temporal stale-projection rejection + multi-source availability and reopen/close truth;
- **S3.12** — cross-cut crash/restart recovery across all prior semantics.

## 8. Scope-size comparison with Slice 2

Slice 2 had 10 packages (S2.0–S2.9) and primarily broadened structured acquisition plus canonical/search surfaces while explicitly deferring the full durable frontier/crawler/revalidation system.

Slice 3 has **14 packages (S3.0–S3.13)** and is **larger and materially harder than Slice 2 in correctness complexity**, even if its net source-provider feature count is smaller. The reason is cross-cut state-machine/concurrency work: claims, leases, capacity, retries, circuits, coverage, 304 cache membership, fallback truth, temporal ordering and crash recovery must agree under interruption and reverse completion order.

## 9. Recommended first implementation package

**S3.0 — contract lock and additive schema foundation.**

Do not start with claims/crawler code. VER-14 requires the versioned contract fixture gate before cross-component expansion, and v15 establishes the durable storage later packages depend on. S3.0 is also the cleanest point to pin the promoted Slice-2 schema and prevent accidental architecture drift before concurrency work begins.

## 10. Planning audit

The completed decomposition was audited against ROAD-04 and the owning normative modules.

### Coverage audit — every ROAD-04 item has one primary owner

- full durable frontier / request uniqueness → S3.1/S3.5;
- service-owned capacity coordinator → S3.3;
- atomic claims / leases / heartbeat / expired rejection / attempts → S3.1;
- fenced terminal commits / current revocation override / common envelope → S3.2;
- retries / Retry-After / durable circuit-cooldown / cancellation → S3.4;
- cursor/checkpoint + HTTP crawler + budgets/scope → S3.5;
- sitemap → S3.6;
- revalidation + 304 cache-membership → S3.7;
- coverage union/finalization + PARTIAL + absence exactly once → S3.8;
- fallback rank/group/run aggregate truth → S3.9;
- job-source presence + multi-source availability + temporal ordering → S3.10;
- cancellation-safe local downstream processing + stale evaluation rejection → S3.11;
- crash/restart recovery → S3.12;
- integrated automated/package/native readiness → S3.13.

### Sequencing audit

- contracts precede cross-component behavior;
- ownership/fence precedes capacity/retry;
- runtime substrate precedes crawler;
- crawler/revalidation precede authoritative coverage;
- coverage precedes availability inference;
- availability ordering precedes downstream reconciliation;
- all components precede crash/restart integration and final acceptance.

No dependency cycle was found.

### Future-slice audit

Explicitly excluded: profile/rules product expansion, Adapter Lab/recipe teaching, browser/authenticated acquisition, contacts/application workflow expansion, scheduler, merge/undo UI, exports/backup product expansion and optional integrations. Existing security/browser/foundation behavior is regression-tested only; it is not expanded here.

### Authority audit

No v0.3.1.3 contradiction was found during planning. The main implementation implication is that Slice 3 is a **hardening/completion** of already-created runtime/coverage/obligation scaffolds rather than a greenfield replacement.

### Known scaffold gaps the implementation must not mistake for final semantics

- current coverage absence logic must be made explicitly scope-aware;
- current listing reconciliation is not yet sufficient for source-quality + temporal evidence precedence;
- current eligibility/score upserts require stale-input rejection;
- current retry delay is not the full typed durable Retry-After/circuit policy;
- current restart recovery does not yet reconcile the complete crawler/coverage/fallback/local-processing state machine;
- no full service-owned capacity coordinator or generic sitemap crawler is present at the planning base.

These are expected ROAD-04 work, not architecture contradictions.