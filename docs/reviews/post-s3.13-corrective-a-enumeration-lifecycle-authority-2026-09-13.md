# Post-S3.13 Corrective A — Enumeration Lifecycle & Authority

**Status:** APPROVED ARCHITECTURE LOCK — A0 GOVERNANCE AUTHORITY; production behavior changes remain gated by A1–A5
**Date:** 2026-09-13
**Repository:** `velsync/windows-job-scraper
**Working lineage reviewed:** `feat/slice3-s3.5-generic-http-crawler`, current harness checkpoint `0a531c0` as supplied by the operator
**Normative baseline:** `docs/spec/v0.3.1.3/`, Slice-3 worker plan, Slice-3 Windows acceptance strategy, and controlling Slice-3 planning corrective R2
**Trigger:** packaged W3 calibration against frozen candidate `c1cc7b07af9ce74f5629aa2de6332709e1739c1a` exposed a real W3-05 correctness failure plus broader lifecycle contradictions.

---

## 1. Executive decision

The W3-05 failure is not an isolated cache bug. The current implementation conflates two different state lifetimes:

1. **run-continuation pagination state** — e.g. “this exact RunSourcePlan finished page 1; after a crash continue at page 2”; and
2. **cross-run synchronization state** — e.g. a provider-defined durable delta token or watermark intentionally reusable by a later run.

The current `crawl_cursors` v16 design stores one mutable compatible row per binding revision + adapter/version/schema and lets a later RunSourcePlan inherit the previous plan's ordinary pagination position. Coverage, requests, attempts, and absence authority are instead scoped to the exact RunSourcePlan. That mismatch can cause a fresh coverage generation to begin in the middle of a prior enumeration with an empty seen-membership prefix.

The corrective architecture is therefore:

- ordinary `crawl_cursors` become **RunSourcePlan-scoped continuation checkpoints**;
- a fresh RunSourcePlan starts its own enumeration from its authoritative beginning;
- retained HTTP cache representations remain binding-revision/request-variant scoped and may safely cross runs;
- fresh-run 304 revalidation happens naturally by planning the seed page, attaching exact validators, and restoring retained membership into the new coverage generation;
- true cross-run incremental synchronization, if added later, must use a **separate explicit checkpoint abstraction** and MUST NOT reuse `crawl_cursors`;
- coverage authority is no longer hard-coded by the driver; it is pinned immutably into the RunSourcePlan from an explicit adapter/binding enumeration contract;
- concurrent compatible runs cannot share mutable pagination state;
- run counters distinguish `NEW`, `UPDATED`, `UNCHANGED`, and stale/no-current-effect observations so an unchanged re-observation does not inflate `jobs_updated`.

This corrective requires a new additive schema migration **v22**. Migrations v1-v21 remain byte-for-byte immutable.

Corrective A deliberately does **not** implement the missing packaged acquisition coordinator. That is Post-S3.13 Corrective B and must follow only after enumeration state has correct ownership and authority.

---

## 2. Proven contradiction requiring an architecture amendmen

`AGENTS.md` permits redesign only when a genuine contradiction or implementation blocker is proven. That threshold is met.

### 2.1 Higher-level recovery authority is same-run

The acceptance authority requires an **interrupted run** to resume from a compatible cursor. W3-03 is explicitly a process/service restart of the **same durable RunSourcePlan**, with already-succeeded work not refetched merely because of restart.

### 2.2 v16 cursor architecture went further

The v16 migration and `acquisition/crawler/cursor.py` explicitly make RunSourcePlan identity *not* part of cursor lookup identity. A compatible later run can load the same binding-wide cursor; only pagination-guard history is reset.

### 2.3 Coverage authority is plan-scoped

S3.8 correctly makes every `enumeration_coverage` generation, contributing request, and seen-identity union belong to one exact RunSourcePlan. A later plan therefore starts with a new seen union even when it inherits an old binding-wide cursor.

### 2.4 Packaged W3 proved the unsafe composition

The W3-05 native evidence showed:

- first run: page 1 returned A/B and stored an ETag and retained membership;
- first run: page 2 completed terminally;
- second fresh run: inherited page-2 cursor;
- page 1 was never requested;
- no `If-None-Match` could be sent;
- no 304 could occur;
- the fresh coverage generation had no A/B seen membership;
- A/B moved `ACTIVE -> UNCERTAIN` under fresh full-source absence authority.

The same mechanism can miss a newly posted job inserted on page 1 between runs. This is a periodic-collection correctness defect, not merely a native-test inconvenience.

---

## 3. Existing lifecycle map

| State / fact | Current owner | Current lifetime | Corrective disposition |
|---|---|---|---|
| RunSourcePlan | `run_source_plans` | exact run/plan, immutable | Keep |
| durable request | `scrape_requests` | exact run/plan | Keep |
| request attempts / fences | request + service epoch | exact attempt | Keep |
| ordinary pagination cursor | `crawl_cursors` | **binding-wide compatible row** | Change to exact RunSourcePlan |
| pagination guard | same cursor row | same plan only in code | Keep same-plan; storage becomes plan-scoped |
| coverage generation | `enumeration_coverage` | exact RunSourcePlan | Keep |
| coverage seen union | `coverage_seen_identity` | exact coverage generation | Keep |
| cache representation | `cache_representation` | binding revision + exact request variant/parser/auth scope | Keep cross-run |
| retained list membership | cache representation membership | exact representation | Keep cross-run |
| Retry-After / circuit state | binding + host | cross-run service protection | Keep cross-run intentionally |
| true incremental watermark | no distinct owner | conflated conceptually with cursor capability | Do not implement in Corrective A; reserve separate future abstraction |

The design rule is: **mutable state may cross a RunSourcePlan boundary only when its semantics explicitly require cross-run lifetime.** Ordinary pagination does not.

---

## 4. Approaches considered

### Approach 1 — plan-scoped cursors + pinned enumeration contract (**recommended**)

Use the existing `checkpoint_run_source_plan_id` as the real cursor lookup/uniqueness owner in v22. Keep cache representations cross-run. Pin coverage authority/scope/stability into every new RunSourcePlan. Reserve true incremental state for a different future abstraction.

**Advantages**

- directly aligns cursor, request, coverage, and recovery lifetimes;
- same-plan restart remains efficient and exact;
- W3-05 304 path becomes normal S3.7 behavior rather than a W3 special case;
- avoids destructive cursor-table rebuild;
- eliminates shared mutable cursor interference between overlapping plans;
- preserves immutable historical v1-v21 migration bytes.

**Cost**

- v22 migration;
- accepted tests that encoded arbitrary cross-run page-offset reuse must be corrected;
- fresh periodic runs perform real verification again (conditional when possible), which is correct behavior rather than a regression.

### Approach 2 — keep binding-wide cursors but add cursor-kind flags

Add `RUN_SCOPED` vs `CROSS_RUN` flags to the same table and teach the driver/adapters to decide per load.

**Rejected.** The table would continue mixing two lifetimes, keep a shared mutable row as a concurrency hazard, and make a future adapter bug capable of re-authorizing cross-run page-offset reuse. It is more complex and less safe than simply giving ordinary pagination the correct owner.

### Approach 3 — tactical page-1/ETag exception

Keep v16 semantics generally but, when a compatible seed-page cache representation exists, ignore the inherited cursor and revalidate page 1.

**Rejected.** This fixes the observed W3-05 fixture but not the missed-new-job case when no validator exists, not the shared-cursor concurrency hazard, not durable request identity, and not the architectural contradiction.

---

## 5. Chosen architecture

### 5.1 Cursor invarian

`crawl_cursors` means only:

> **durable continuation state for one exact RunSourcePlan.**

Rules:

1. A cursor written by `rsp-A` is loadable only by `rsp-A`.
2. Process/service restart does not change RunSourcePlan identity; therefore the same cursor resumes.
3. A different/new RunSourcePlan never consumes `rsp-A`'s ordinary pagination cursor, even when source, binding revision, adapter version, and schema are identical.
4. Terminal completion does not need to delete the old cursor. It becomes immutable historical continuation evidence for that plan and is simply irrelevant to later plans.
5. The pagination guard is stored and resumed with the same plan-scoped cursor.
6. Legacy rows whose `checkpoint_run_source_plan_id` is NULL remain preserved but non-loadable, as today.
7. A true future provider incremental token/watermark MUST NOT be stored in `crawl_cursors`; it requires a separately named cross-run checkpoint owner and an explicit compatibility/coverage model.

### 5.2 Fresh-run invarian

A new RunSourcePlan begins from the adapter's authoritative seed state:

```tex
new RunSourcePlan
    -> no plan-scoped crawl cursor
    -> adapter plans seed request/page 1
    -> host evaluates exact S3.7 revalidation cache
       -> compatible ETag/Last-Modified: conditional reques
       -> 304: restore retained membership into this new coverage generation
       -> 200: parse current representation normally
    -> continuation cursor belongs to this new RunSourcePlan only


This is the required periodic-collection behavior. It allows newly inserted page-1 jobs to be discovered and allows normal HTTP conditional verification to avoid unnecessary body transfer.

### 5.3 Same-plan restart invarian

```tex
same RunSourcePlan after crash/restar
    -> load its plan-scoped cursor + guard
    -> already-SUCCEEDED requests stay terminal
    -> continue accepted PENDING/RETRY_WAIT work
    -> do not replay page 1 solely because the process restarted
    -> continue the same coverage generation / seen union


This preserves W3-03 and the valid S3.12/S3.13 restart semantics.

### 5.4 Cache lifetime remains cross-run

`cache_representation` is intentionally **not** plan-scoped. It represents an exact validated HTTP representation under:

- source;
- binding revision;
- auth-scope reference;
- exact request variant;
- page class;
- parser/normalization compatibility;
- content hash;
- retained membership when needed.

A later RunSourcePlan may revalidate this immutable representation because the cache contract already proves compatibility. This is qualitatively different from inheriting a mutable page offset.

### 5.5 No implicit incremental semantics

`json_api_feed` currently advertises `incremental` while its only state is ordinary `{page: N}` pagination. Corrective A treats that capability declaration as false metadata.

- Remove the `incremental` capability from the generic JSON feed's effective manifest/contract.
- Do not create a fake cross-run watermark to preserve old test expectations.
- If a future feed supports a real provider delta token, design it explicitly under a separate cross-run sync-checkpoint contract.

The correction should be documented as a capability-declaration corrective; do not silently reinterpret page numbers as incremental state.

---

## 6. Enumeration authority contrac

### 6.1 Problem

`pipeline/driver.py` currently opens every enumeration coverage generation with:

`coverage_authority="AUTHORITATIVE_FULL_SOURCE"

That is too broad. The normative spec says unstable pagination/order that can skip records is non-authoritative unless a stable snapshot/cursor mechanism or equivalent tested contract proves coverage.

### 6.2 New immutable RunSourcePlan pins

Migration v22 adds the following fields to `run_source_plans`:

```tex
enumeration_contract_version INTEGER NOT NULL DEFAULT 1
coverage_authority           TEXT NOT NULL DEFAULT 'NO_ABSENCE_INFERENCE'
coverage_scope_key           TEXT NOT NULL DEFAULT 'full-source'
pagination_stability         TEXT NOT NULL DEFAULT 'UNKNOWN'
listing_identity_sufficient  INTEGER NOT NULL DEFAULT 0


Allowed `coverage_authority` values reuse the existing coverage enum:

```tex
AUTHORITATIVE_FULL_SOURCE
AUTHORITATIVE_DECLARED_SCOPE
NON_AUTHORITATIVE_QUERY
DETAIL_ONLY
NO_ABSENCE_INFERENCE


Allowed v1 `pagination_stability` values:

```tex
SINGLE_RESPONSE
STABLE_SNAPSHO
UNKNOWN


Rules:

- `AUTHORITATIVE_FULL_SOURCE` / `AUTHORITATIVE_DECLARED_SCOPE` require a non-`UNKNOWN` stability basis for any paginated enumeration.
- `NO_ABSENCE_INFERENCE` may still collect, cache, revalidate, normalize, score, and surface positive observations; it simply cannot age unseen jobs.
- the driver reads these pins from the RunSourcePlan. It does not recalculate authority ad hoc at coverage-open time.
- startup recovery validates that the installed pinned adapter/config still resolves to the same enumeration contract; mismatch is fail-closed diagnostic evidence, not silent substitution.

### 6.3 Adapter/binding contract resolution

Add a small host-visible `EnumerationContract` value object in the adapter contract/registry layer. It is resolved when a run plan is created and then pinned into `run_source_plans`.

The contract contains exactly:

```tex
version
coverage_authority
scope_key
pagination_stability
listing_identity_sufficien


Do not put mutable current source state inside it.

### 6.4 Current adapter mapping

The initial v22 mapping should preserve already-reviewed provider semantics while making the configurable feed conservative:

| Adapter | v22 enumeration contract |
|---|---|
| Greenhouse | full-source; `SINGLE_RESPONSE`; listing identity sufficient |
| Ashby | full-source; `SINGLE_RESPONSE`; listing identity sufficient |
| Lever | full-source; accepted run-scoped offset enumeration; stable according to the existing reviewed adapter contract/tests; listing identity sufficient |
| `json_api_feed` default | `NO_ABSENCE_INFERENCE`; `UNKNOWN`; listing identity sufficient |
| `json_api_feed` explicitly reviewed stable fixture/binding | may pin full-source + `STABLE_SNAPSHOT` |
| generic discovery | no absence authority |
| future generic crawl | not authorized by Corrective A; W3-04 remains separate |

For `json_api_feed`, add an explicit immutable binding-config declaration for stable full-source semantics. Default omission is conservative/non-authoritative. Existing arbitrary feed configs do not gain disappearance authority merely because pagination reached an empty page.

---

## 7. Migration v22

v1-v21 remain immutable. v22 is additive/corrective.

### 7.1 Cursor indexing

Drop only the v16 binding-wide compatibility index:

```sql
DROP INDEX idx_crawl_cursors_compatible_identity;


Replace it with plan-scoped uniqueness:

```sql
CREATE UNIQUE INDEX idx_crawl_cursors_plan_identity
ON crawl_cursors(
    checkpoint_run_source_plan_id,
    adapter_id,
    adapter_version,
    cursor_schema_version
)
WHERE checkpoint_run_source_plan_id IS NOT NULL;


Keep the existing binding/binding-revision indexes for diagnostics/history.

No cursor table rewrite is required.

### 7.2 RunSourcePlan contract pins

Add the five fields in §6.2 with conservative defaults.

Historical plans migrate to `NO_ABSENCE_INFERENCE` / `UNKNOWN` unless they are newly created after v22 and explicitly pin a reviewed contract. This avoids fabricating authority during migration.

### 7.3 Unfinished historical coverage

A pre-v22 **finalized** coverage row is historical evidence and is not rewritten.

A pre-v22 **unfinished** coverage generation may have been opened under the old unconditional full-source assumption. v22 must conservatively:

- set `absence_inference_allowed = 0`;
- set `coverage_authority = 'NO_ABSENCE_INFERENCE'` for the unfinished row;
- retain positive seen membership and contributing requests;
- retain the exact RunSourcePlan/binding identity;
- allow safe same-plan continuation, but never let the migrated generation create disappearance evidence.

This mirrors the conservative v18 treatment of unfinished pre-S3.8 generations.

### 7.4 Durable run-accounting effect fields

Also reserve the v22 schema foundation needed by A5 so v22 is not edited after its first accepted commit:

```tex
job_observations.resolved_job_id  TEXT REFERENCES jobs(id)
job_observations.run_effect       TEXT NULL


`run_effect` allowed values:

```tex
NEW_JOB
UPDATED_JOB
UNCHANGED_JOB
STALE_IGNORED


Add an index suitable for run counter derivation:

```tex
(run_id, run_effect, resolved_job_id)


Existing observations remain NULL; historical run counters are not rewritten.

---

## 8. Cursor implementation behavior

### 8.1 `load_cursor

Lookup becomes exact-plan:

```tex
checkpoint_run_source_plan_id = requested RunSourcePlan
+ adapter id/version
+ cursor schema version


Then validate the row's source, binding, binding revision, adapter/version/schema against the immutable RunSourcePlan. A mismatch is `CursorCompatibilityError`.

Do **not** fall back to a different plan's compatible row.

### 8.2 `save_cursor

Update only the row owned by the exact RunSourcePlan. If none exists, insert a new row. Multiple RunSourcePlans for the same binding revision therefore have separate cursor rows.

The current adapter/version/schema provenance checks remain.

### 8.3 Adapter defense in depth

Lever's existing plan-ID-in-cursor check may remain. Under v22 it becomes defense in depth rather than the primary ownership mechanism.

The generic JSON feed does not need plan ID embedded into `{page:N}` once the host storage owner is exact-plan.

---

## 9. Durable continuation request identity

Corrective A should also close the related identity mismatch discovered in the audit.

Today, after page 1 proposes page 2, the continuation request's `logical_key` contains the new cursor but its `target_identity` may still be derived from the just-completed page's URL. The adapter later loads the global cursor and can fetch a different URL than the durable request appears to name.

New rule:

> A durable continuation request must identify the same next logical target that the adapter will plan when that request is claimed.

For cursor-driven continuation:

1. after `next_cursor` is accepted, call the pure adapter planning function with that next cursor and the same immutable planning context;
2. use the resulting next URL/target as the continuation request's `target_identity`;
3. keep the serialized cursor state as `logical_key` / typed payload;
4. at claim time, if the adapter plans a target inconsistent with the durable target under the same pins, fail closed as plan/cursor incompatibility instead of silently fetching a different logical unit.

This keeps request uniqueness/provenance aligned with actual network work.

---

## 10. Revalidation and W3-05 flow

No W3-specific production branch is required after A1/A2.

Expected exact flow:

```tex
Run 1 / Plan A
  page 1 200 -> A/B + ETag -> cache representation R1 + complete page membership
  page 2 terminal
  Plan A cursor/history remains plan-scoped

restart / later Run 2 / Plan B
  Plan B has no cursor
  plan seed page 1
  prepare_revalidation finds exact R1
  sends If-None-Match
  response 304
  resolve_304 binds exact R1
  retained A/B membership copied into Plan B coverage seen union
  content revision does not incremen
  adapter proposes Plan B page-2 continuation
  terminal page finalizes complete coverage
  A/B remain ACTIVE
  zero absence transitions


If the representation is missing/pruned/incompatible, existing S3.7 behavior remains: unconditional refetch or non-authoritative outcome, never authoritative empty coverage.

---

## 11. Overlapping-run isolation

Correctness must not depend on accidental synchronous service behavior.

After v22, two compatible runs may coexist without mutable cursor interference:

```tex
Plan A cursor row -> A only
Plan B cursor row -> B only


Required invariants:

- Plan A checkpoint cannot change Plan B's cursor.
- Plan B checkpoint cannot change Plan A's pagination guard.
- each coverage generation sees only requests from its own plan;
- immutable cache representations may be shared only through exact S3.7 compatibility;
- binding/host rate protection remains intentionally shared;
- reverse completion of coverage generations remains governed by existing S3.8/S3.10 ordering.

Corrective A does not need to serialize all runs by source/binding. Isolation is preferable to relying on a global one-run-at-a-time policy for correctness.

---

## 12. Honest run accounting (`jobs_updated`)

### 12.1 Existing inconsistency

Current `_update_run_counters()` counts an existing job as updated whenever its latest source observation came from the current run. An unchanged 200 re-verification therefore increments `jobs_updated`, even when `content_revision` does not change.

Some provider tests currently accept this behavior, while Slice-1 second-run acceptance expects zero updates only because page 1 was skipped. Those expectations conflict.

### 12.2 New durable effec

During the same fenced ingestion transaction, after entity resolution/current-evidence gating/canonical projection, set the just-inserted observation's `resolved_job_id` and `run_effect`:

- `NEW_JOB` — this observation created the canonical job;
- `UPDATED_JOB` — an existing canonical job experienced a meaningful canonical/evaluation revision due to this observation;
- `UNCHANGED_JOB` — accepted current re-observation, but no meaningful canonical revision changed;
- `STALE_IGNORED` — immutable observation accepted as history but rejected from current projection by evidence ordering.

These fields are finalized before the fenced transaction commits; post-commit observations remain immutable.

### 12.3 Counter derivation

`jobs_saved` = distinct `resolved_job_id` with `NEW_JOB` in this run.

`jobs_updated` = distinct `resolved_job_id` with `UPDATED_JOB` in this run **excluding any job that also has `NEW_JOB` in the same run**, preventing double counting.

`jobs_discovered` continues to count immutable observations according to existing semantics.

An unchanged re-verification may create new immutable evidence/observation rows and advance verification timestamps while correctly reporting:

```tex
jobs_saved   = 0
jobs_updated = 0


---

## 13. Exact implementation sequence

Corrective A must be implemented as bounded checkpoints. Do not batch all changes into one opaque commit.

### A0 — architecture/contract lock

**Purpose:** freeze this amendment before behavior changes.

**Repository changes:** documentation/contract tests only; no production behavior.

**Required outputs:**

- controlling Post-S3.13 Corrective A review/design record;
- explicit test names or fixtures pinning:
  - same-plan cursor restart;
  - fresh-plan no cursor inheritance;
  - conservative generic-feed authority;
  - provider authority preservation;
  - v22 migration policy;
  - observation run-effect vocabulary.

**Gate:** review/approval before A1.

### A1 — v22 schema foundation + plan-scoped cursor ownership

**Primary files:**

- `src/jobscraper/db/schema_sql.py
- `src/jobscraper/acquisition/crawler/cursor.py
- `tests/integration/test_schema_slice3.py
- `tests/unit/test_crawler_cursor.py
- migration/promoted-boundary tests in the existing Slice-3 gate

**Behavior:**

- `LATEST_SCHEMA_VERSION -> 22`;
- drop binding-wide cursor unique index, add plan-scoped index;
- add RunSourcePlan enumeration pins;
- add observation run-effect columns/index;
- degrade unfinished pre-v22 coverage to no-absence authority;
- load/save cursors only by exact plan;
- preserve legacy NULL-provenance rows as non-loadable evidence.

**RED tests before code:** fresh Plan B must not load Plan A page-2 cursor; two plans must be able to persist two cursor rows for same binding revision; same Plan A restart must still load exact cursor+guard.

**Stop gate:** focused schema/cursor tests + full migration chain + `verify_slice3.py`. Do not start A2 on failure.

### A2 — pin and consume enumeration authority contrac

**Primary files:**

- `src/jobscraper/adapters/contract.py
- `src/jobscraper/adapters/feed_api.py
- `src/jobscraper/adapters/greenhouse.py
- `src/jobscraper/adapters/lever.py
- `src/jobscraper/adapters/ashby.py
- adapter registry/provisioning owner as required
- `src/jobscraper/runtime/runs.py
- `src/jobscraper/service/s1_routes.py
- `src/jobscraper/pipeline/driver.py
- `src/jobscraper/runtime/recovery.py

**Behavior:**

- resolve `EnumerationContract` when constructing a production RunSourcePlan;
- pin it into v22 fields;
- driver opens/resumes coverage from pinned authority/scope/listing sufficiency;
- recovery validates pinned contract against pinned adapter/config and fails closed on mismatch;
- remove false generic-feed incremental claim;
- generic feed defaults non-authoritative unless its immutable binding config explicitly declares reviewed stable full-source semantics;
- preserve accepted Greenhouse/Ashby/Lever authority.

**Stop gate:** provider E2E suites, generic feed authority tests, coverage/recovery suites, full `verify_slice3.py`.

### A3 — fresh-run revalidation correctness

**Primary files:** mainly tests; production S3.7 code should require little or no special-case change if A1/A2 are correct.

**Required integration cases:**

1. run 1 page1 A/B + ETag + terminal page2;
2. restart/service boundary;
3. fresh run 2 starts page1 rather than inherited page2;
4. exact `If-None-Match` observed;
5. 304 bound to exact cache representation;
6. A/B membership restored to run-2 coverage;
7. content revisions unchanged;
8. A/B remain ACTIVE;
9. zero absence transitions;
10. incompatible/pruned representation forces refetch/non-authoritative behavior.

Also add a **no-validator** case where a new job inserted on page 1 between runs is discovered by the second run. This is the regression the old binding-wide cursor could miss.

**Stop gate:** S3.7/S3.8 suites + W3-05-equivalent dev/integration proof + full `verify_slice3.py`.

### A4 — overlap isolation + continuation request identity

**Primary files:**

- `src/jobscraper/pipeline/driver.py
- request/cursor tests
- concurrency/overlap integration tests

**Behavior/tests:**

- continuation durable `target_identity` names the next planned target, not the just-completed page;
- actual claimed plan must agree with durable target;
- two compatible RunSourcePlans for one binding revision advance independently;
- reverse checkpoint/finish order cannot swap cursor state or coverage membership;
- rate-state sharing remains intentional and unaffected.

**Stop gate:** focused overlap/identity tests + complete Slice-3 gate.

### A5 — honest run counters and corrected acceptance expectations

**Primary files:**

- `src/jobscraper/pipeline/ingest.py
- `src/jobscraper/pipeline/canonical.py` only if needed to expose a deterministic meaningful-change resul
- `src/jobscraper/pipeline/driver.py
- provider E2E tests
- `tests/integration/test_slice1_acceptance.py

**Behavior/tests:**

- fill `resolved_job_id` + `run_effect` atomically before observation commit;
- derive run counters from durable effect rows;
- unchanged fresh 200 re-verification: new immutable evidence allowed, `jobs_updated=0`;
- actual content/canonical change: `jobs_updated=1`;
- new job: `jobs_saved=1`, not also updated;
- stale observation: neither saved nor updated;
- update old tests that equated “observed again” with “updated.”

**Stop gate:** Slice0/1/2 regressions + provider E2Es + full `verify_slice3.py`.

### Corrective A closure gate

After A0-A5:

- worktree clean except explicitly excluded local artifacts;
- v1-v21 migration digests unchanged;
- v22 migration from promoted v14/current v21 boundaries proven;
- full Slice0/1/2 regression green;
- all Slice3 focused suites green;
- repository-wide suite green;
- `pip check` green;
- `scripts/verify_slice3.py` green;
- separate architecture corrective review finds zero unresolved blocking findings.

Only then proceed to Corrective B (service acquisition coordinator).

---

## 14. Required acceptance matrix for Corrective A

| Scenario | Required result |
|---|---|
| same plan crash after page 1 | resume page 2, no page-1 replay solely due restart |
| fresh plan after completed page-2 cursor | starts seed/page 1 |
| fresh plan, new page-1 job | new job is discovered |
| fresh plan, exact ETag representation | conditional request is sent |
| accepted 304 authoritative list | retained membership enters new coverage union |
| accepted 304 | no content revision increment |
| incompatible 304 cache | no authoritative empty result; refetch/degrade |
| generic feed without stability declaration | no absence inference |
| reviewed stable generic feed | may receive pinned full-source authority |
| Greenhouse/Ashby accepted contracts | remain full-source authoritative |
| Lever foreign old-plan offset | fresh run restarts at provider enumeration start |
| two plans same binding | independent cursor/guard rows |
| unfinished pre-v22 coverage | positive evidence kept; no absence authority |
| finalized pre-v22 coverage | unchanged historical evidence |
| unchanged 200 re-observation | `jobs_updated=0` |
| meaningful existing-job change | `jobs_updated=1` |
| new job | `jobs_saved=1`; no update double count |
| stale observation | immutable evidence retained; no saved/updated current effect |

---

## 15. Tests whose existing expectation must change

The following existing expectations are architectural drift, not invariants to preserve:

1. S3.5 cross-run test that requires a fresh compatible RunSourcePlan to inherit the old page cursor.
2. Slice-1 acceptance comment/assertions that treat “page 1 is not re-fetched on a second fresh run” as idempotency.
3. Provider E2E expectations that effectively classify every existing job observed in a later run as `jobs_updated` even when content is unchanged.

The replacement invariants are:

- idempotency means no duplicate canonical identity/events and no false update count, **not** “never verify the source again”;
- fresh collection must be capable of seeing new first-page records;
- HTTP conditional revalidation is the appropriate cross-run optimization when the exact representation is reusable;
- same-run crash recovery remains no-replay where prior work is already durably accepted.

---

## 16. Explicit non-goals

Corrective A does not:

- implement the service acquisition coordinator/redrive loop (Corrective B);
- add scheduler cadence;
- add browser acquisition;
- implement W3-04 generic packaged crawl surface;
- implement W3-07 fallback-group operator surface;
- implement W3-09 native reverse-order injection surface;
- create a generic cross-run incremental framework without a real provider need;
- rewrite finalized historical coverage;
- modify migration bytes v1-v21;
- weaken SSRF, fencing, cancellation, cache compatibility, or absence barriers.

---

## 17. Relationship to Corrective B

Corrective B must come **after** A because automatic redrive must not execute ambiguous/shared pagination state.

Once A is closed, Corrective B can safely make the service own acquisition execution across:

- new run submission;
- process/service restart;
- Retry-After expiry;
- same-plan pagination continuation;
- unfinished coverage continuation;
- responsive cancellation.

Corrective A therefore supplies the trustworthy durable state that Corrective B will execute.

---

## 18. Promotion / package consequence

The frozen package based on `c1cc7b07...` remains historical evidence only. Corrective A is behavior-bearing and schema-bearing; Corrective B will also be behavior-bearing.

Do not rebuild/promote after A alone unless explicitly needed as an intermediate diagnostic package. The efficient promotion path is:

```tex
Corrective A automated closure
-> Corrective B automated closure
-> remaining W3 packaged-surface blockers
-> freeze a new candidate
-> package verification
-> W0/W1/W2 regression
-> W3 calibration/final native acceptance
-> exact-candidate CI/promotion closure


---

## 19. Architecture decision summary

The durable rule after Corrective A is:

> **Pagination progress belongs to the run plan that performed the pagination. Cache representations may cross runs when exact compatibility proves they describe the same request representation. Absence authority belongs to an immutable, explicitly pinned enumeration contract and an exact coverage generation.**

That rule aligns request identity, cursor state, cache reuse, coverage membership, restart recovery, and periodic collection without W3-specific exceptions.

---

## 20. Approval gate

The user approved this architecture on **2026-09-13**.

This approval authorizes **A0 only** as the governance/contract-lock checkpoint. It does not authorize A1–A5 production behavior changes as an undifferentiated batch. Each later checkpoint remains separately review-gated.
