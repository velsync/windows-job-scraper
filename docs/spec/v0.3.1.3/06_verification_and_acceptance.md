# Windows Job Scraper v0.3.1.3 — Verification & Acceptance Specification

**Version:** Windows Job Scraper v0.3.1.3 — Final Corrected Modular Specification Set  
**Date:** 2026-09-05  
**Status:** **NORMATIVE — implementation specification**  
**Target:** Windows 11 · local-first · single-user  
**Core stack:** Python 3.12+ · FastAPI · SQLite + FTS5 · Playwright/Chromium

> This file is one member of the canonical modular specification set. Normative ownership is defined by `00_architecture_overview_and_authority.md`. If duplicated explanatory text conflicts with the normative owner, the normative owner wins.


## VER-00. Normative ownership

This file owns proof obligations. Architecture is not considered implemented until the relevant tests/acceptance evidence pass.

# 59. Testing architecture

The majority of tests should not require manual Windows interaction.

Required layers:

- pure unit;
- SQLite/storage;
- migration;
- queue/lease;
- adapter contract;
- fixture parser;
- router/fingerprint;
- plan validation;
- executor;
- page classifier;
- recipe/navigation;
- Adapter Lab;
- normalization;
- eligibility;
- salary;
- dedup/undo;
- revalidation/repost;
- FTS;
- workflow;
- localhost security;
- auth isolation;
- diagnostics redaction;
- browser worker integration;
- packaging;
- real Windows acceptance.

---


# 60. Queue and recovery test matrix

Must cover:

- duplicate enqueue;
- atomic claim;
- concurrent claim race;
- lease renewal;
- lease expiry;
- orphan reclaim;
- attempt fencing;
- stale worker commit rejection;
- retry wait;
- retry budget exhaustion;
- `Retry-After`;
- idempotent retry;
- cancellation;
- restart recovery;
- crash after fetch before observation commit;
- crash after observation before normalization;
- browser worker death;
- HTTP→browser escalation;
- succeeded request not duplicated on resume;
- cursor checkpoint recovery;
- cursor-loop quarantine.

A queue test is incomplete if it proves reclaim but not stale-attempt fencing.

---


# 61. Adapter lifecycle test matrix

Must cover:

- manifest validation;
- permission validation;
- stable adapter ID;
- adapter-version provenance;
- source ↔ binding multiplicity;
- run version pinning;
- promotion affects new runs only;
- rollback;
- cursor compatibility/migration;
- incompatible cursor refusal;
- parser deterministic on fixtures;
- valid empty vs parser empty;
- source-native ID preservation;
- direct application URL preservation;
- no direct canonical-job mutation;
- plan cannot escape allowed host;
- plan cannot request forbidden execution class.

---


# 62. Adapter Lab / recipe tests

Must cover:

- recorder output;
- list auto-suggest;
- zero-click structured-data candidate;
- multiple locator fallback;
- required-field failure;
- locator telemetry;
- navigation recording;
- `API_JSON`;
- authorized XHR discovery;
- unrelated/non-JSON response ignored;
- snapshot iframe network inertness;
- candidate version created;
- fixture validation required;
- promotion;
- rollback;
- invalid page cannot trigger repair;
- wrong-source recipe cannot be activated for another source.

---


# 63. Localhost/security tests

Must cover:

- bind is loopback only;
- LAN access not enabled by default;
- hostile Host rejected;
- hostile Origin rejected;
- missing mutation token rejected;
- invalid mutation token rejected;
- no permissive CORS;
- cross-site form attempt changes nothing;
- recipe import rejects `file://`;
- redirect to private network blocked;
- response-size limit;
- timeout limit;
- snapshot teaching has no network;
- diagnostics omit cookies/tokens/keys;
- code-adapter permission violations denied.

---


# 64. Authenticated-session tests

Must verify:

- normal run does not mutate golden state;
- storage-state runtime works;
- persistent-profile fallback works where required;
- source A cannot receive source B cookies;
- session expiry classified as auth problem;
- auth expiry cannot trigger recipe repair;
- crash cleanup;
- secret redaction;
- promotion requires approval;
- adapter config never contains credentials.

---


# 65. Revalidation and data-lifecycle tests

Must cover:

- first seen;
- repeated seen;
- 304;
- unchanged hash;
- one COMPLETE absence-authoritative enumeration for the relevant scope → uncertain, not closed;
- repeated disappearance policy;
- direct closed evidence;
- reopen;
- salary/location/title/apply URL change;
- repost relation;
- source-native ID reuse;
- failed source run does not expire jobs;
- history retained;
- saved/applied jobs receive expected retention;
- merge undo preserves observations.

---


# 66. Product vertical-slice Windows test

A complete early vertical-slice acceptance must prove:

```text
launch Windows app
→ dashboard opens
→ create search profile
→ run one supported simple source
→ normalized jobs persist
→ Inbox displays relevant jobs
→ shortlist one
→ dismiss one
→ open exact direct application link
→ create application record
→ set/update application status
→ close app
→ restart app
→ all user-owned state remains correct
```

This is the **product MVP test**.

It is deliberately different from the adapter-architecture acceptance set.

---


# 67. Real Windows acceptance

On the target Windows 11 machine verify:

- packaged launcher starts;
- single-instance behavior;
- dashboard opens;
- local security token flow;
- SQLite opens/migrates;
- FTS5 works;
- backup/restore path works;
- browser worker launches;
- Chromium launches;
- browser process kill is recoverable;
- lease reclaim/fencing works;
- authenticated source isolation works;
- diagnostics export is redacted;
- CSV/JSON export works;
- app upgrade/migration succeeds;
- resource measurements are captured.

Manual/native Windows tests should focus on genuinely OS-specific behavior. Parser, cursor, rate policy, recipes, and most queue logic remain automated/offline.

---


# 68. Resource acceptance

Measure and record:

- idle service memory;
- HTTP-only run;
- one browser run;
- authenticated browser run;
- long scheduler session;
- browser crash/restart;
- 10k+ job FTS query;
- migration;
- snapshot pruning;
- disk growth;
- backup size/time.

Resource budgets are adjusted from evidence.

The architecture does not hardcode guessed RAM ceilings as correctness facts.

---


# 70. Adapter architecture acceptance set

This is **not** the definition of the product MVP.

After the adapter/runtime architecture matures, prove diversity with:

1. one provider-native/API source;
2. one structured/static HTTP ATS path;
3. one browser-required source;
4. one generic company-career URL successfully fingerprinted/routed.

This demonstrates:

- source registry;
- bindings;
- version pinning;
- planning/execution boundary;
- execution classes;
- provenance;
- health;
- cursor;
- parser/pagination seam.

Do not chase source count before these contracts are stable.

---


# 72. Final acceptance criteria

v0.3.1.3 is successfully implemented when a Windows user can:

1. launch the application normally;
2. open the local browser dashboard without LAN exposure;
3. create multiple search profiles;
4. collect from retained remote feeds;
5. collect from Greenhouse, Lever, and Ashby;
6. preserve discovery, canonical job, origin, and direct application URLs;
7. add a company careers URL and receive ATS/source fingerprint evidence;
8. see one company across multiple observations;
9. see multi-location jobs correctly;
10. see evidence-backed eligibility per profile;
11. see normalized salary and unknown salary distinctly;
12. see deterministic score breakdowns;
13. use FTS5 search;
14. see source observations and adapter-version provenance;
15. merge duplicates without deleting provenance;
16. undo a merge;
17. observe a run pinning a specific binding/adapter version;
18. resume an interrupted run from a compatible cursor;
19. prove a stale worker cannot commit after lease loss;
20. recover from browser-worker/Chromium termination;
21. see binding-specific health and rolled-up source health;
22. distinguish valid source-empty from parser failure;
23. see rate limiting/backoff without automated challenge bypass;
24. revalidate jobs without treating one failed/missing scrape as closed;
25. see meaningful job changes/reopened/repost evidence;
26. teach a suitable custom source via Adapter Lab;
27. use multi-locator recipe fallback;
28. promote a recipe only after validation;
29. rollback a recipe/adapter version;
30. establish a legitimate authenticated session in an isolated source context;
31. detect auth expiry without leaking credentials or triggering parser repair;
32. preserve authenticated-session isolation across sources;
33. use Inbox shortlist/dismiss/snooze/archive;
34. open the exact best application link;
35. create and update an application independently from listing status;
36. set a next action/reminder;
37. see an event when an applied job closes;
38. inspect source/run diagnostics without secrets;
39. export CSV/JSON;
40. export a redacted diagnostics bundle;
41. migrate/upgrade with a verified backup;
42. restart without losing user-owned workflow state;
43. operate within measured resource/disk budgets on the target Windows system.

---


## VER-01. Additional queue/fencing tests required by corrective review

Add mandatory tests for:

- heartbeat after lease expiry is rejected;
- expired worker cannot revive itself before reclaimer runs;
- observation/output + `SUCCEEDED` commit is fenced/atomic;
- crash after output write boundary does not duplicate durable observations;
- run cancellation prevents new claims and prevents stale terminal commit;
- durable cooldown/`Retry-After` survives restart;
- logical request uniqueness suppresses same task;
- same URL under HTTP probe vs supported browser escalation remains representable;
- HTTP and browser attempts carry a common `ExecutionPlanEnvelope` identity linking request/attempt/run-source-plan/binding revision;
- `PARTIAL` parse commits valid idempotent observations without authorizing absence inference;
- retry after `PARTIAL` does not duplicate observations/child work;
- multi-source run with some successful sources yields `PARTIAL` rather than data loss.

## VER-02. Run/config pinning tests

Must prove:

- profile edited during run → old run uses pinned revision;
- binding edited during run → old run uses pinned binding revision/config;
- recipe/NavigationPlan promotion → old run unchanged;
- adapter promotion → old run unchanged;
- retired historical binding remains resolvable for history;
- incompatible cursor is refused;
- explicit compatible cursor migration succeeds only under declared rule;
- fallback binding order is pinned.
- binding revision identity is immutable/resolvable;
- permission-profile revision used by the plan is pinned;
- current cancellation/quarantine/auth/security revocation blocks future execution despite historical plan pinning.

## VER-03. Profile workflow tests

Must prove:

```text
same job
Profile A → SHORTLISTED
Profile B → DISMISSED
```

without state leakage.

Also test:

- snooze is profile-specific;
- archive/dismiss reason is profile-specific;
- one repeated source observation does not repeatedly re-enter Inbox;
- meaningful change/reopen can re-surface according to policy.
- the same durable Inbox trigger/change revision is surfaced once rather than repeatedly after restart/refresh;
- a different meaningful revision creates a new Inbox event.

## VER-04. Provenance/presence tests

Must prove:

- `JobObservation` is immutable;
- repeated observations update `job_sources`, not prior observations;
- original discovery/source URL is preserved after origin resolution;
- selected canonical provenance can change without deleting older provenance;
- aggregator missing + employer ATS active → canonical job remains active;
- direct trusted close evidence updates canonical status according to policy;
- source-native ID reuse guard splits/reviews incompatible reuse.
- `job_sources` is created/updated only after canonical job identity is resolved;
- complete absence-authoritative enumeration may create absence evidence;
- `PARTIAL`, budget-exhausted, query-non-authoritative, cancelled, failed, challenged or auth-expired enumeration cannot create absence evidence;
- absence transition records its authorizing enumeration-coverage record.
- declared-scope absence inference uses explicit scope membership rather than inferred job-field guesses;
- repaired binding revision health does not blindly inherit stale parser-broken state from prior revision.

## VER-05. Browser/network security tests

Must include:

- page attempts XHR/fetch to `127.0.0.1` → blocked;
- page attempts RFC1918/link-local request → blocked;
- top-level redirect to forbidden private host → blocked;
- `file:`/`javascript:` browser navigation rejected;
- additional authenticated API host requires approval;
- service-worker/subrequest policy is exercised where supported.
- browser downloads, clipboard, geolocation, camera/microphone, notifications, external protocols and uncontrolled popups are denied by default;
- explicitly permitted browser capability remains binding/policy-scoped and cannot escape host controls.

## VER-06. Dashboard content/XSS tests

Inject malicious source fields containing:

- `<script>`;
- event-handler attributes;
- `javascript:` URLs;
- malicious Markdown/HTML;
- SVG/script payloads;
- hostile diagnostic/event strings.

Prove they cannot execute in application origin or perform a mutation.

## VER-07. Acquisition side-effect tests

A malicious/buggy adapter asking to:

- submit application;
- send message;
- change account setting;
- perform unknown high-impact form action

must be rejected by host policy.

A benign filter/navigation form remains supported.

## VER-08. Export tests

Must prove:

- export beyond 200 rows is complete;
- no silent truncation;
- explicit user limit is honored;
- row count is reported;
- direct application/source URLs survive;
- UTF-8 round trip;
- CSV formula-prefix content is neutralized according to policy;
- failed export is clearly incomplete rather than masquerading as success.

## VER-09. Build/browser reproducibility tests

For each release:

- install/build from lock;
- verify Python/Playwright/browser compatibility;
- browser Doctor failure is actionable when runtime missing/mismatched;
- packaged app starts without development environment;
- migrations/backup restore from prior released slice;
- backup of an open WAL database uses a SQLite-consistent mechanism and restores cleanly;
- older binary is refused against an incompatible newer schema unless backward compatibility is declared/tested;
- pre-upgrade backup + compatible binary restores successfully;
- build identifier maps to acceptance evidence.

## VER-10. Acceptance source diversity vs product MVP

Keep these separate.

### Product MVP

Proves:

```text
launcher → one source → profile → provenance → canonical job
→ Inbox → disposition → direct apply → application tracking → restart
```

### Adapter architecture diversity

Later proves:

1. provider-native/API;
2. structured/static HTTP ATS;
3. browser-required source;
4. generic careers URL fingerprint/routing.

The second set must not delay the first user-visible product loop.

## VER-11. Final acceptance additions

In addition to the inherited acceptance criteria, the final product must prove:

44. per-profile disposition does not leak across profiles;
45. expired lease cannot be revived by heartbeat;
46. immutable run/binding/config snapshots survive later edits;
47. immutable observations and mutable source presence remain separate;
48. conflicting multi-source availability cannot falsely close an active employer-origin listing;
49. browser execution cannot access local/private destinations outside explicit policy;
50. hostile scraped content cannot execute in the dashboard;
51. acquisition automation cannot submit an application/business transaction;
52. exports never silently truncate;
53. dependency/browser build metadata is reproducible and Doctor-verifiable.
54. DST/timezone schedule behavior is deterministic across ambiguous/nonexistent local times and sleep/wake catch-up.
55. launcher port ownership/allocation is collision-safe and does not rely on an unsafe probe-close-bind race.
56. `UNEXPECTED_REDIRECT` classification does not misclassify an ordinary valid redirect as a page-validity class.
57. binding-level quarantine and auth/permission revocation override pinned plans at the next bounded policy/ownership checkpoint.
58. enumeration coverage distinguishes complete authoritative scope from partial/query-limited scope.

## VER-13. v0.3.1.3 cross-subsystem corrective acceptance

The following are release-gating because they prove interactions that single-subsystem tests can miss.

### First exposed acquisition slice

- hostile source description/title/link renders as escaped/sanitized inert content; no source HTML executes in application origin;
- invalid/login/challenge content cannot produce normal observations or valid empty coverage;
- minimal SSRF/destination policy is active before arbitrary/source-controlled URLs are fetched;
- accepted observation + terminal request commit includes a durable local-processing obligation;
- cancellation/duplicate delivery cannot orphan or duplicate accepted evidence.

### Enumeration coverage/finalization

- multi-page list union (`A` on page 1, `B` on page 2) finalizes one generation containing both identities;
- terminal listing finalization with details still pending behaves according to the binding's declared listing-identity sufficiency;
- cancellation/failure/budget exhaustion before finalization yields zero absence transitions;
- duplicate finalization/application of one generation yields zero additional absence transitions;
- overlapping generations completing in reverse order cannot regress newer presence;
- unstable pagination/order is denied absence authority unless the tested binding proves stable coverage.

### Run/fallback outcomes and recovery

- primary failure + successful fallback → logical source-plan group `SATISFIED` and unused later fallbacks `SKIPPED_NOT_NEEDED`;
- primary success with unused fallback is not `PARTIAL`;
- valid complete zero-job result is success;
- usable but budget-exhausted/accepted partial result remains visibly incomplete;
- restart between primary failure and fallback activation preserves deterministic rank/order;
- a run cannot terminalize while relevant dynamic child/continuation/local-processing work remains pending;
- cancel/restart immediately after observation commit eventually materializes it exactly once or exposes the explicit resumable processing state without new network I/O.

### Temporal/current projection ordering

- two distinct valid requests for the same source identity complete in reverse chronological order; current state reflects the newer evidence while both observations remain historical;
- trusted close followed by newer reopen and the reverse ordering both resolve by evidence precedence, not worker completion order;
- profile edit/rescore cannot let an old eligibility/score row overwrite the new revision;
- concurrent same-identity creation uses conditional uniqueness/entity resolution and creates no duplicate current entity.

### Revalidation/cache

- list A/B then `304` with retained compatible membership keeps A/B seen;
- same scenario after restart behaves identically;
- pruned/missing/incompatible cached representation causes unconditional refetch or non-authoritative outcome, never authoritative empty coverage;
- verification-only `304` does not fabricate a content revision.

### Merge/Inbox/user-state

- merge jobs with conflicting dispositions, two applications, notes and Inbox events; edit after merge; undo; restart; every object retains deterministic ownership and no event resurfaces solely due to identity change;
- reused origin provider/board/job ID fails automatic merge when temporal/entity/content guard detects reuse;
- job change during unexpired snooze stays suppressed; expiry emits at most one stable occurrence under policy;
- old job becoming eligible after profile revision follows explicit first-eligible semantics;
- two dashboard tabs cannot silently overwrite stale disposition/application edits.

### Localhost/session security

- normal launcher bootstrap succeeds without exposing the install secret in URL/logs;
- second launch attaches to the correct service instance;
- stale runtime descriptor/old port occupied by another process fails service-instance validation;
- absent/expired/revoked session fails private reads/SSE/exports and mutations; minimal liveness exposes no private data;
- hostile Host/Origin and CSRF attempts fail;
- token rotation/service restart invalidates session authority according to contract.

### Browser/network side-effect security

- permitted hostname resolving/redirecting to loopback/private/link-local is denied at connection time for IPv4 and IPv6;
- unsupported service-worker/WebSocket/proxy containment returns denied/unsupported, not silent bypass;
- containment is installed before page/connection creation and covers subframes/popups/alternate enabled channels;
- a permitted-looking click that causes application submission/account mutation is denied;
- direct HTTP request to the same forbidden operation is denied; explicitly supported read-only search/filter POST still works.

### Storage/restore/Windows packaging

- migration gate includes `foreign_key_check`, application consistency and effective PRAGMA verification;
- release records actual `journal_mode`, `foreign_keys` and `synchronous` values;
- backup while pruning cannot produce a manifest referring to a deleted required artifact;
- clean restore with original app stopped validates DB + required artifact hashes/references before activation;
- missing external user document is shown as missing, not silently treated as restored;
- clean packaged Windows install resolves pinned IANA timezone data and spring-gap/autumn-fold behavior exactly once;
- material wall-clock anomaly invalidates/reconciles lease ownership without reviving expired ownership.

## VER-14. Versioned contract fixture gate

Fixtures/schema tests must exist before cross-component implementation for `PlanningContext`, `ValidatedResultEnvelope`, `ParseContext`, `ParseOutcome`, discovered tasks and coverage proposals. They cover `HEALTH`, `DISCOVER`, `ENUMERATE`, `CRAWL`, `DETAIL`, `SMOKE`, successful empty, closed/missing, partial, terminal-no-work and host-native non-network dispatch.

## VER-15. Release resource acceptance record

Each packaged release records a repeatable resource test against a named Windows target/build and fixture corpus. The acceptance record includes at minimum idle service memory, total browser process-tree peak memory, p95 local search latency, peak temporary disk, WAL growth during the scenario, cancellation responsiveness, total request/redirect/subrequest/retry bytes and browser cleanup. Release-specific ceilings are explicitly approved from measured evidence; absence of a recorded procedure/result is not a resource PASS.

