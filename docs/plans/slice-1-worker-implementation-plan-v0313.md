# Slice 1 Worker Implementation Plan — v0.3.1.3

Status: PLANNED — decomposition of ROAD-02 per ROAD-13 step 5.
Authority: `docs/spec/v0.3.1.3/` (normative set), especially
`01_product_and_workflow.md`, `02_acquisition_adapters_and_crawler.md`,
`03_durable_runtime_and_persistence.md`, `04_security_and_authentication.md`.
Precondition: Slice 0 automated gate PASS + native harness ready
(`docs/reviews/slice-0-corrective-review-and-status-2026-09-07.md`).

Goal: the smallest **non-disposable** end-to-end architecture per ROAD-02:

```text
Source → AdapterDefinition → Binding + immutable BindingRevision
→ RunSourcePlan with pinned fallback order → one simple reliable API/feed adapter
→ Query → one SearchProfile → acquisition → JobObservation
→ canonical Job identity + job_source presence/provenance
→ job_profile_state + durable Inbox event → basic score/eligibility
→ shortlist/dismiss → direct apply → Application tracking → restart persistence
```

Ship condition (PROD-08): packaged vertical slice passes launch → create
profile → collect → observation/provenance → canonical job → Inbox →
shortlist/dismiss → direct apply → application tracking → restart → state
preserved.

## 0. Non-negotiable Slice 1 rules

1. **Prohibition (ROAD-02):** adapters never write canonical jobs directly;
   every job enters through `job_observations` → normalization → entity
   resolution → `job_sources`. No "retrofit provenance later".
2. Safety prerequisites are in-slice, minimal but final-shaped: SSRF/URL
   destination policy, page-validity gate, escaped/sanitized rendering,
   host-owned execution envelope, immutable observation identity,
   fenced/idempotent output persistence, atomic durable downstream-processing
   obligations, cancellation checks, RUN-21 projection ordering.
3. Single-machine capacity model (RUN-09): the service process is the
   authoritative claim/lease coordinator; executors never claim durable work.
4. All mutations stay behind the Slice-0 security shell (session + CSRF +
   loopback + bounded bodies). Every new route is deny-by-default.
5. Timestamps: UTC RFC-3339 (timeutil); lease/scheduling decisions use the
   database UTC clock consistently (RUN-20).
6. No Slice 2+ scope: one adapter, no FTS5, no fingerprinting/strategy
   router beyond pinned fallback order, no companies module beyond a
   minimal `companies` row for provenance, no contacts, no exports,
   no Adapter Lab, no scheduler (manual "collect now" only), no merge UI
   (merge ledger tables exist but no user-facing merge flow).

## 1. Work packages

| Package | Scope | Ship artifact |
|---|---|---|
| S1.0 | This plan; branch/bookkeeping | committed plan |
| S1.1 | Domain schema migration (v2) with RUN-18 constraints | `db/migrations` + tests |
| S1.2 | URL normalization + SSRF destination policy + safe links | `net/` module + tests |
| S1.3 | Durable run/request core: RunSourcePlan, claim/lease/fence, cancellation, run aggregation | `runtime/` + tests |
| S1.4 | Host-owned HTTP executor, ResultEnvelope, page-validity classifier, typed failures | `acquisition/` + tests |
| S1.5 | Adapter contract (manifest/task/plan/parse/cursor) + one declarative API/feed adapter | `adapters/` + tests |
| S1.6 | Observation persistence → normalization → entity resolution → job_sources → canonical jobs (RUN-21 ordered) | `pipeline/` + tests |
| S1.7 | Host-native downstream obligations: RECONCILE/ELIGIBILITY/SCORE + basic deterministic implementations | `pipeline/` + tests |
| S1.8 | Inbox: job_profile_state, durable inbox events, disposition transitions, dedupe | `inbox/` + tests |
| S1.9 | Applications, application_events, documents refs, direct-apply selection | `applications/` + tests |
| S1.10 | Service API + minimal dashboard (profiles, run trigger/cancel, inbox, jobs, applications) | `web/` + tests |
| S1.11 | Contract tests + automated acceptance gate (`verify_slice1.py`, fixture feed E2E incl. restart persistence) | `tests/` + gate |
| S1.12 | Native Windows acceptance strategy + harness W1-xx + packaged build update + final report | harness + report |

Execution order is linear; each package ends green (unit + integration) before
the next starts. Corrective review happens at the end (task §10 discipline),
then the native run.

## 2. Package details

### S1.1 — Domain schema (migration v2)

Physical tables (first Slice-1 tranche; later packages add their own
forward-only migrations on top):

- Source model: `sources`, `adapter_definitions`, `source_adapter_bindings`,
  `source_adapter_binding_revisions` (UNIQUE(binding_id, revision)),
  `adapter_permission_profiles`, `adapter_permission_profile_revisions`
  (UNIQUE(permission_profile_id, revision)).
- Profiles/queries: `search_profiles`, `profile_revisions`, `queries`,
  `query_revisions` (RUN-03).
- Run model: `scrape_runs`, `run_source_plans`
  (UNIQUE(run_id, source_plan_group_id, fallback_rank)), `scrape_requests`
  (UNIQUE(run_id, request_unique_key)), `request_attempts`, `crawl_cursors`.
- Evidence model: `fetch_attempts`, `parse_attempts`, `field_evidence`,
  `job_observations` (UNIQUE(request_id, observation_unique_key)),
  `enumeration_coverage` (+`coverage_contributing_request`,
  `coverage_seen_identity`), `entity_resolution_events`.
- Canonical model: `companies`, `jobs`, `job_locations`, `job_sources`,
  `job_history`.
- Profile-relative: `job_profile_state` (PK(job_id, profile_id) + `row_revision`),
  `job_profile_inbox_events` (UNIQUE(job_id, profile_id, dedupe_key)),
  `job_eligibility`, `job_scores`.
- Applications: `applications`, `application_events`, `documents`.

Rules: forward-only via existing migration machinery (backup-before-migrate,
integrity/FK gates); `job_sources.job_id` nullable until entity resolution;
no application status in `jobs`; availability fields per RUN-11/RUN-13.
Tests: migration up/down-integrity, RUN-18 uniqueness violations rejected,
slice-boundary (no Slice 2 tables).

### S1.2 — URL safety (`jobscraper/net/`)

`urlnorm.py`: spec 02 §31 normalization (escape decode, relative resolution,
scheme/host case, default-port trim, fragment policy, tracking-param removal
list is conservative and versioned, raw URL always preserved separately).
`destination.py`: allowlist policy object — schemes {http, https}, reject
embedded credentials, resolve DNS, reject loopback/private/link-local/special
ranges unless an explicit internal-feature grant (tests use it for the
fixture server), redirect validation per hop with cap, duration/byte caps,
source allow-host/path policy enforcement. Fail closed when the backend
cannot prove the peer. `safelinks.py`: PROD-05 render allowlist (http/https
only; `javascript:`/`data:` never clickable from source fields).
Tests: normalization equivalence classes, SSRF negatives (localhost by
literal/IPv6/DNS-rebinding-sim/redirect/decimal-IP forms), safe-link matrix.

### S1.3 — Durable run/request core (`jobscraper/runtime/`)

`runs.py`: create run + immutable `run_source_plans` snapshots (RUN-02 —
pins binding revision, permission revision, config snapshots, fallback
rank); aggregate states per RUN-01 truth table from source-plan groups.
`requests.py`: enqueue with `request_unique_key` (RUN-05 derivation),
states PENDING/RUNNING/RETRY_WAIT/SUCCEEDED/FAILED/CANCELLED, backoff +
max attempts. `claims.py`: atomic claim (RUN-07) with fresh attempt token +
lease, heartbeat validation (attempt + lease + not-cancelled), reclaim
(ABANDONED → PENDING/RETRY_WAIT/FAILED), "expired lease is already lost".
`fence.py`: fenced terminal commit helper (RUN-08) — BEGIN IMMEDIATE,
verify ownership affects ≥1 row, persist outputs + obligations + terminal
transition atomically, zero-row verify ⇒ no commit. `cancellation.py`:
§18 semantics — durable cancel, stop new acquisition claims, drain local
obligations, never delete evidence. Single-process coordinator only (RUN-09);
no worker-token design.
Tests: claim races (two claimers, one winner), lease expiry/reclaim, fenced
commit rejection after ownership loss, cancellation invariants, run
aggregation truth table (SATISFIED/SATISFIED_PARTIAL/FAILED/SKIPPED_NOT_NEEDED,
zero-job success), crash-point simulations (RUN-19 list) against a temp DB.

### S1.4 — Host-owned HTTP executor + validity gate (`jobscraper/acquisition/`)

`envelope.py`: ExecutionPlanEnvelope (§11) construction + validation by the
host after a durable claim; RequestPlan dataclass (§11.1). `httpexec.py`:
bounded executor (stdlib `http.client` or httpx if already locked — check
lockfile; no new deps without lockfile update) enforcing destination policy
per connection and per redirect, byte/duration caps, redacted headers,
304/revalidation passthrough fields. `result.py`: ResultEnvelope (§11.3).
`pagevalidity.py`: classifier with the Slice-1-relevant states (VALID_LIST,
VALID_JOB, LOGIN_REQUIRED, RATE_LIMITED, CHALLENGE_PAGE, EMPTY, NOT_FOUND,
JOB_CLOSED, UNEXPECTED_REDIRECT, UNEXPECTED_CONTENT, UNKNOWN) from
status/content-type/markers; login ≠ parser-failure ≠ empty (§21 invariant).
`failures.py`: typed FailureKind record (§27) with retryability +
source_health_impact.
Tests: fixture HTTP server (loopback, explicit internal grant) — happy path,
redirect outside policy, oversized body, timeout, 304, login-page
classification, redaction of secret headers.

### S1.5 — Adapter contract + feed adapter (`jobscraper/adapters/`)

`contract.py`: AdapterManifest schema validation, AdapterTaskKind
(HEALTH/DISCOVER/ENUMERATE/CRAWL/DETAIL/SMOKE), plan/parse/next_cursor
protocol (ACQ-02), ParseOutcome kinds incl. PARTIAL semantics (ACQ-03),
CrawlCursor + declared stop policy + loop/trap detection (§19: repeated
cursor/URL/page-hash/job-set, one empty page ≠ terminal).
`feed_api.py`: one declarative JSON API adapter (config: URL template,
page param, field mapping via JSON paths, source-native id field) —
sufficient for a real public JSON jobs feed and for the fixture server.
Registry: built-in adapters only, manifest-validated, no dynamic import.
Tests: manifest validation negatives, parse of fixture payloads (jobs,
empty, malformed field → required-field failure without invented values),
pagination stop policies, PARTIAL simulation (idempotent re-parse of same
request → no duplicate observations).

### S1.6 — Observation → canonical pipeline (`jobscraper/pipeline/`)

`observations.py`: insert immutable JobObservation under the request fence
(deterministic observation_unique_key; request-scoped idempotency).
`normalize.py`: deterministic description cleaning → markdown/text/lang/
hash (§34 minimal), salary parse preserving unknown-as-NULL (§37 minimal —
original text + min/max/currency/period when parseable).
`entity.py`: stage-1 identity (source_id, source_job_id) with the PROD-03
reuse guard (incompatible title/company/time-window evidence ⇒ split → new
identity + review evidence, never silent merge); entity_resolution_events.
`presence.py`: job_sources create/update under the same transaction as
entity resolution (RUN-11 ordering); presence transitions ACTIVE/UNCERTAIN/
EXPIRED/CLOSED driven only by enumeration coverage authority (RUN-13); a
`enumeration_coverage` finalization barrier (COMPLETE requires terminal
cursor + contributing requests + seen-identity union durable).
`canonical.py`: canonical jobs projection + job_history meaningful-change
classes (UNCHANGED/CONTENT_CHANGED/TITLE_CHANGED/APPLY_URL_CHANGED/
JOB_CLOSED/JOB_REOPENED minimal set), canonical provenance selection
(prefer employer/ATS over aggregator, §39) and best_application_url.
RUN-21: every projection update is conditional on (last input
evidence/revision order); older-late arrivals cannot regress newer state;
repeated application idempotent.
Tests: two observations same source id → one canonical job; reuse guard
split; older observation arriving after newer does not regress; change
classes; coverage COMPLETE vs PARTIAL absence rules; single-source close
evidence; canonical link selection.

### S1.7 — Downstream obligations + basic evaluation

`obligations.py`: atomic creation of host-native downstream tasks
(RECONCILE/ELIGIBILITY/SCORE per accepted observation) inside the fenced
commit; drain locally; obligations survive cancellation (§18) as
PENDING_PROCESSING/resumable. `eligibility.py`: minimal evidence-derived
verdict (ELIGIBLE when profile country/remote rules match normalized
locations; INELIGIBLE on explicit hard mismatch; UNCLEAR default — absence
of restriction text is never worldwide eligibility). `scoring.py`:
deterministic profile-relative score with per-contribution
rule/points/evidence/rule_version breakdown (§41) — title keyword fit,
salary floor, unknown-salary penalty; versioned rules snapshot pinned by
profile revision.
Tests: obligation atomicity (no observation without obligation, crash
between → recovery), cancellation drains, verdict matrix, score determinism
+ breakdown shape, revision pinning.

### S1.8 — Inbox (`jobscraper/inbox/`)

`state.py`: job_profile_state transitions + optimistic concurrency
(row_revision; stale overwrite rejected with typed conflict).
`events.py`: job_profile_inbox_events with dedupe_key (profile + job +
trigger occurrence identity: content revision / reopen relation / snooze
occurrence), trigger kinds NEW_ELIGIBLE_APPEARANCE, MEANINGFUL_CHANGE,
REOPENED, SNOOZE_EXPIRED, PROFILE_REVISION_ELIGIBLE; the PROD-02 transition
table exactly (NONE/SHORTLISTED emit-once; SNOOZED suppress until expiry;
DISMISSED/ARCHIVED sticky); re-observation alone creates no event;
restart/refresh/duplicate trigger ⇒ zero new rows.
Tests: full transition matrix, dedupe across restart, snooze occurrence
identity, sticky dismissal, optimistic concurrency conflict.

### S1.9 — Applications (`jobscraper/applications/`)

CRUD + status pipeline (PREPARING→APPLIED→…→CLOSED set), application_events
(incl. listing-closed event when canonical job closes while application
active), documents as references (no upload), next_action_at/text.
`applylink.py`: direct apply = open best_application_url in the user's
browser (host `webbrowser`-equivalent through the OS), never automated
submission (PROD-06); link passes the safelinks allowlist.
Tests: status transitions + events, closed-listing event, document
reference integrity, apply link scheme policy.

### S1.10 — Service API + dashboard

Routes (all session+CSRF protected except `/health/live`): profiles CRUD
(edits create profile_revisions), `POST /api/runs` (manual collect; creates
run + plans from current revisions), `POST /api/runs/{id}/cancel`,
`GET /api/inbox` (profile-relative, score+eligibility+breakdown),
`POST /api/profiles/{pid}/jobs/{jid}/disposition` (shortlist/dismiss/
snooze/archive with reason), `GET /api/jobs/{id}` (provenance: all source
links inspectable), applications CRUD. Minimal server-rendered dashboard
pages (Inbox triage with keyboard actions, job detail, applications) with
Jinja autoescape + safelinks; htmx where it helps, no new vendor deps.
Tests: route auth matrix, revision creation on edit, disposition
round-trip, provenance visibility.

### S1.11 — Contract + acceptance gate

`tests/contract/test_slice1_contract.py`: slice boundary (no FTS5 usage,
no second adapter, no scheduler, no contacts/export tables used), lockfile
discipline, schema/migration monotonicity, security shell still intact.
`tests/integration/test_slice1_acceptance.py`: full E2E against the fixture
feed server through the real service process: launch → create profile →
run → observation → canonical job → score/eligibility → inbox event →
shortlist → dismiss another → application create/update → **restart
service** → all state preserved (PROD-08). Plus cancellation mid-run and
crash-recovery (kill during run → resume → no duplicate observations).
`scripts/verify_slice1.py`: gate aggregator (tests + pip check + doctor).

### S1.12 — Native acceptance + report

W1 checks (extend `scripts/native_acceptance.py`): packaged E2E vertical
slice incl. restart persistence on Windows, SSRF negative proof from
packaged exe, inbox dedupe across restart, doctor healthy. Update
`build/jobscraper.spec` if new data files are needed. Final Slice 1
status report with the same discipline as Slice 0: IMPLEMENTATION
COMPLETE / NATIVE PROMOTION PENDING until the Windows run passes.

## 3. Completion gates

1. Automated gate green (`scripts/verify_slice1.py`).
2. Corrective review pass (races, fail-open, swallowed errors, stale-owner,
   projection ordering, Linux-only assumptions) with findings fixed.
3. Native Windows W1 run executed and evidence committed, or blockers
   documented precisely.
4. Slice 2 not started until the above.

## 4. Explicit deferrals (with justification)

- Scheduler (§49): Slice 1 ships manual collection; scheduling is runtime
  breadth owned by Slice 3. Restart persistence does not require it.
- FTS5 search: ROAD-03; the Slice-1 dashboard lists inbox/jobs without
  text search. (Doctor already verifies FTS5 capability.)
- Multi-adapter breadth, fingerprinting, strategy router, origin resolver,
  companies module, contacts, exports, Adapter Lab: ROAD-02/03 boundaries.
- Merge UI + undo: entity resolution stage 1 only; merge ledger exists but
  no user flow (ROAD-02 does not require user-facing merge).
