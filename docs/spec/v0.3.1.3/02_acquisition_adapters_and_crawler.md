# Windows Job Scraper v0.3.1.3 — Acquisition, Adapters & Crawler Specification

**Version:** Windows Job Scraper v0.3.1.3 — Final Corrected Modular Specification Set  
**Date:** 2026-09-05  
**Status:** **NORMATIVE — implementation specification**  
**Target:** Windows 11 · local-first · single-user  
**Core stack:** Python 3.12+ · FastAPI · SQLite + FTS5 · Playwright/Chromium

> This file is one member of the canonical modular specification set. Normative ownership is defined by `00_architecture_overview_and_authority.md`. If duplicated explanatory text conflicts with the normative owner, the normative owner wins.


## ACQ-00. Normative ownership

This file owns source classification, Source/Adapter/Binding semantics, adapter contracts, execution plans, strategy routing, page validity, crawling, recipes, Adapter Lab and binding health semantics.

Durable claims/rate/circuit persistence live in `03`. Security enforcement lives in `04`.

## ACQ-01. Corrected Source / Binding auth ownership

A `Source` describes the real-world target.

Authentication/execution requirements are binding-owned.

A source may legitimately expose:

```text
public structured binding
+
authenticated browser fallback
```

Therefore a source-level access field is descriptive policy/roll-up only.

Each active `SourceAdapterBinding` revision MUST own or reference its normative:

```text
auth_requirement
auth_scope_id
execution_class
strategy
permission_profile_id
permission_profile_revision
```

The adapter manifest declares supported capabilities/modes; it does not choose the actual authentication requirement for a particular source binding.

# 8. Source, adapter, and binding model

A source is not an adapter.

## 8.1 Source

A `Source` represents the real-world collection target:

- one remote feed;
- one ATS board;
- one company's careers site;
- one public board;
- one authenticated platform;
- one user-defined source.

Suggested fields:

```text
sources
-------
id
display_name
source_family
entry_url
canonical_host
source_access_policy  # descriptive roll-up only; runtime auth belongs to bindings
desired_state
administrative_state
robots_mode
terms_notes
created_at
updated_at
last_checked_at
```

`desired_state`:

```text
ENABLED
DISABLED
```

`administrative_state`:

```text
NORMAL
QUARANTINED
```

Quarantine is an administrative/policy control, not an operational-health value.

## 8.2 AdapterDefinition

An adapter definition is reusable source-specific logic.

Suggested identity:

```text
adapter_id
adapter_version
adapter_api_version
```

Example: one `greenhouse` adapter can serve thousands of company boards.

## 8.3 SourceAdapterBinding

A source may have multiple bindings:

```text
same source
├── provider/API binding
├── structured-page binding
├── HTTP HTML fallback
└── authorized-browser fallback
```

A binding has a stable logical identity and immutable revisions.

Suggested stable binding fields:

```text
source_adapter_bindings
-----------------------
id
source_id
display_name
desired_state
administrative_state
quarantined_at
quarantine_reason
current_revision_id
created_at
retired_at
```

`desired_state` is `ENABLED | DISABLED`. `administrative_state` is `NORMAL | QUARANTINED`.

Suggested immutable revision fields:

```text
source_adapter_binding_revisions
--------------------------------
id
binding_id
revision
adapter_id
adapter_version
strategy
priority
config_json
auth_requirement
auth_scope_id
execution_class
permission_profile_id
permission_profile_revision
created_at
superseded_at

UNIQUE(binding_id, revision)
```

Editing adapter version, strategy, priority, auth requirement/scope, execution class, permission profile, or binding config creates a **new revision**. Historical revisions remain resolvable for provenance and compatible resume.

Health is tracked primarily at:

```text
source + binding revision + strategy
```

The stable binding identity provides continuity across revisions, but operational evidence records the active `binding_revision_id`/adapter version so a repaired revision does not inherit stale parser-health conclusions blindly.

The UI derives a rolled-up source health from viable bindings.

A source MUST NOT hard-bind to one adapter/version in its primary record.

---


# 9. Adapter manifest, capabilities, permissions, and isolation

Every adapter has a schema-validated manifest.

Example:

```json
{
  "id": "greenhouse",
  "version": "1.2.0",
  "adapter_api_version": "1",
  "capabilities": [
    "discover",
    "listing_parse",
    "detail_parse",
    "incremental"
  ],
  "supported_execution_classes": ["HTTP"],
  "supported_auth_modes": ["NONE"],
  "cost_class": "LIGHT",
  "cursor_schema_version": 2
}
```

Do not encode host policy such as "rotate proxy on block" inside adapter manifests.

## 9.1 Permission profile

Adapter permissions are explicit:

```text
network_hosts / source host scopes
authenticated_session_access
browser_capability
snapshot_read
fixture_read
egress_modes
filesystem_access
subprocess_access
```

Defaults:

- arbitrary filesystem: denied;
- arbitrary subprocess: denied;
- arbitrary network: denied;
- authenticated state: denied unless binding grants it;
- browser: denied unless capability and binding permit it.


Permission profiles have stable identities plus immutable revisions:

```text
adapter_permission_profiles
---------------------------
id
display_name
administrative_state
current_revision_id
created_at
retired_at

adapter_permission_profile_revisions
------------------------------------
id
permission_profile_id
revision
policy_json
created_at
superseded_at

UNIQUE(permission_profile_id, revision)
```

A normal permission edit creates a new immutable revision. `RunSourcePlan`/`ExecutionPlanEnvelope` pins the revision used for reproducibility. Emergency administrative revocation on the stable profile remains a current-state deny and overrides historical pinning.

## 9.2 Adapter implementation tiers

### Declarative adapter

Preferred when behavior fits:

- URL/request templates;
- JSON/XML feeds;
- JSON path;
- JSON-LD;
- CSS/XPath/semantic locators;
- deterministic pagination;
- field mapping.

### Code adapter

Used for:

- complex source state decoding;
- non-trivial pagination;
- source-specific transformations;
- browser interaction planning;
- supported authenticated behavior.

Built-in audited adapters MAY run in-process **only if** they obey the host-owned I/O boundary.

Addon/third-party-style code adapters, if ever supported, MUST run in an isolated process with typed IPC and explicit permissions.

v0.3.1.3 does not require an open plugin marketplace.

---


# 11. ExecutionPlanEnvelope, RequestPlan, BrowserActionPlan, and ResultEnvelope

Every executor receives one common ownership/policy envelope plus one class-specific payload.

```text
ExecutionPlanEnvelope
---------------------
plan_id
request_id
attempt_id
run_id
run_source_plan_id
source_id
binding_id
binding_revision_id
adapter_id
adapter_version
strategy
execution_class
policy_snapshot_ref
permission_profile_id
permission_profile_revision
payload_kind
payload
```

The host creates and validates this envelope only after a durable request is claimed. The envelope identity is recorded with the attempt/result. Class-specific payloads cannot silently change binding/auth/policy identity.

## 11.1 RequestPlan

```text
RequestPlan
-----------
method
url
headers_without_secrets
body_ref_or_json
expected_content_types
allowed_redirects
timeout
max_bytes
cache_policy
revalidation_headers_allowed
auth_scope_ref
egress_requirement
purpose
operation_capability_ref
expected_operation_class
```

The executor attaches approved auth material; adapters do not receive raw secrets unless a narrowly defined capability requires it. `method`, body and destination are validated against a host-approved binding capability. Unknown state-changing HTTP operations are denied; an explicitly supported read-only search/filter POST may be allowed by capability.

## 11.2 BrowserActionPlan

Browser behavior is bounded and declarative where practical:

```text
BrowserActionPlan
-----------------
entry_url
auth_scope_ref
navigation_state
allowed_hosts
max_actions
max_runtime
actions[]
completion_guards[]
capture_requirements[]
approved_action_capabilities[]
expected_request_guards[]
```

Supported actions may include:

- navigate;
- wait for selector/state;
- click;
- scroll by bounded amount;
- select option;
- submit a user-authorized form needed for navigation **only through an approved read-acquisition capability**;
- capture DOM/structured payload evidence.

Arbitrary JavaScript execution is not a default adapter capability.

## 11.3 ResultEnvelope

All execution paths normalize to:

```text
ResultEnvelope
--------------
execution_plan_id
request_id
attempt_id
run_source_plan_id
source_id
binding_id
binding_revision_id
adapter_id
adapter_version
strategy
execution_class
requested_url
final_url
status_code
headers_redacted
content_type
body_ref / structured_payload_ref
body_hash
normalized_content_hash
fetched_at
duration_ms
bytes_downloaded
redirect_chain
transport
browser_used
robots_decision
validators_sent
was_304
resource_blocking_applied
failure
```

The parser does not need to know whether raw transport was implemented with `httpx`, Playwright, or a later compatible backend.

---


# 12. Source discovery, fingerprinting, and strategy routing

## 12.1 Fingerprint before specialized route

For an unknown company-career URL:

```text
URL
→ cheap fingerprint/classification
→ candidate ATS/source family
→ confidence + evidence
→ candidate bindings
→ strategy router
```

Fingerprint evidence may include:

- host/domain pattern;
- script URLs;
- iframe hosts;
- HTML markers;
- canonical links;
- JSON keys;
- public API paths;
- JSON-LD;
- known ATS endpoint patterns.

Output:

```text
family
confidence
evidence[]
recommended_adapter_id
```

Low-confidence fingerprinting MUST fall back to generic discovery rather than silently forcing a specialized adapter.

Discovery itself is not pre-queue I/O. Before the first probe, the host creates a provisional `Source`, a durable `SOURCE_DISCOVERY` request, and a built-in immutable generic discovery binding/revision (or equivalent typed `DiscoveryPlan`). The probe therefore has normal request/attempt provenance, budgets and security policy. Crash/restart resumes from that durable identity; later specialized bindings do not rewrite the probe history.

## 12.2 Strategy order

Default preference:

```text
PROVIDER_NATIVE
→ FEED_OR_PUBLIC_STRUCTURED_ENDPOINT
→ STRUCTURED_PAGE
→ HTTP_HTML
→ PLAYWRIGHT_PUBLIC
→ PLAYWRIGHT_AUTHENTICATED
→ MANUAL_UNSUPPORTED
```

`MANUAL_UNSUPPORTED` means:

> No supported automated route is available; show a clear manual action/status.

It must not mean "try an unspecified bypass."

## 12.3 Initial provider priorities

Early production adapters SHOULD include:

- Greenhouse;
- Lever;
- Ashby;
- retained reliable remote-job feeds.

Additional providers are graduated only after fixture and contract validation. Candidates include SmartRecruiters, Workable, Recruitee, Personio, Workday, BambooHR, Teamtailor, JazzHR, iCIMS, Jobvite, Breezy, Pinpoint, Eightfold, and SuccessFactors where practical.

An endpoint that merely worked once is not automatically a production contract.

---


# 14. Execution classes and capacity pools

Scheduler-visible execution classes:

```text
HTTP
BROWSER
BROWSER_INTERACTIVE
```

## HTTP

For:

- APIs;
- feeds;
- static HTML;
- ATS structured endpoints.

Characteristics:

- low memory;
- pooled connections;
- higher concurrency;
- no browser lifecycle.

## BROWSER

For:

- JavaScript rendering;
- client-side enumeration;
- browser-only page generation.

Characteristics:

- supervised process;
- lower concurrency;
- isolated context/page;
- resource accounting.

## BROWSER_INTERACTIVE

For:

- guarded clicks;
- filter interaction;
- infinite scroll;
- authenticated user-authorized flows.

Characteristics:

- lowest concurrency;
- strongest auditing;
- action count/runtime bounds;
- explicit cancellation.

HTTP and browser units MUST NOT share one undifferentiated thread/concurrency limit.

---


# 19. Crawl cursor and pagination contract

A `CrawlCursor` carries:

```text
source_id
binding_id
adapter_id
adapter_version
cursor_schema_version
state_json
checkpoint_at
```

It may represent:

- page;
- next URL;
- API token;
- offset;
- last source-native job ID;
- opaque adapter checkpoint.

Every adapter declares stop policy:

```text
max_pages
max_consecutive_empty_pages
max_consecutive_no_new_jobs_pages
max_duplicate_pages
max_runtime
max_requests
```

Loop/trap protection must detect:

- repeated next cursor;
- repeated next URL;
- repeated normalized page hash;
- repeated same job set;
- cyclic pagination;
- no-progress infinite scroll;
- same page under changing tracking parameters.

One empty page MUST NOT automatically mean terminal.

The system distinguishes:

- valid recognized empty source;
- parser empty;
- access challenge;
- HTTP failure;
- stale/parked page;
- repeated page.

---


# 20. Crawl scope, robots, sitemap, and policy

Every source has explicit crawl scope:

- allowed hosts;
- allowed path prefixes/patterns;
- deny patterns;
- max depth;
- max pages;
- max request count;
- max bytes;
- max runtime;
- page/detail budgets;
- execution-class budgets.

No source configuration may disable every safety bound.

Public crawl modes support visible robots-aware behavior and record the decision.

Sitemap support:

- sitemap discovery;
- sitemap indexes;
- `<lastmod>`;
- job/career path prioritization;
- selective enqueue of changed URLs.

Only the crawler behavior and contracts defined in this module are normative; historical research-tool internals are not part of the implementation contract.

---


# 21. Page Validity Classifier

Acquisition success is not equivalent to content validity.

Canonical states:

```text
VALID_LIST
VALID_JOB
JS_SHELL
LOGIN_REQUIRED
AUTH_EXPIRED
RATE_LIMITED
CHALLENGE_PAGE
EMPTY
NOT_FOUND
JOB_CLOSED
UNEXPECTED_REDIRECT
UNEXPECTED_CONTENT
UNKNOWN
```

Classifier evidence may include:

Ordinary redirect history is metadata in `ResultEnvelope.redirect_chain`; it is not mutually exclusive with `VALID_JOB` or `VALID_LIST`. `UNEXPECTED_REDIRECT` is reserved for a redirect whose destination violates the expected source/content contract while remaining within host security policy.


- HTTP status;
- final URL;
- content type;
- title;
- body size;
- visible text;
- structured data;
- expected source signatures;
- login markers;
- challenge markers;
- SPA shell indicators;
- recipe probes.

Extraction, adaptive repair, and recipe auto-promotion are prohibited on invalid page classes.

Critical invariant:

```text
login page
≠ parser failure
≠ empty jobs list
```

---


# 22. ExtractionRecipe

Recipes operate on already valid content.

Supported modes:

```text
HTML
JSON_LD
EMBEDDED_JSON
API_JSON
```

A recipe is versioned.

Each field may have ordered locator candidates.

Supported locator concepts:

- stable `data-*`;
- role/ARIA;
- relative CSS;
- CSS;
- relative XPath;
- XPath;
- href pattern;
- text anchor;
- JSON path;
- JSON-LD path;
- adaptive fingerprint metadata.

Absolute DOM paths should not be the sole primary locator.

Example:

```json
{
  "mode": "HTML",
  "card_locators": [
    {"kind": "data_attr", "value": "data-testid=job-card"},
    {"kind": "relative_css", "value": "article:has(a[href*='/job/'])"}
  ],
  "fields": {
    "title": {
      "required": true,
      "locators": [
        {"kind": "relative_css", "value": "h2,h3"},
        {"kind": "role", "value": "heading"}
      ]
    },
    "job_url": {
      "required": true,
      "locators": [
        {"kind": "href_pattern", "value": "/job/"}
      ]
    }
  }
}
```

Required-field failure does not invent values. It produces structured review/health evidence.

---


# 23. NavigationPlan

Navigation and extraction remain separate.

A NavigationPlan is a guarded state machine, not a blind macro.

Example:

```json
{
  "state": "LIST",
  "rules": [
    {
      "when": {"selector_exists": "#cookie-banner"},
      "do": [{"action": "click", "locator": "#accept"}]
    },
    {
      "when": {"page_class": "AUTH_EXPIRED"},
      "do": [{"action": "stop"}],
      "next_state": "NEEDS_LOGIN"
    },
    {
      "when": {"selector_exists": "button.load-more"},
      "do": [{"action": "click", "locator": "button.load-more"}],
      "next_state": "LIST"
    }
  ]
}
```

Actions are bounded by:

- allowed hosts;
- action count;
- elapsed time;
- browser-page count;
- cancellation;
- source policy.

---


# 24. Adapter Lab

Adapter Lab is a core maintainability surface.

It lives **inside** the adapter architecture:

```text
Source Adapter
├── manifest / capabilities / permissions
├── source bindings
├── versioned extraction recipe
├── versioned navigation plan
├── optional code parser/planner
├── fixture corpus
└── Adapter Lab
    ├── source/ATS detection
    ├── structured-data inspection
    ├── list auto-suggest
    ├── recorder
    ├── locator telemetry
    ├── navigation recording
    ├── authorized XHR/API discovery
    ├── API_JSON candidate
    ├── fixture validation
    ├── candidate promotion
    └── rollback
```

## 24.1 Teaching workflow

```text
Add Source
→ enter URL/name
→ choose Public or Authenticated
→ fingerprint ATS/source
→ inspect structured data
→ if sufficient: zero-click recipe candidate
→ otherwise select repeated card
→ select required fields
→ select optional fields
→ preview multiple records
→ teach navigation if needed
→ capture sanitized fixtures
→ validate
→ save candidate version
→ smoke test
→ activate
```

## 24.2 Authorized XHR/API discovery

Only during an explicit teaching session:

- observe same-session fetch/XHR response metadata;
- consider same-origin/same-site by default;
- require explicit approval for additional hosts;
- inspect bounded JSON locally for repeated job-like structures;
- match candidates against visible selected rows;
- propose `API_JSON`;
- require explicit confirmation before activation;
- reuse the authorized execution context when authentication is required;
- never export cookies/tokens with the recipe.

## 24.3 Snapshot teaching

Snapshots are network-inert.

A snapshot renderer MUST prevent remote images, styles, scripts, frames, or fonts from loading.

Authenticated snapshots receive stricter retention/redaction treatment.

---


# 25. Recipe/adapter versioning, promotion, and rollback

Recipe updates never silently overwrite the active version.

Promotion flow:

```text
active version
→ candidate
→ offline fixture tests
→ field-level diff
→ bounded live smoke if permitted
→ activate or reject
```

Promotion is transactional.

Rollback to a prior compatible version remains possible.

For adapter code:

```text
health degradation
→ evidence
→ repair candidate
→ fixture tests
→ bounded smoke
→ new adapter version
→ promotion for new runs
→ health recovery observation
```

Old adapter versions remain historically identifiable.

Remote adapter-pack updating is deferred. If later introduced, it requires signed manifests, cryptographic hashes, compatibility checks, staged activation, and rollback.

---


# 26. Fixture corpus

Maintained sources should have sanitized offline fixtures.

Minimum useful set:

- valid list;
- valid job;
- structured JSON/JSON-LD where applicable;
- empty source;
- closed job;
- auth-expired page where relevant;
- JS shell where relevant;
- challenge/rate-limit response where appropriate;
- changed template;
- multi-location example.

Fixtures are used for:

- adapter parser regression;
- recipe regression;
- classifier regression;
- promotion;
- rollback confidence;
- migration/upgrade acceptance;
- repair validation.

Live sites are smoke tests, not the primary regression suite.

---


# 27. Typed failure model

Failures are structured objects, not result strings.

Core kinds:

```text
DNS_ERROR
CONNECT_ERROR
TLS_ERROR
TIMEOUT
HTTP_4XX
HTTP_5XX
RATE_LIMIT
BLOCKED
CHALLENGE
AUTH_REQUIRED
CONSENT_INTERSTITIAL
PARSE_MARKER_MISSING
PARSE_EMPTY
PAGINATION_LOOP
DUPLICATE_PAGE
SOURCE_CHANGED
INVALID_JOB_RECORD
NORMALIZATION_ERROR
BROWSER_CRASH
WORKER_CRASH
LEASE_LOST
CANCELLED
POLICY_REJECTED
```

Failure record:

```text
kind
retryable
source_health_impact
http_status
source_id
binding_id
adapter_id
adapter_version
run_id
request_id
attempt_id
details_redacted
observed_at
```

`SUCCESS_EMPTY` is a successful recognized parse outcome and is therefore not a `FailureKind`.

---


# 28. Operational health model

Health belongs primarily to a source binding/strategy.

## 28.1 Operational health

```text
UNKNOWN
HEALTHY
DEGRADED
RATE_LIMITED
CHALLENGED
NEEDS_LOGIN
BROKEN
```

## 28.2 Administrative state

```text
NORMAL
QUARANTINED
```

## 28.3 Desired state

```text
ENABLED
DISABLED
```

These planes MUST NOT be conflated.

A source can be:

```text
desired = ENABLED
binding A API = HEALTHY
binding B browser = CHALLENGED
source rollup = HEALTHY_WITH_DEGRADED_FALLBACK
```

Example transitions:

```text
UNKNOWN → HEALTHY
HEALTHY → DEGRADED
DEGRADED → HEALTHY
HEALTHY/DEGRADED → RATE_LIMITED
RATE_LIMITED → DEGRADED/HEALTHY
HEALTHY/DEGRADED → CHALLENGED
CHALLENGED → DEGRADED/HEALTHY
DEGRADED → BROKEN
BROKEN → DEGRADED/HEALTHY after validated repair
any operational state + policy action → administrative QUARANTINED
QUARANTINED → NORMAL only by explicit administrative release
```

One zero-result run is never sufficient proof that a source is broken.

---


# 31. URL normalization and direct-link preservation

Store separately:

```text
discovery_url
raw_source_url
canonical_job_url
best_application_url
origin_url
```

Never overwrite the discovery path just because a better origin is found.

Normalize before URL-based identity comparison:

- decode HTML/JSON escapes;
- resolve relative URLs;
- lowercase scheme/host where appropriate;
- normalize safe default ports;
- remove fragments where non-semantic;
- remove only proven tracking parameters;
- preserve raw URL.

Identity preference:

1. source-native job/requisition ID;
2. ATS + company/board + requisition ID;
3. canonical detail URL;
4. canonical application URL where sufficiently job-specific;
5. structured content identity;
6. normalized company/title/location/time-window similarity.

A generic careers/home/apply URL alone MUST NOT cause an automatic merge.

---


# 32. Origin resolver

For aggregator or intermediate sources:

```text
observation
→ bounded redirect unwrap
→ tracking cleanup
→ ATS fingerprint
→ origin provider/board/job ID
→ direct application URL candidate
→ evidence-backed origin relation
```

Store:

```text
origin_provider
origin_board
origin_job_id
origin_url
origin_resolution_confidence
origin_resolution_evidence
origin_resolved_at
```

Origin resolution informs canonical source selection and dedup but does not destroy the original source link.

Public ATS resolution and exact application-URL preservation are encouraged; access restrictions are not bypassed to obtain them.

---


## ACQ-02. Corrected adapter protocol

The old `plan_discovery()`-only protocol is replaced by a task-oriented protocol capable of expressing health, enumeration, details and smoke work.

Conceptual contract:

```python
class AdapterTaskKind(Enum):
    HEALTH = "HEALTH"
    DISCOVER = "DISCOVER"
    ENUMERATE = "ENUMERATE"
    CRAWL = "CRAWL"
    DETAIL = "DETAIL"
    SMOKE = "SMOKE"

@dataclass(frozen=True)
class AdapterTask:
    kind: AdapterTaskKind
    payload: Mapping[str, Any]
    query_id: str | None = None

class SourceAdapter(Protocol):
    manifest: AdapterManifest

    def plan(
        self,
        task: AdapterTask,
        cursor: CrawlCursor | None,
        ctx: PlanningContext,
    ) -> RequestPlan | BrowserActionPlan:
        ...

    def parse(
        self,
        task: AdapterTask,
        result: ValidatedResultEnvelope,
        ctx: ParseContext,
    ) -> ParseOutcome:
        ...

    def next_cursor(
        self,
        task: AdapterTask,
        outcome: ParseOutcome,
        current_cursor: CrawlCursor | None,
        ctx: ParseContext,
    ) -> CrawlCursor | None:
        ...
```

Separate typed planning methods MAY be used instead, provided every supported task is explicit and the same host-owned-I/O boundary is preserved.


Durable acquisition request mapping is explicit:

```text
SOURCE_HEALTH_CHECK → HEALTH
SOURCE_DISCOVERY    → DISCOVER
LIST_FETCH          → ENUMERATE
SOURCE_CRAWL        → CRAWL
DETAIL_FETCH        → DETAIL
ADAPTER_SMOKE       → SMOKE
```

Pipeline tasks such as `NORMALIZE`, `RECONCILE`, `ENRICH`, `ELIGIBILITY`, `SCORE`, and `EXPORT` are host-native durable tasks and are not dispatched through the source-adapter planning protocol.

### Parser validity gate

A normal adapter parser receives `ValidatedResultEnvelope`, not an unchecked raw response.

Invalid classes such as:

```text
LOGIN_REQUIRED
AUTH_EXPIRED
RATE_LIMITED
CHALLENGE_PAGE
UNEXPECTED_CONTENT
```

must be handled by host policy/health/retry logic before normal observation extraction.

A diagnostic parser hook MAY inspect invalid content, but it MUST NOT emit normal job observations.

Recognized non-job outcomes map explicitly: `EMPTY` may support `SUCCESS_EMPTY` only for a successfully recognized enumeration whose authority rules are separately satisfied; `JOB_CLOSED` and `NOT_FOUND` produce typed closure/missing evidence where the binding declares that meaning, not fabricated normal observations; `UNKNOWN`/invalid classes cannot authorize absence.

## ACQ-03. Parse outcome model

Do not encode successful empty enumeration as a failure.

Recommended:

```text
ParseOutcomeKind
----------------
SUCCESS_WITH_JOBS
SUCCESS_EMPTY
PARTIAL
FAILURE
```

`SUCCESS_EMPTY` means the source was successfully recognized and genuinely contained no matching/listed jobs.

`PARSE_EMPTY` remains a failure when valid content was expected to contain parseable records but the parser unexpectedly produced none.


`PARTIAL` has strict semantics:

- the result was valid/recognized and at least one valid observation or deterministic child task may be persisted;
- the intended enumeration/parse unit did not complete sufficiently to claim authoritative coverage;
- persisted observations use deterministic request-scoped idempotency keys;
- enumeration coverage is recorded as `PARTIAL`;
- absence inference is forbidden for that coverage record;
- host policy either schedules a deterministic continuation/retry or accepts the bounded partial result;
- a retry MUST NOT duplicate already committed observations/child work.

A `PARTIAL` parse is therefore neither equivalent to `SUCCESS_EMPTY` nor a blanket `FAILURE`.

## ACQ-04. Detail-child planning

A parse result may emit typed child tasks rather than raw executable URLs:

```text
DiscoveredTask
--------------
kind = DETAIL | ENUMERATE | CRAWL
logical_key
target_reference
priority
depth
parent_observation/reference
```

The adapter planner converts that typed task into an execution plan, after which the host validates it.

This preserves the rule:

> discovered URL/reference ≠ permission to perform I/O.

## ACQ-05. Binding health dimensions

Keep scalar operational state **and** health dimensions.

Operational state:

```text
UNKNOWN
HEALTHY
DEGRADED
RATE_LIMITED
CHALLENGED
NEEDS_LOGIN
BROKEN
```

Recommended health dimensions:

```text
CONNECTIVITY
AUTH
CONTENT_VALIDITY
NAVIGATION
EXTRACTION
RATE_LIMIT
DATA_QUALITY
```

A binding may therefore be:

```text
overall = DEGRADED
CONNECTIVITY = HEALTHY
AUTH = HEALTHY
CONTENT_VALIDITY = HEALTHY
EXTRACTION = BROKEN
```

which prevents parser drift from being misdiagnosed as a network/authentication failure.


Host/runtime dimensions such as `QUEUE`, `WORKER`, `BROWSER_SUPERVISOR`, and `DATABASE` are diagnosed separately. A queue/worker failure normally has `source_health_impact = NONE` unless independent source-specific evidence also exists.

### Source roll-up

Source roll-up is derived from binding identities whose `desired_state = ENABLED` and `administrative_state = NORMAL`, using their current viable revisions and operational-health evidence. A source-level quarantine also excludes all bindings from normal execution.

Example derived labels:

```text
HEALTHY
HEALTHY_WITH_DEGRADED_FALLBACK
DEGRADED
NEEDS_LOGIN
UNAVAILABLE
UNKNOWN
```

The implementation must document deterministic roll-up precedence.

## ACQ-06. Browser action side-effect restriction

Acquisition plans may submit only a user-authorized form required for **navigation/filter/login/consent state used to reach authorized content**.

They MUST NOT submit:

- a job application;
- recruiter outreach;
- purchase/order;
- account setting change;
- destructive action;
- any comparable business transaction.

This restriction is independently enforced by security policy, not trusted solely to adapter convention. The host validates the **resulting network operation**, not only an adapter action label: expected destination/method/operation guards are installed before the action, and unexpected write-like requests are denied. The same read-acquisition rule applies to direct HTTP `RequestPlan` execution.

## ACQ-07. Strategy escalation

HTTP→browser escalation is a new typed logical task/attempt, not an identity-evasion loop.

The same target URL may legitimately be executed under two strategies when the first strategy demonstrates a supported technical need such as a JavaScript shell.

The durable request identity rules in `03` must therefore distinguish logical purpose/strategy where needed.

## ACQ-08. Import/activation boundary

Adapter/recipe import must satisfy the validation rules owned by `04_security_and_authentication.md`.

No imported declarative source config gains executable-code privilege merely because it is syntactically valid. Import also enforces bounded file/document size, nesting/depth, item count and total extraction cost. XML/JSON/YAML or comparable structured formats use safe non-executing parsers with external-entity/code/object construction disabled.

## ACQ-09. Versioned cross-component data contracts

Before a task kind is implemented across component/process boundaries, its data shape is versioned and fixture-tested. At minimum the contract set defines:

```text
PlanningContext
---------------
contract_version
run_id
run_source_plan_id
source_snapshot_ref
binding_revision_id
permission_profile_revision
policy_snapshot_ref
query_revision_ref
budget_snapshot_ref

ValidatedResultEnvelope
-----------------------
contract_version
result_envelope_ref
validated_page_class
validation_evidence_ref
security_policy_result
cache_representation_ref

ParseContext
------------
contract_version
request_id
attempt_id
run_source_plan_id
parser/recipe version
normalization contract version
request-scoped idempotency namespace

ParseOutcome
------------
contract_version
kind = SUCCESS_WITH_JOBS | SUCCESS_EMPTY | PARTIAL | FAILURE
observations[]
closure_or_missing_evidence[]
discovered_tasks[]
cursor_proposal
coverage_proposal
continuation_required
failure
```

Contract fixtures cover `HEALTH`, `DISCOVER`, `ENUMERATE`, `CRAWL`, `DETAIL` and `SMOKE`, including valid empty, closed, missing, partial and terminal-no-further-work cases. Host-native durable tasks (`NORMALIZE`, `RECONCILE`, `ENRICH`, `ELIGIBILITY`, `SCORE`, `EXPORT`) use an explicit non-network dispatcher and cannot gain source-network authority merely because they share queue mechanics.

A parser reports closure or successful health through typed outcome/evidence; it does not invent a normal `JobObservation` to communicate non-job state.
