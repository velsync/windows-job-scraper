# Corrective Review — Post-S3.13 Corrective A Design

**Date:** 2026-09-13
**Reviewed artifact:** `POST-S3.13-CORRECTIVE-A-ENUMERATION-LIFECYCLE-AUTHORITY-DESIGN-2026-09-13.md
**Review type:** architecture/design corrective pass before implementation
**Result:** **ACCEPTED — USER APPROVED 2026-09-13; A0 ONLY AUTHORIZED FOR EXECUTION**

## 1. Review method

The design was checked against the live repository owners and current authority, including:

- `AGENTS.md`;
- v0.3.1.3 durable runtime/persistence §16/§40;
- Slice-3 worker plan S3.5/S3.7/S3.8/S3.12/S3.13;
- controlling Slice-3 planning corrective R2;
- `acquisition/crawler/cursor.py`;
- schema migrations v16-v21;
- `pipeline/coverage.py`;
- `pipeline/driver.py`;
- `acquisition/crawler/revalidation.py` behavior as consumed by the driver;
- `runtime/runs.py`;
- `runtime/recovery.py`;
- service `/api/runs` execution path;
- Greenhouse/Lever/Ashby/feed adapter contracts;
- Slice-1, S3.5, S3.7, S3.8, S3.12, S3.13 tests;
- packaged W3 calibration evidence, especially W3-03/W3-05.

The failed first W3-05 corrective was treated as evidence that a narrow fix was insufficient, not as authority to weaken existing behavior.

## 2. Findings

### CR-A-1 — plan-scoped cursor ownership is necessary and sufficient for the core lifecycle contradiction

**Disposition: ACCEPTED.**

Making `checkpoint_run_source_plan_id` the lookup/uniqueness owner aligns ordinary pagination state with requests and coverage. It preserves same-plan restart and removes fresh-run inheritance without requiring a new cursor table.

### CR-A-2 — do not migrate all adapters to non-authoritative coverage

**Disposition: CORRECTED IN DESIGN.**

The first audit phrasing risked sounding broader than warranted. Existing Greenhouse/Ashby/Lever tests intentionally define provider enumeration authority. The design preserves those accepted contracts and makes only unproven/configurable enumeration conservative.

### CR-A-3 — cache representation must remain cross-run

**Disposition: ACCEPTED.**

Plan-scoping cache rows would destroy the intended S3.7 304 model. The design correctly separates mutable pagination continuation from immutable exact representation reuse.

### CR-A-4 — no separate incremental checkpoint table ye

**Disposition: ACCEPTED (YAGNI).**

The design reserves a distinct future abstraction but does not implement one without a real provider delta protocol. Removing the generic feed's false `incremental` claim is sufficient for this corrective.

### CR-A-5 — v22 must reserve all schema fields before subpackages use them

**Disposition: CORRECTED IN DESIGN.**

Because migrations are treated as append-only once accepted, A1 owns the complete v22 schema foundation, including run-accounting effect fields needed by A5. Later A2-A5 may use those fields but must not edit the accepted v22 migration bytes.

### CR-A-6 — unfinished pre-v22 authoritative coverage needs explicit migration handling

**Disposition: CORRECTED IN DESIGN.**

Simply defaulting old RunSourcePlans to `NO_ABSENCE_INFERENCE` would conflict with an unfinished coverage row previously opened as `AUTHORITATIVE_FULL_SOURCE`. The design now explicitly degrades/relabels only unfinished pre-v22 coverage to `NO_ABSENCE_INFERENCE`, preserving positive evidence. Finalized historical coverage is untouched.

### CR-A-7 — continuation request identity was part of the same defect family

**Disposition: INCLUDED IN A4.**

A continuation request should durably identify the next planned target. The design requires next-plan derivation before enqueue and fail-closed target agreement on claim, preventing request provenance from naming page N while network I/O actually performs page N+1.

### CR-A-8 — overlap safety should come from isolation, not accidental serialization

**Disposition: ACCEPTED.**

The design does not introduce a global same-binding run mutex merely to hide shared-state bugs. Plan-scoped cursor rows allow legitimate future concurrency while existing rate protection remains binding/host shared intentionally.

### CR-A-9 — `jobs_updated` needs durable semantics

**Disposition: CORRECTED IN DESIGN.**

The design does not merely modify a counter query. It reserves durable observation effect fields so run accounting remains reconstructible from persisted evidence. A new job is excluded from `jobs_updated` even if later observations in the same run change it.

### CR-A-10 — Corrective A must precede the acquisition coordinator

**Disposition: ACCEPTED.**

Corrective B will cause recovered state to execute automatically. Doing B first would increase the blast radius of the current ambiguous cursor lifetime. A-first is the safe dependency order.

## 3. Contradiction scan

No unresolved contradiction remains in the proposed design between:

- same-plan restart and fresh-run periodic collection;
- cache reuse and cursor ownership;
- provider full-source authority and generic-feed conservatism;
- historical finalized coverage and migrated unfinished coverage;
- concurrent runs and shared source-protection state;
- immutable observations and durable run accounting.

## 4. Scope scan

The design remains focused on Corrective A. It explicitly excludes the acquisition coordinator and W3-04/07/09 packaged surfaces. It does not introduce scheduler, browser acquisition, or a speculative incremental framework.

## 5. Ambiguity scan

The following decisions are explicit rather than left as TODOs:

- ordinary crawl cursor lifetime = exact RunSourcePlan;
- cache lifetime = exact representation compatibility, cross-run allowed;
- new generic feed authority default = no absence inference;
- provider authority = preserve accepted reviewed contracts;
- pre-v22 unfinished coverage = conservative no-absence migration;
- finalized coverage = immutable;
- true incremental state = future separate abstraction;
- unchanged re-observation = not a job update.

## 6. Implementation-risk notes

Highest-risk implementation points are:

1. v22 migration and migration-test preservation of v1-v21 bytes;
2. production run planner pinning an enumeration contract for every RunSourcePlan;
3. recovery comparing the pinned contract without consulting mutable current binding state;
4. updating historical tests without weakening same-plan restart or provider absence correctness;
5. setting observation run effects atomically and idempotently.

These are why A0-A5 must be separate review checkpoints.

## 7. Final review decision

**ACCEPTED.**

The user approved the design on **2026-09-13**. A0 may execute as a documentation/contract-lock checkpoint. A1–A5 remain separately gated and must be implemented/reviewed one bounded checkpoint at a time.
