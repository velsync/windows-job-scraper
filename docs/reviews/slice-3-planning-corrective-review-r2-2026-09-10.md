# Slice 3 Planning Corrective Review R2 — v0.3.1.3

Date: 2026-09-10  
Repository: `velsync/windows-job-scraper`  
Authoritative implementation lineage: `arena/01a0882c-windows-job-scraper`  
Planning base originally audited: `c9cd5e6b426fc40874f9421c05de9f58beca3c03`  
Documents reviewed:

- `docs/plans/slice-3-worker-implementation-plan-v0313.md`
- `docs/plans/slice-3-windows-acceptance-strategy-v0313.md`
- `docs/reviews/slice-3-planning-audit-2026-09-10.md`

Status: **PASS WITH REQUIRED CORRECTIONS — IMPLEMENTATION-READY ONLY WITH THIS REVIEW**

This is a planning/governance corrective review only. No production code is authorized or changed by this record.

This R2 review **supersedes `docs/reviews/slice-3-planning-audit-2026-09-10.md` as the controlling Slice-3 planning corrective record**. The worker plan and Windows acceptance strategy remain the base plans; where either conflicts with this review, this review controls. The original planning audit remains historical evidence of the first pass and is not rewritten.

The 14-package decomposition and overall sequencing remain valid. The corrections below prevent ambiguous migration ownership, accidental future-slice scope drift, stranded accepted observations, under-pinned coverage authority, and incomplete native acceptance.

## 1. Audit basis

The corrective pass rechecked ROAD-04 item by item against the owning v0.3.1.3 contracts, especially:

- `01_product_and_workflow.md` for canonical/user-state boundaries;
- `02_acquisition_adapters_and_crawler.md` §§11, 14, 19–21 and ACQ-02/03/04/07/09;
- `03_durable_runtime_and_persistence.md` §§15/16/18/40/50 and RUN-01/02/04–10/11/13/14/14A/18–21;
- `04_security_and_authentication.md` for current authorization and outbound safety;
- `05_windows_packaging_and_operations.md` for package/process/Doctor constraints;
- `06_verification_and_acceptance.md` VER-01/02/04/11/13/14/15;
- `07_implementation_roadmap.md` ROAD-04 and the authoritative Slice 4–8 deferrals.

The promoted code scaffold was also checked where planning correctness depends on its actual shape. Two relevant current facts are:

1. `runtime/claims.py` currently applies source/binding enabled+administrative checks to the general claim path, while cancellation has an acquisition-only exception. Slice 3 must not let that broad scaffold silently strand already-accepted host-native obligations.
2. `pipeline/coverage.py::open_coverage()` currently inserts `source_plan_group_id = NULL` and does not populate `binding_revision_id`, even though the durable coverage schema already has those columns. Slice 3 must bind new authoritative coverage to the immutable run plan/revision rather than trust loose caller identifiers.

## 2. Finding R2-F1 — migration ownership collision across packages

**Severity: BLOCKING planning defect if left uncorrected.**

The base worker plan assigns or conditionally assigns the same migration versions to multiple later packages (`v16` across S3.5/S3.7 and `v17` across S3.8–S3.11). That conflicts with the repository's append-only migration discipline: once one package commits and pins a migration step, a later package must not reopen/edit the same migration bytes.

There is also an internal inconsistency in S3.0: it says v15 owns only S3.1–S3.4 runtime-foundation gaps, but then suggests placing a possible S3.9 fallback-group state object into v15.

### Corrective rule

- **S3.0 owns v15** for runtime-foundation storage actually required by S3.1–S3.4.
- Do **not** pre-create S3.9 fallback/group schema in v15 merely because it might later be useful.
- After v15, any S3 package that proves it needs a schema change receives the **next unused sequential migration version at the start of that package**.
- A package that needs no schema change creates no empty placeholder migration.
- Once a migration is committed/pinned by a completed package, no later package may edit it.
- Therefore references in the base plan such as “S3.7 owns v16” or “S3.8–S3.11 use v17” are planning labels only and are superseded by this next-unused-version rule.
- `test_schema_slice3.py` must pin every completed S3 migration byte-for-byte as packages advance.

This preserves forward-only history and keeps one package from invalidating another package's migration acceptance.

## 3. Finding R2-F2 — future-slice numbering does not match the normative roadmap

**Severity: HIGH.**

The base worker plan/audit incorrectly refer to future normative “Slice 9”, “Slice 10”, and “Slice 11”. v0.3.1.3 defines nine slices total, numbered **Slice 0 through Slice 8**.

### Correct normative deferral map

- **Slice 4 — understanding, scoring and dedup:** fact extraction, evidence-backed eligibility, salary/FX, explainable scoring, reversible dedup, source-ID reuse guard, merge ledger/dependent-state undo contract, change/repost logic, profile feedback.
- **Slice 5 — Adapter Lab and repairability:** recipes, fixtures, teaching/repair/promotion/rollback, binding-health dimensions/UI.
- **Slice 6 — browser and authenticated sources:** browser supervision hardening, NavigationPlan execution, browser containment, authenticated acquisition/session isolation.
- **Slice 7 — workflow depth and operations:** reminders/notifications, contacts, documents/application events, complete exports, diagnostics bundle, scheduler refinements, company watch/blocklist, analytics, backup/restore UX.
- **Slice 8 — optional expansion:** more ATSs/bulk import and optional integrations such as JobSpy/Crawlee/Scrapling/embeddings/LLM/alternate browser experiments.

Any Slice-3 text that says scheduler is Slice 8, merge/undo is Slice 9, exports/backup are Slice 10, or optional integrations are Slice 11 is superseded by the mapping above. No Slice 9–11 implementation authority exists in v0.3.1.3.

## 4. Finding R2-F3 — host-native claim policy must not silently strand accepted evidence

**Severity: HIGH.**

RUN §18 explicitly requires already-accepted observations/evidence to retain their durable host-native processing obligations across cancellation. The current generic claim scaffold checks source/binding enabled/admin state broadly. If Slice 3 simply strengthens that predicate without separating acquisition from local processing, a later source/binding disable/quarantine could leave accepted observations permanently unclaimable while the run appears stuck.

### Corrective rule for S3.1/S3.2/S3.11

Define claim/authorization policy by task class:

- **Acquisition requests** require current run/source/binding/auth/permission/security authority and must stop after cancellation or applicable current revocation.
- **Host-native requests** (`NORMALIZE`, `RECONCILE`, `ENRICH`, `ELIGIBILITY`, `SCORE`, `EXPORT` where applicable) never gain source-network authority from being claimable.
- Cancellation MUST NOT silently cancel already-created host-native obligations for accepted observations.
- Source/binding disable/quarantine or permission/auth revocation MUST NOT accidentally strand a host-native obligation through a generic source-network predicate. If policy intentionally prevents local processing of already-accepted evidence, the obligation/run must enter an explicit visible resumable/blocked processing state with a durable reason; it may not remain silently pending forever.
- Claim, pre-dispatch and commit authorization helpers must therefore distinguish **network authority** from **local evidence-processing authority**.

Required tests must include changing source/binding administrative state after an observation has been accepted and proving that processing either drains safely without I/O or becomes explicitly visible/resumable—never silently disappears and never performs new network access.

## 5. Finding R2-F4 — authoritative coverage must pin group and binding revision from RunSourcePlan

**Severity: HIGH.**

The normative `enumeration_coverage` shape contains `source_plan_group_id` and `binding_revision_id`. The current scaffold leaves them unset for newly opened coverage. That is insufficient for final ROAD-04 authority because absence decisions must be reproducible against the exact immutable plan/binding revision.

### Corrective rule for S3.8

- Opening a coverage generation must derive authoritative identity from `run_source_plan_id`; do not trust independently supplied `source_id`/`binding_id` when they can be derived from the immutable plan.
- Persist the exact `source_plan_group_id`, `source_id`, `binding_id`, and `binding_revision_id` belonging to that run plan.
- Reject caller-supplied identity that conflicts with the referenced RunSourcePlan.
- Cache/revalidation membership reused into coverage must be compatible with the same binding revision/request-variant/parser contract required by §40.
- An absence transition must record the exact coverage generation that authorized it.

Required tests: mismatched source/binding inputs cannot open authoritative coverage; binding-revision mismatch cannot authorize 304 membership reuse or absence; resumed coverage retains the same pinned plan/group/revision identity.

## 6. Finding R2-F5 — page-validity ownership must be folded into S3.5

**Severity: HIGH; confirms and incorporates prior audit C1.**

ROAD-04 explicitly includes page validity. S3.5 therefore owns crawler integration with the existing `acquisition/pagevalidity.py` classifier.

Required behavior:

- every generic crawler result is classified before normal adapter parsing;
- only `VALID_LIST`, `VALID_JOB`, and a specifically recognized `EMPTY` path enter normal parse semantics;
- `LOGIN_REQUIRED`, `AUTH_EXPIRED`, `RATE_LIMITED`, `CHALLENGE_PAGE`, `JS_SHELL`, `NOT_FOUND`, `JOB_CLOSED`, `UNEXPECTED_REDIRECT`, `UNEXPECTED_CONTENT`, and `UNKNOWN` remain typed validity/failure/closure evidence and cannot fabricate normal observations or authoritative empty coverage;
- ordinary security-valid redirects remain redirect metadata and are not automatically `UNEXPECTED_REDIRECT`;
- invalid/challenge/auth content cannot create or restore absence authority.

S3.13 must regression-test this integration.

## 7. Finding R2-F6 — configured egress must participate in capacity/rate accounting without adding proxy product scope

**Severity: MEDIUM; confirms and incorporates prior audit C2.**

Where an already host-approved egress identity exists, S3.3/S3.4 capacity/rate keys must be able to include it. This is policy/accounting plumbing only.

- Direct connection remains default.
- Imported/source-controlled data cannot manufacture an egress identity or widen permission.
- No proxy setup, proxy harvesting, rotation, anti-block evasion, or optional-integration UX is authorized in Slice 3.

## 8. Finding R2-F7 — cursor-loop containment must be explicit and restart-safe

**Severity: HIGH; confirms and incorporates prior audit C3.**

S3.5 must convert proven cursor/pagination traps into typed bounded terminal handling:

- stop further work for the affected run-source-plan/binding revision;
- record `PAGINATION_LOOP`/equivalent diagnostic evidence;
- never convert the generation to COMPLETE or grant absence authority;
- do not automatically administratively quarantine the entire source unless separate current policy explicitly warrants it;
- restart must not resume the same proven-bad cursor into another infinite loop.

S3.12 must include recovery proof for this condition.

## 9. Finding R2-F8 — concurrent same-identity creation must use conditional uniqueness/re-resolution

**Severity: HIGH; confirms and incorporates prior audit C4.**

S3.10 must explicitly test the VER-13 race where two valid requests resolve the same current identity concurrently:

- exactly one current canonical/source-presence identity is created;
- both immutable observations remain;
- the losing creation path re-reads/re-resolves/attaches instead of creating a duplicate;
- genuine source-native ID reuse still invokes the reuse guard rather than being forced into a false merge.

This is separate from stale-update ordering; both are required.

## 10. Finding R2-F9 — robots-aware generic crawling needs an explicit owner

**Severity: MEDIUM/HIGH.**

ACQ §20 requires public crawl modes to support visible robots-aware behavior and record the decision. The base S3.5 text mentions recording a robots decision but does not explicitly own retrieval/evaluation behavior.

### Corrective rule for S3.5

- implement or integrate deterministic robots-policy evaluation for generic public crawling;
- respect the source's configured robots mode/policy;
- record the decision in acquisition evidence/ResultEnvelope as applicable;
- any `robots.txt` network retrieval must itself use the ordinary host-owned durable acquisition path and budgets—never hidden pre-queue/pre-provenance network I/O;
- robots denial/unknown handling cannot be treated as a valid empty enumeration and cannot authorize absence;
- fixture tests cover allow, deny, malformed/unavailable policy, scope interaction and restart-safe caching if caching is introduced.

This does not authorize bypassing robots or source restrictions.

## 11. Finding R2-F10 — packaged loopback fixtures must not weaken SSRF protection

**Severity: HIGH acceptance-harness requirement.**

The W3 strategy correctly prefers deterministic loopback fixture servers, but production SSRF policy rejects loopback/private destinations by default. The harness therefore needs the same narrow host-owned test authority discipline already used by earlier packaged acquisition acceptance; otherwise an implementer could be tempted to relax the real destination boundary merely to make native tests reachable.

### Corrective rule for N3-A/W3 harnesses

- use only a narrowly scoped, harness-controlled internal/test grant already supported by host policy or an equivalently explicit test-only authority;
- the source/adapter/crawler payload cannot create this grant;
- the grant is bound to the fixture endpoint/operation and unavailable to ordinary imported source configuration;
- add a negative companion assertion showing unrelated loopback/private destinations remain denied;
- never disable global SSRF checks for native acceptance.

## 12. Finding R2-F11 — wall-clock anomaly is release-gating and needs explicit acceptance evidence

**Severity: HIGH.**

S3.1 mentions forward/backward clock-anomaly tests, but the base native strategy does not explicitly carry the VER-13 wall-clock requirement into a named gate.

### Corrective rule

- S3.1 automated tests must inject both material forward and backward anomalies around active leases and prove new claims stop, the service ownership epoch changes/invalidates in-memory ownership, and recovery never revives an expired lease.
- N3-A should include a controlled Windows test of the same behavior if the implementation exposes a safe deterministic clock/test seam.
- If changing the real Windows system clock is required, do **not** do so merely for acceptance; instead use a packaged/native test seam or Windows integration harness that proves the production anomaly detector without mutating the user's machine clock.
- `scripts/verify_slice3.py` and final evidence must contain an explicit PASS for this requirement even if W3-01 incorporates it rather than assigning a separate W3 number.

## 13. Finding R2-F12 — final resource acceptance must enumerate the full VER-15 record

**Severity: HIGH release-gate omission.**

W3-10 currently says “relevant resource metrics,” but VER-15 defines a minimum record for every packaged release. The final Slice-3 acceptance evidence must therefore record, against the named candidate/build and fixture corpus:

- idle service memory;
- total browser process-tree peak memory (foundation/browser smoke may be used; Slice 3 does not expand browser acquisition);
- p95 local search latency;
- peak temporary disk usage;
- WAL growth during the scenario;
- cancellation responsiveness;
- total request/redirect/subrequest/retry bytes;
- browser cleanup/process-tree result;
- explicit release-specific ceilings/approval derived from measured evidence.

Absence of a required measurement is not a resource PASS. This requirement extends W3-10; the W3 count may remain 10 if W3-10 reports every required sub-measurement distinctly.

## 14. Finding R2-F13 — S3.0 future-slice boundary test wording is too broad

**Severity: MEDIUM.**

The base S3.0 text says the contract test should “forbid new Slice-4+ modules/surfaces.” Earlier slices already contain minimal final-architecture eligibility/scoring/application/browser-foundation modules, so a literal module-level ban would be wrong.

### Corrective rule

The test must forbid **new future-slice capability expansion**, not the existence or regression maintenance of already accepted minimal modules. It may assert, for example:

- no new recipe/Adapter-Lab teaching capability;
- no new authenticated/browser acquisition path;
- no Slice-4 dedup/fact/salary/change-repost expansion;
- no Slice-7 scheduler/export/contacts/backup-UX expansion;
- no Slice-8 optional integrations;

while allowing necessary compatibility changes/regression tests to already accepted surfaces.

## 15. Clarification R2-C1 — split S3.8 and S3.10 ownership without duplicate state machines

S3.8 and S3.10 touch the same presence lifecycle and must not create competing implementations.

- **S3.8 owns coverage authority:** coverage identity, contributing requests, seen union, explicit scope membership, terminal barrier, generation ordering, and exactly-once application of authorized absence evidence to the affected source-presence scope.
- **S3.10 owns general current-projection resolution:** ordering among independent active/closure/absence evidence, source-quality precedence, canonical multi-source availability and current canonical presentation/status.
- S3.10 must extend the S3.8 per-source absence semantics; it must not replace them with a second disappearance mechanism.

## 16. Clarification R2-C2 — binding revision/fallback pinning must be directly regression-tested

ROAD-04 explicitly names binding revisions + fallback-rank pinning. S3.2/S3.9/S3.12 must carry direct tests proving:

- editing a binding after run creation does not mutate the existing RunSourcePlan;
- old run execution/envelope uses the pinned binding revision/config while current revocation can still deny future execution;
- fallback rank/order is immutable for the run;
- retired historical binding revisions remain resolvable for history/resume;
- incompatible cursor/implementation substitution is refused unless an explicit compatibility rule exists.

These are not satisfied merely by the existence of `binding_revision_id` columns.

## 17. Revised package interpretation

The package count and order remain:

```text
S3.0 → S3.1 → S3.2 → S3.3 → S3.4 → N3-A
→ S3.5 → S3.6 → S3.7 → S3.8 → S3.9
→ S3.10 → S3.11 → S3.12 → S3.13 → W3
```

No new package is required. Apply the corrections to these owners:

| Package | Required R2 additions |
|---|---|
| S3.0 | next-unused migration discipline; no premature S3.9 schema; future-slice boundary wording correction |
| S3.1 | task-class claim semantics; service/clock epoch + anomaly tests |
| S3.2 | network-vs-local live authorization split; direct binding-revision pin tests |
| S3.3 | configured egress capacity key |
| S3.4 | configured egress rate key; local-vs-source failure separation remains |
| S3.5 | page-validity integration; robots-aware crawl; cursor-loop containment |
| S3.6 | unchanged except ordinary migration discipline |
| S3.7 | next-unused migration version; exact binding/request/cache compatibility |
| S3.8 | next-unused migration version; derive coverage identity from RunSourcePlan; explicit group/revision pins; coverage-owner boundary |
| S3.9 | next-unused migration version only if group-state storage is actually required; fallback pin regression |
| S3.10 | next-unused migration version if required; concurrent identity race; general temporal/current projection owner |
| S3.11 | next-unused migration version if required; host-native obligations never acquire network authority |
| S3.12 | restart-safe loop containment; pinned historical implementation resume; idempotent recovery |
| S3.13 | consolidated corrective regression; explicit wall-clock PASS; safe loopback harness; full VER-15 resource record |

## 18. Revised Windows/native acceptance interpretation

The two native stages remain correct:

1. **N3-A after S3.4** — still 6 named checks, but N3-A-01..06 must also preserve explicit evidence for the service/clock epoch behavior; add the controlled wall-clock anomaly proof where safely testable on Windows.
2. **W3 after S3.13** — still 10 named checks. W3-01 carries service-epoch/clock-anomaly evidence; W3 fixture networking uses narrow harness-only loopback authority plus a negative SSRF assertion; W3-10 records the complete VER-15 metric set.

The overall recommended final native count may remain **41/41** = W0 18 + W1 7 + W2 6 + W3 10, provided every required sub-proof above is individually recorded under its owning W3 check. Do not reduce proof merely to preserve the count.

## 19. Final corrective verdict

After applying this R2 record as controlling planning authority:

- **Slice-3 package count remains 14 (S3.0–S3.13).**
- **No package reordering is required.**
- **N3-A remains after S3.4.**
- **Final W3 remains after S3.13.**
- **S3.0 remains the recommended first implementation package.**
- **No v0.3.1.3 architecture contradiction was found.**
- **No production code was changed or authorized by this corrective review.**

The plan is implementation-ready only when a worker reads the base worker plan, Windows acceptance strategy, and this R2 corrective review together. A worker must not implement from the base plan alone where this R2 record changes or narrows the instruction.