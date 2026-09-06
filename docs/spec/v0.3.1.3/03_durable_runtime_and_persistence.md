# Windows Job Scraper v0.3.1.3 — Durable Runtime & Persistence Specification

**Version:** Windows Job Scraper v0.3.1.3 — Final Corrected Modular Specification Set  
**Date:** 2026-09-05  
**Status:** **NORMATIVE — implementation specification**  
**Target:** Windows 11 · local-first · single-user  
**Core stack:** Python 3.12+ · FastAPI · SQLite + FTS5 · Playwright/Chromium

> This file is one member of the canonical modular specification set. Normative ownership is defined by `00_architecture_overview_and_authority.md`. If duplicated explanatory text conflicts with the normative owner, the normative owner wins.


## RUN-00. Normative ownership

This file owns durable run identity, queue semantics, leases, attempts, concurrency coordination, retries, circuits, query/profile snapshots, observations/presence persistence, revalidation, scheduler and SQLite correctness.

# 15. Rate, pacing, backoff, and circuit breakers

Concurrency and rate are separate controls.

Required policy dimensions:

- global HTTP concurrency;
- global browser concurrency;
- global browser-interactive concurrency;
- per-source concurrency;
- per-host concurrency;
- optional per-egress concurrency;
- minimum inter-request delay or token-bucket rate where needed;
- requests/time-window budget;
- page/detail budget;
- browser-time budget;
- run runtime budget;
- `Retry-After` honoring;
- exponential backoff;
- jitter;
- cooldown/circuit breaker;
- clean-success recovery.

Example policy:

```yaml
http:
  max_concurrency: 16

browser:
  max_concurrency: 1

source:
  max_concurrency: 2
  min_interval_ms: 750
  max_requests_per_run: 300
  max_pages_per_run: 200
  max_detail_fetches_per_run: 200
  max_runtime_seconds: 900
```

These values are tunable defaults.

Repeated `429`, challenge, or block evidence opens a circuit or degrades the affected binding. It does not trigger identity rotation or CAPTCHA bypass.

---


# 16. Durable run/frontier contract

The durable queue is a correctness subsystem, not merely a retry list.

Recommended logical tables:

```text
scrape_runs
scrape_requests
crawl_cursors
```

## 16.1 `scrape_runs`

```text
id
profile_id
status
collection_status
processing_status
created_at
started_at
finished_at
cancel_requested_at
jobs_discovered
jobs_saved
jobs_updated
requests_total
requests_failed
error_summary_json
```

## 16.2 `scrape_requests`

```text
id
run_id
source_id
binding_id
request_type
request_unique_key
payload_json
strategy
execution_class
priority
depth
parent_request_id

status
attempt_count
max_attempts
next_retry_at

current_worker_id
current_attempt_id
lease_until
heartbeat_at

cursor_checkpoint_ref
started_at
finished_at

page_class
bytes_downloaded
duration_ms

last_failure_kind
last_failure_json
created_at
updated_at
```

Request types may include:

```text
SOURCE_HEALTH_CHECK
SOURCE_DISCOVERY
LIST_FETCH
DETAIL_FETCH
SOURCE_CRAWL
NORMALIZE
RECONCILE
ENRICH
ELIGIBILITY
SCORE
ADAPTER_SMOKE
EXPORT
```


Acquisition request types map to the adapter protocol owned by `02`: health→`HEALTH`, discovery→`DISCOVER`, list→`ENUMERATE`, crawl→`CRAWL`, detail→`DETAIL`, smoke→`SMOKE`. `NORMALIZE`, `RECONCILE`, `ENRICH`, `ELIGIBILITY`, `SCORE`, and `EXPORT` are host-native pipeline tasks.

Physical implementation MAY split I/O requests and pipeline tasks into separate tables if desired, but the lease/fencing semantics below remain mandatory.

## 16.3 Request states

```text
PENDING
RUNNING
RETRY_WAIT
SUCCEEDED
FAILED
CANCELLED
```

No terminal state transitions back to `RUNNING`.

---


# 18. Cancellation semantics

Cancellation is durable.

When a run is cancelled:

- set `cancel_requested_at` and stop **new source-network acquisition** claims for that run;
- running network workers observe cancellation at bounded checkpoints and lose authority for further network work/commit as defined by the fence;
- pending/retry-wait acquisition requests become `CANCELLED`;
- already accepted observations/evidence remain valid;
- durable host-native processing obligations already created for accepted observations are **not silently cancelled**; they either drain locally to a canonical projection or remain visibly `PENDING_PROCESSING`/resumable under explicit policy;
- local processing MUST NOT use cancellation as authority to initiate new source I/O;
- cancellation does not delete already persisted evidence;
- browser worker teardown is supervised;
- cancellation is distinct from failure.

Run state has two observable dimensions: collection completion/cancellation and downstream processing completion. A run must not be reported fully finished while relevant dynamically created work remains pending or while accepted observations are stranded without a declared resumable state.

A worker that loses its lease or receives cancellation may not perform a late canonical commit.

---


# 29. Provenance-first observation model

A collector never writes directly to the canonical job table.

First persist a `JobObservation`.

```text
job_observations
----------------
id
run_id
request_id
attempt_id
source_id
binding_id
adapter_id
adapter_version
strategy
execution_class
query_id
source_job_id
raw_url
canonical_url_candidate
application_url_candidate
page_cursor_json
enumeration_scope_key
source_rank_or_order
observed_at
raw_payload_ref
parse_evidence_ref
observation_unique_key
```

The canonicalization pipeline consumes observations.


Each observation emitted by one logical request carries a deterministic `observation_unique_key` derived from stable source-native identity or a stable normalized record key plus logical cursor/detail identity. Enforce uniqueness within the request so retries after a `PARTIAL` outcome or crash do not duplicate the same observation while later independent runs may still record re-observation.

Every canonical job can therefore answer:

- where it came from;
- which adapter/version parsed it;
- which request fetched it;
- which raw/structured evidence supported it;
- which fields were extracted;
- how it was normalized;
- why it merged with another observation.

---


# 30. Fetch, parse, and field evidence

Logical evidence chain:

```text
FetchAttempt
→ ResultEnvelope
→ ParseAttempt
→ FieldEvidence
→ JobObservation
→ Normalization event
→ EntityResolution event
→ Canonical Job identity
→ job_sources presence/provenance update
→ canonical presentation/provenance selection
```

Retention need not keep every full HTML page forever.

The evidence plane may retain:

- hashes;
- selected sanitized excerpts;
- structured payload snapshots;
- status/headers metadata;
- parser diagnostics;
- fixture captures;
- field evidence spans.

High-value saved/applied jobs may receive longer evidence retention.

---


# 40. Revalidation, disappearance, closure, and reposts

Each canonical job / mutable source-presence record tracks:

```text
first_seen_at
last_seen_at
last_verified_at
last_changed_at
availability_state
content_revision
```

Source URL revalidation may use:

- `ETag`;
- `Last-Modified`;
- `If-None-Match`;
- `If-Modified-Since`;
- normalized content hash;
- provider-native updated timestamps;
- sitemap `lastmod`.

HTTP `304 Not Modified` is revalidation of a **previously validated retained representation**, never an empty result. It may skip body parsing only when the request resolves a cache representation with the same source/binding/auth/request variant and compatible parser/recipe interpretation. For authoritative enumeration, the retained representation must also retain its known membership/seen identities; those members may be reused as seen evidence for the new verification. A bare `304` with missing/pruned/incompatible representation or membership cannot create authoritative empty coverage: fetch unconditionally or return a non-authoritative outcome. Verification time may advance without incrementing content revision when content is unchanged.

The retained cache identity is explicit and durable enough to prove reuse:

```text
cache_representation
--------------------
id
source_id
binding_revision_id
auth_scope_generation
request_variant_key
validated_page_class
body/content hash
parser_recipe_compatibility_key
membership_ref                 # required for authoritative list reuse
stored_at
expires_at / retention_policy
```

Auth/session generation or request-variant mismatch forbids reuse. Retention must not prune a representation/membership that an in-progress backup or accepted revalidation still references.

Absence inference requires an explicit durable enumeration-coverage result:

```text
enumeration_coverage
--------------------
id                         # one durable coverage generation
run_source_plan_id
source_plan_group_id
source_id
binding_id
binding_revision_id
scope_key
generation_key
started_at
finished_at
completion_state
stop_reason
pages_completed
items_observed
cursor_terminal
terminal_enumeration_proven
contributing_request_count
coverage_authority
absence_inference_allowed
finalized_at
applied_at
created_at
```

`completion_state`:

```text
COMPLETE
PARTIAL
CANCELLED
FAILED
BUDGET_EXHAUSTED
UNKNOWN
```

`coverage_authority`:

```text
AUTHORITATIVE_FULL_SOURCE
AUTHORITATIVE_DECLARED_SCOPE
NON_AUTHORITATIVE_QUERY
DETAIL_ONLY
NO_ABSENCE_INFERENCE
```

Absence evidence may be created only when all are true:

1. `completion_state = COMPLETE`;
2. `coverage_authority` is `AUTHORITATIVE_FULL_SOURCE` or `AUTHORITATIVE_DECLARED_SCOPE`;
3. `absence_inference_allowed = true`;
4. the source-presence record belongs to the same declared `scope_key`;
5. the run was not invalidated by challenge, auth failure, policy denial, or cancellation.

A budget-limited, page-limited, query-ranked, cancelled, failed, or otherwise partial run MUST NOT age unseen jobs toward expiry merely because the job was not observed.

Each `enumeration_coverage` row is one **coverage generation**, not page-by-page disappearance evidence. It durably links all contributing enumeration requests/pages and the stable union of seen source identities:

```text
coverage_contributing_request
-----------------------------
coverage_id
request_id
PRIMARY KEY(coverage_id, request_id)

coverage_seen_identity
----------------------
coverage_id
stable_source_identity
source_identity_generation
observation_or_listing_evidence_ref
PRIMARY KEY(coverage_id, stable_source_identity, source_identity_generation)
```

The service owns a finalization barrier. `COMPLETE` may be declared only after terminal enumeration is proven, every contributing page/cursor required for that generation has reached an accepted terminal state, no relevant continuation work remains, and the seen-identity union is durable. Detail fetch completion is **not** required to prove listing presence when the binding contract declares listing identity sufficient; otherwise detail completion is part of the barrier. Unstable pagination/order that can skip records while paging is non-authoritative unless the source exposes a stable snapshot/cursor contract or another tested mechanism that proves coverage.

Each `enumeration_coverage.id` is applied to each affected source-presence/scope at most once. Replaying/finalizing the same generation is idempotent and creates zero additional disappearance transitions. Overlapping runs/generations are ordered under `RUN-21`; an older generation cannot regress newer presence.

A missing job in one complete absence-authoritative enumeration yields evidence of disappearance, not immediate closure.

Example policy:

```text
seen recently
→ ACTIVE

missing from one complete absence-authoritative enumeration
→ UNCERTAIN

missing repeatedly from complete absence-authoritative enumerations under policy
→ EXPIRED candidate

direct provider/page evidence that job closed
→ CLOSED
```

A failed source run MUST NOT mark all absent jobs closed.

Meaningful change classes:

```text
UNCHANGED
CONTENT_CHANGED
SALARY_CHANGED
LOCATION_CHANGED
TITLE_CHANGED
APPLY_URL_CHANGED
JOB_CLOSED
JOB_REOPENED
REPOST_DETECTED
PAGE_TEMPLATE_CHANGED
EXTRACTION_DRIFT
UNKNOWN_CHANGE
```

Repost logic is evidence/heuristic driven. It may link a new posting to an earlier job without automatically treating the new record as identical.

Source-native ID reuse MUST be detected by incompatible content/time evidence.

---


# 47. Snapshot and retention policy

App-owned raw snapshots/fixtures/cache artifacts referenced by the database participate in retention through stable content IDs. Pruning and backup capture are coordinated so an artifact cannot be deleted after the backup manifest references it but before capture completes. Historical provenance retains at least the hashes, minimal field/evidence excerpt, input revision and parser/normalizer/build identity needed to explain a derived value after bulky raw content is pruned.

Snapshot types may include:

```text
RAW_METADATA
NORMALIZED_TEXT
HTML_FRAGMENT
STRUCTURED_JSON
SCREENSHOT_DEBUG
```

Do not retain every full HTML page forever.

Retention priorities:

- keep current normalized representation;
- keep previous meaningful revision;
- keep more history for shortlisted/applied jobs;
- keep fixture corpus for maintained adapters;
- prune low-value raw snapshots;
- enforce configurable disk budget.

Authenticated snapshots:

- local only;
- bounded retention;
- redacted where practical;
- no secrets in filenames;
- no auth material included in adapter exports.

---


# 49. Local scheduler

The scheduler is local.

Support:

- per-source intervals;
- profile schedules;
- one active run per source/binding policy where appropriate;
- global/class capacity limits;
- staggered starts;
- catch-up after sleep/wake;
- missed-run policy that avoids launching N duplicate runs;
- optional pause on battery;
- optional pause on metered network;
- optional Windows Task Scheduler registration to start app at logon.


Scheduler/time semantics:

- durable timestamps remain UTC;
- each profile schedule/reminder carries an explicit timezone identifier;
- next-run/reminder calculation records the timezone/rule revision used;
- DST nonexistent/ambiguous local times have a deterministic documented policy;
- sleep/wake catch-up remains bounded and never fans out one run per missed interval by default.

Adaptive cadence may later use:

- new-job rate;
- 304 ratio;
- change rate;
- sitemap churn;
- repeated no-change runs.

Adaptive cadence remains within hard configured min/max bounds.

---


# 50. SQLite operating rules

Release baseline connections enable foreign keys and use WAL with a declared durability policy. For v0.3.1.3 the default is `PRAGMA synchronous=FULL` for the primary user-workflow database; a future release may relax this only after an explicit durability amendment and Windows power/interruption evidence demonstrating the promised guarantee. Migration/restore gates run `integrity_check`, `foreign_key_check`, and application-level consistency checks, and verify effective PRAGMA values on every relevant connection.

Lease correctness uses database UTC plus a service clock epoch. If the service detects a material backward/forward wall-clock anomaly beyond the configured tolerance, it stops new claims, invalidates current in-memory ownership under a fresh service epoch, records the anomaly, and safely reclaims/retries rather than extending an already elapsed lease. Monotonic elapsed time is used within a live process for bounded heartbeat/timeout decisions where applicable.

Required:

- `foreign_keys=ON`;
- explicit `busy_timeout`;
- WAL unless real Windows testing proves a blocker;
- forward-only migrations;
- backup before schema mutation;
- integrity check after migration;
- UTC timestamps;
- explicit transaction boundaries;
- no network/browser wait while holding DB transaction;
- batched writes where appropriate;
- bounded event/snapshot retention;
- `PRAGMA optimize`/maintenance policy;
- verifiable backups.

Recommended connection model:

- one write coordinator per process;
- reader connections separated;
- FastAPI database calls do not block the event loop.

Durability policy may differ by data class; correctness and recoverability matter more than tiny throughput gains.

---


# 51. Core logical data-model inventory

This is a compact inventory, not a second competing schema definition. The corrected authoritative logical table set and additions are consolidated in `RUN-17`; if this inventory omits a later corrective object, `RUN-17` and the owning requirement section control. The exact migration layout may refine columns, but the following semantics are mandatory.

Core tables/objects:

```text
schema_migrations

sources
adapter_definitions
source_adapter_bindings
source_adapter_binding_revisions
adapter_permission_profiles
adapter_permission_profile_revisions

search_profiles
profile_revisions
queries
query_revisions
job_profile_state
job_profile_inbox_events

scrape_runs
scrape_requests
crawl_cursors

fetch_attempts
parse_attempts
job_observations
enumeration_coverage
coverage_contributing_request
coverage_seen_identity
field_evidence
entity_resolution_events

companies
jobs
job_locations
job_sources
job_history
job_facts
job_eligibility
job_scores
job_merges

recipes
recipe_versions
navigation_plans
navigation_plan_versions
source_fixtures
locator_health

contacts

applications
application_events
documents
reminders / notification state

source_binding_health
source_health_events
events

snapshots
egress_profiles          # optional
fx_rates
user_feedback
```

Do not create duplicate physical tables merely to match this list. A physical schema may combine equivalent concepts if the stated semantics, indexes, constraints, and auditability remain intact.

Recommended indexes include:

```text
source_id + binding_id + observed_at
adapter_id + adapter_version
run_id + request status
lease_until + status
request_unique_key
source_job_id
origin provider/board/job id
canonical URL candidate
company_id
first_seen_at / last_seen_at / last_verified_at
availability/listing status
failure kind + binding
profile_id + score
```

---


# 52. Canonical `jobs` model

Suggested fields:

```text
jobs
----
id
company_id
title
normalized_title
description_md
description_text
description_lang
description_hash
employment_type
experience_level
remote_mode
remote_worldwide

salary_original_text
salary_min
salary_max
salary_currency
salary_period
salary_annual_min_ref
salary_annual_max_ref

posted_at
discovered_at
first_seen_at
last_seen_at
last_verified_at
last_changed_at

listing_status
fingerprint
canonical_provenance_id

origin_provider
origin_board
origin_job_id

notes_md
created_at
updated_at
```

Do not store application pipeline status in `jobs`.

Source URLs and application URLs belong in provenance/source observation structures, with selected canonical link fields derived for presentation where useful.

---


## RUN-01. Run aggregate states

Canonical run states:

```text
QUEUED
RUNNING
SUCCEEDED
PARTIAL
FAILED
CANCELLED
```

Run status is aggregated from **logical source-plan groups**, not individual fallback rows. The complete rule is the truth table below; in particular, a successful fallback satisfies its group, an unused fallback is not a failure, and a valid complete zero-job result is success.

One source-group failure MUST NOT erase or invalidate successful independent source results.

### Source-plan-group and aggregate outcome contract

Fallback bindings belong to one logical `source_plan_group_id`. The group, not each unused fallback, is the unit aggregated into the run result.

Group outcomes:

```text
SATISFIED            # primary or fallback completed the logical source plan, including valid zero-job success
SATISFIED_PARTIAL    # usable accepted data exists but the logical plan remains visibly incomplete/budget-limited
FAILED               # all eligible fallbacks exhausted/terminally failed
CANCELLED
POLICY_DENIED
SKIPPED_NOT_NEEDED   # fallback not activated because an earlier rank satisfied the group
```

Activation is deterministic by pinned `fallback_rank`: activate the next eligible fallback only after the current rank reaches a declared fallback-eligible terminal outcome. A successful fallback makes the group `SATISFIED`; prior binding failure remains diagnostic history, not run failure. Unused fallbacks are `SKIPPED_NOT_NEEDED`, never failures. A recognized complete collection with zero jobs is `SATISFIED`.

Run aggregation truth table:

| Relevant source-plan groups | Run status |
|---|---|
| all `SATISFIED` | `SUCCEEDED` |
| at least one `SATISFIED`/`SATISFIED_PARTIAL` and at least one terminal `FAILED`/`POLICY_DENIED`, or any accepted group remains deliberately incomplete | `PARTIAL` |
| all terminal non-cancelled groups failed/denied and no usable accepted result | `FAILED` |
| user cancellation requested | `CANCELLED` (preserving already committed results and processing visibility) |

A run cannot become terminal while relevant dynamically created group work, continuations, or required local processing obligations remain pending.

## RUN-02. Immutable `RunSourcePlan`

A run may target many sources and each source may have ordered fallback bindings.

At run creation, persist immutable execution plans:

```text
run_source_plans
----------------
id
run_id
source_id
source_revision_id
source_config_snapshot_ref
query_id
query_revision_id

source_plan_group_id
fallback_rank

binding_id
binding_revision_id
binding_revision
binding_config_snapshot_json

adapter_id
adapter_version
adapter_api_version
strategy
execution_class
cursor_schema_version
recipe_version_id
navigation_plan_version_id

crawl_policy_snapshot_json
rate_policy_snapshot_json
auth_scope_id
permission_profile_id
permission_profile_revision

profile_revision
rules_revision
run_config_hash
created_at
```

Rules:

1. A plan is immutable after work begins and references reconstructible immutable snapshots/revisions for source configuration, query, profile/rules, binding permissions/policies, and effective normalization/evaluation policy that can change interpretation. Hash/version references are valid only while the referenced immutable content remains resolvable.
2. Adapter/binding promotion affects new plans only.
3. Retired historical bindings/versions remain resolvable for history/resume.
4. A source may pin an ordered set of viable fallback plans where policy allows.
5. Cursor compatibility is checked against the pinned identity.
6. If the exact pinned implementation is unavailable, resume requires an explicit compatible migration/restart path.
7. `source_plan_group_id + fallback_rank` pins deterministic fallback order.
8. The plan pins the exact binding revision and security-relevant permission-profile revision used to construct execution.
9. Current cancellation, source/binding quarantine, auth-scope revocation, permission revocation, or security-policy denial overrides a pinned plan for future execution/commit; reproducibility never grants continuing authorization.
10. Derived eligibility/score/presentation events record the input content/profile/rule/normalization/evaluator/build versions they evaluated separately from the mutable current projection.

## RUN-03. Query and profile reproducibility

Define mutable identities separately from immutable revisions:

```text
queries
-------
id
profile_id
current_revision_id
created_at
updated_at

query_revisions
---------------
id
query_id
profile_revision_id
revision
query_kind
query_text_or_json
source_scope_json
content_hash
created_at

profile_revisions
-----------------
id
profile_id
revision
profile_snapshot_json
rules_revision_id
content_hash
created_at
```

A run MUST preserve references to the exact immutable query/profile/rules/config revisions used. Editing a query or profile creates a new revision; it MUST NOT mutate an active or historical run's interpretation.

Salary/reference conversions should record the FX data version/date used.

## RUN-04. Corrected request model

Recommended request fields include:

```text
scrape_requests
---------------
id
run_id
run_source_plan_id
query_id
source_id
binding_id

request_type
request_unique_key
payload_json
strategy
execution_class
priority
depth
parent_request_id

status
attempt_count
max_attempts
next_retry_at

current_worker_id
current_attempt_id
lease_until
heartbeat_at

cursor_checkpoint_ref
started_at
finished_at

page_class
bytes_downloaded
duration_ms
last_failure_kind
last_failure_json
created_at
updated_at
```

## RUN-05. `request_unique_key`

The key must identify a logical unit of work, not merely a URL.

Recommended derivation:

```text
hash(
  run_source_plan_id,
  request_type,
  normalized target identity,
  strategy/purpose,
  logical pagination/detail key
)
```

Normal uniqueness:

```text
UNIQUE(run_id, request_unique_key)
```

Requirements:

- same logical request enqueued twice → one durable work item;
- same URL under HTTP probe and supported browser escalation → may be distinct;
- tracking-only URL variation must not create unbounded duplicates;
- retry creates a new attempt, not a duplicate request.

## RUN-06. Generic attempt history

Current request ownership is not enough for audit/recovery.

Add:

```text
request_attempts
----------------
attempt_id
request_id
execution_plan_id
worker_id
started_at
last_heartbeat_at
lease_expires_at
finished_at
outcome
failure_kind
abandoned_reason
created_at
```

`fetch_attempts` and `parse_attempts` reference this attempt where applicable.

Non-fetch tasks such as normalization/reconciliation therefore retain the same retry/ownership history.

## RUN-07. Corrected atomic claim and lease semantics

Atomic claim:

```text
PENDING or due RETRY_WAIT
→ RUNNING
current_worker_id = worker
current_attempt_id = fresh unique token
lease_until = database_now + lease_window
heartbeat_at = database_now
attempt_count += 1
```

Only one worker receives the claim.

### Heartbeat

A heartbeat is accepted only when:

```text
request_id matches
AND status = RUNNING
AND current_attempt_id matches
AND lease_until > database_now
AND run cancellation has not invalidated continued work
```

An expired lease is **already lost ownership**, even if the reclaimer has not yet executed.

A worker MUST NOT revive an expired lease.

### Reclaim

```text
RUNNING + lease_until <= database_now
→ record prior attempt ABANDONED
→ PENDING / RETRY_WAIT if budget remains
→ FAILED otherwise
```

Use the database time source consistently for claim/renew/reclaim comparisons.

## RUN-08. Fenced terminal commit

Output persistence and the request terminal transition MUST be atomic or equivalently idempotent under the same ownership fence.

Conceptual transaction:

```text
BEGIN IMMEDIATE

verify:
  request = RUNNING
  current_attempt_id = this attempt
  lease_until > database_now
  run not invalidated for terminal commit

persist:
  fetch/parse evidence
  immutable observations
  atomically create/satisfy deterministic host-native downstream processing obligations for each accepted observation
  deterministic child-work enqueue
  cursor/checkpoint update

transition:
  request → SUCCEEDED
  OR request → RETRY_WAIT when validated PARTIAL policy requires retry

COMMIT
```

For a `PARTIAL` parse, valid observations/evidence and deterministic child work may be committed under the fence, but associated enumeration coverage is `PARTIAL` and cannot support absence inference. If retry/continuation is scheduled, request-scoped observation/child-work idempotency prevents duplicates.

If ownership verification affects zero rows, the worker MUST NOT commit request-owned outputs.

User-owned workflow mutations are never hidden inside retryable acquisition transactions.

## RUN-09. Single-machine capacity coordinator

For this Windows-local architecture:

> **The service process is the authoritative durable claim and capacity coordinator.**

It owns global/per-source/per-host/class capacity.

Flow:

```text
service claims fenced request
→ reserves relevant capacity
→ dispatches typed work to HTTP/browser/isolated worker
→ worker returns heartbeat/result over typed IPC
→ service performs fenced persistence
```

Workers do not independently claim more durable work behind the service's back.

A later DB-token design may replace this only through an explicit architecture revision.

## RUN-10. Durable rate/circuit state

Rate control is not only in-memory.

Persist per binding/host where appropriate:

```text
circuit_state
cooldown_until
recent_failure_count
recent_success_count
last_retry_after
last_rate_event_at
last_success_at
```

Restarting the application MUST NOT erase a meaningful `Retry-After` or active cooldown and immediately hammer the source again.

## RUN-11. `job_sources` / source-presence record

Immutable `job_observations` must not be overloaded with mutable current presence.

Define:

```text
job_sources
-----------
id
job_id
source_id
binding_id
source_job_id

discovery_url
raw_source_url
canonical_job_url
application_url
origin_url

first_seen_at
last_seen_at
last_verified_at
last_changed_at
presence_state
content_revision
last_authoritative_scope_key
last_absence_coverage_id

last_observation_id
source_rank
created_at
updated_at
```

Semantics:

```text
job_observations = immutable source events
job_sources      = mutable current per-source presence/provenance
jobs             = canonical resolved entity
job_history      = canonical meaningful change history
```

The selected direct/canonical links shown in the UI are derived from these provenance records rather than destroying original URLs.


Canonical creation ordering is:

```text
immutable JobObservation
→ normalization
→ entity resolution / create-or-select canonical Job identity
→ create/update job_sources presence/provenance
→ update canonical presentation fields/provenance selection
→ downstream eligibility/scoring
```

`job_sources.job_id` is therefore never required before canonical entity resolution has established the job identity. Entity resolution and first source-presence creation should be atomic where practical.

## RUN-12. Canonical provenance field definitions

`canonical_provenance_id` on `jobs` refers to the currently selected provenance/source-presence record supporting the displayed canonical fields.

`fingerprint` is a deterministic canonical-identity helper/versioned hash, **not** an irreversible primary identity proof.

Recommended distinction:

```text
discovered_at = first time this canonical job entered the local canonical corpus
first_seen_at = earliest supported source-presence observation
```

If implementation chooses one timestamp for both, the semantic equivalence must be deliberate and documented.

## RUN-13. Corrected revalidation state model

Revalidation updates `job_sources`, never retroactively mutates the historical meaning of a `JobObservation`.

Per-source presence transitions may include:

```text
ACTIVE
UNCERTAIN
EXPIRED
CLOSED
WITHDRAWN
UNKNOWN
```

Evidence rules:

```text
complete relevant enumeration + seen
→ presence ACTIVE

missing from one COMPLETE absence-authoritative enumeration for the same scope
→ presence UNCERTAIN

missing repeatedly from COMPLETE absence-authoritative enumerations under policy
→ EXPIRED candidate

explicit provider/detail evidence of closure
→ CLOSED

failed/challenged/auth-expired source run
→ no blanket absence inference
```

One failed run MUST NOT close or expire absent jobs.
A `PARTIAL`, `BUDGET_EXHAUSTED`, query-non-authoritative, cancelled, failed, challenged, auth-expired, or policy-denied enumeration MUST NOT generate absence evidence.

Each absence-driven presence transition records the `enumeration_coverage.id` that authorized the inference.


For declared-scope absence inference, scope membership must be explicit from authoritative observation/plan evidence. If one source-presence can belong to multiple authoritative scopes, model that relationship separately rather than guessing scope membership from normalized job fields.

## RUN-14. Multi-source canonical availability resolution

Canonical `jobs.listing_status` is derived from current source-presence evidence.

Default policy:

```text
explicit trusted employer/ATS close evidence
→ strong CLOSED evidence

otherwise any sufficiently fresh trusted active source presence
→ canonical ACTIVE

all trusted presences repeatedly absent/expired beyond policy
→ UNCERTAIN or EXPIRED according to policy

conflicting evidence
→ preserve conflict;
   prefer current higher-quality employer/origin evidence;
   do not silently close
```

An aggregator disappearance cannot close a job that a trusted employer ATS still reports active.

Applied-job closure notifications use the resolved canonical state.

## RUN-14A. Temporal precedence for availability evidence

Availability resolution is evidence-ordered, not merely worker-completion-ordered. A trusted closure, active sighting, or absence generation carries an observed/effective source timestamp plus local receipt/revision metadata. Older evidence may remain in history but cannot overwrite a newer accepted current projection solely because it finished processing later. Closed→reopened is supported when newer trusted active evidence supersedes older closure evidence; newer explicit trusted closure may supersede older active evidence under the declared source-quality policy.

## RUN-15. Source-native ID reuse

`(source_id, source_job_id)` is strong identity with a reuse guard.

When incompatible temporal/entity/content evidence proves likely reuse, create a new source-presence/canonical identity or require review.

Historical observations remain linked to the old identity. Current source identity uniqueness includes a `source_identity_generation` (or equivalent) once reuse is detected, so simultaneous/current rows cannot collide across reused native identifiers.

## RUN-16. Per-profile disposition persistence

Although product behavior is owned by `01`, the physical schema MUST include or equivalently represent:

```text
job_profile_state(job_id, profile_id, disposition, ...)
```

with a unique `(job_id, profile_id)` key.

## RUN-17. Consolidated corrected logical tables

At minimum the logical model now includes:

```text
schema_migrations

sources
adapter_definitions
source_adapter_bindings
source_adapter_binding_revisions
adapter_permission_profiles
adapter_permission_profile_revisions

search_profiles
profile_revisions
queries
query_revisions
job_profile_state
job_profile_inbox_events

scrape_runs
run_source_plans
scrape_requests
request_attempts
crawl_cursors

fetch_attempts
parse_attempts
field_evidence
job_observations
enumeration_coverage
job_sources
entity_resolution_events

companies
jobs
job_locations
job_history
job_facts
job_eligibility
job_scores
job_merges

recipes
recipe_versions
navigation_plans
navigation_plan_versions
source_fixtures
locator_health

contacts
applications
application_events
documents
reminders / notification_state

source_binding_health
source_health_events
events

snapshots
egress_profiles
fx_rates
user_feedback
```

Physical consolidation is permitted only if semantics, constraints, auditability and indexes remain intact.

## RUN-18. Required additional indexes/constraints

At minimum evaluate:

```text
UNIQUE(run_id, request_unique_key)
UNIQUE(request_id, observation_unique_key) on job_observations
UNIQUE(binding_id, revision) on source_adapter_binding_revisions
UNIQUE(permission_profile_id, revision) on adapter_permission_profile_revisions
UNIQUE(run_id, source_plan_group_id, fallback_rank) on run_source_plans
UNIQUE(job_id, profile_id) on job_profile_state
UNIQUE(job_id, profile_id, dedupe_key) on job_profile_inbox_events
(source_id, binding_id, observed_at)
(job_id, source_id, source_job_id)
(adapter_id, adapter_version)
(status, lease_until)
(run_source_plan_id, status)
(source_job_id)
(origin_provider, origin_board, origin_job_id)
(profile_id, score)
(last_seen_at, last_verified_at)
UNIQUE(run_source_plan_id, scope_key, generation_key) on enumeration_coverage
UNIQUE(coverage_id, request_id) on coverage_contributing_request
UNIQUE(coverage_id, stable_source_identity, source_identity_generation) on coverage_seen_identity
(completion_state, coverage_authority, source_id, binding_id) on enumeration_coverage
```

Exact indexes are selected from query plans, but uniqueness invariants are not optional.

## RUN-19. Crash points that must remain safe

The architecture must tolerate:

- crash after claim before dispatch;
- crash during network/browser I/O;
- crash after fetch evidence before parse;
- crash after observation insert before normalization;
- crash while enqueueing child detail work;
- crash immediately before/after `SUCCEEDED`;
- browser worker death;
- Windows restart;
- service restart with active cooldown;
- adapter promotion while old run is resumable.

No case may permit a stale attempt to overwrite a newer owner.

## RUN-20. Storage timestamp rule

Lease and scheduling comparisons use a consistent UTC/database time source.

Wall-clock adjustments must not be allowed to create duplicate ownership silently.

Where monotonic time is used in-process for elapsed durations, durable timestamps remain UTC/RFC3339-compatible.

## RUN-21. Current projection ordering and stale-update rejection

Request-attempt fencing prevents stale ownership for one logical request; it does **not** by itself order distinct valid requests. Every mutable current projection (`job_sources`, canonical presentation/availability, eligibility and score) therefore uses a comparable input revision/evidence order and conditional update or deterministic recomputation.

Minimum rules:

- retain all historical observations/evidence even when they are older;
- do not let an older observation/coverage generation overwrite a newer current projection because processing completed later;
- conflicting canonicalization uses serialization or compare-and-swap on the canonical/entity revision;
- eligibility/score rows identify the job content revision + profile/rules + evaluator versions they evaluated;
- profile edits create new evaluations without making old rows current;
- repeated/reversed completion order is idempotent.

## RUN-22. Application backup/restore referential contract

The database is not the whole application state. A backup generation manifest records hashes and capture status for every app-owned artifact required by retained database references (for example fixtures, snapshots/evidence retained by policy, auth metadata/state where portable under policy, recipes/resources). User documents outside the app-owned data root are recorded as external references unless the user explicitly chooses a copy/export mode.

Restore occurs only while the target service is stopped/isolated. It validates DB integrity, foreign keys, schema/application compatibility, artifact hashes and reference completeness before activation. Missing optional/external references are surfaced explicitly rather than silently treated as present. Ephemeral runtime locks/process markers are never restored.

