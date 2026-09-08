# Slice 2 Worker Implementation Plan — v0.3.1.3

Status: APPROVED EXECUTION PLAN — decomposition of ROAD-03 per ROAD-13 discipline.
Authority: `docs/spec/v0.3.1.3/` (normative set), especially
`00` (ARC-05/08/10/11/14), `01` (§33/§34/§36/§37/§38/§39/§45),
`02` (§8/§9/§11/§12/§19/§21/§27/§29/§30/§31/§32, ACQ-02…ACQ-09),
`03` (§29/§30/§40/§51/§52, RUN-05/11/12/13/14/14A/15/17/18/21),
`04` (§5.1, SEC-03/04/08/09), `06` (#59/#61/VER-04/VER-10/VER-11/VER-13/VER-14).
Precondition: Slice 0 PROMOTED + Slice 1 ACCEPTED
(`docs/reviews/slice-0-1-native-promotion-closure-2026-09-08.md`).
Accepted starting HEAD: `fa5b63979055bb3aef864571d3f8783464ea4b9e`.

Goal (ROAD-03): structured acquisition breadth on the **already-final**
architecture — nothing is re-plumbed:

```text
Source → BindingRevision → RunSourcePlan → host-owned execution
→ ResultEnvelope → Page/Result Classifier → adapter parse
→ Observation + evidence → canonical pipeline (normalize → entity resolution
→ job_sources presence → canonical presentation/provenance selection)
→ companies + multi-location + cleaned content → FTS5 → Inbox/search result
```

Ship condition: several browser-light structured sources (Greenhouse, Lever,
Ashby) run through the same Source/Binding/RunSourcePlan/Observation
contracts, with ATS fingerprinting, a strategy router, richer
ResultEnvelope/evidence, an origin resolver, companies, multi-location,
deterministic content cleaning, FTS5 search and canonical provenance
selection.

## 0. Non-negotiable Slice 2 rules

1. **No new write path.** Adapters plan and parse only. They never perform
   uncontrolled network I/O, never open a DB connection, and never write
   `jobs`/`job_sources`/canonical state. Canonical state is produced only by
   the existing fenced pipeline (`pipeline/ingest.py` + `pipeline/canonical.py`
   + `pipeline/obligations.py`) under the request fence (ROAD-02 prohibition,
   ARC-04.4, ACQ-02/ACQ-04).
2. **Security boundary unchanged.** Every fetch still goes through
   `acquisition/envelope.validate_envelope` + `net/destination` per-connection
   validation, per-hop redirect revalidation, byte/duration caps, header
   redaction, and the host-owned narrow loopback grant rule in
   `pipeline/driver.source_policy`. Scraped/imported content can never
   construct a grant or widen an allowed host (04 §5.1, SEC-09).
3. **Provenance is immutable and complete.** Observations stay immutable;
   richer evidence is appended, never retro-mutated. Revalidation/fingerprint/
   origin/route records are evidence, not current-state overrides.
   `discovery_url` is never overwritten because a better origin was found
   (02 §31/§32).
4. **Idempotency/fencing preserved.** New child work (DETAIL) and new
   per-request outputs use the existing deterministic
   `request_unique_key`/`observation_unique_key` machinery; a retry never
   duplicates evidence or canonical rows (RUN-05, 03 §29, RUN-08).
5. **Migrations are append-only.** Released step bytes are pinned and
   immovable; Slice 2 adds steps `v11+` only (RUN-17, S0/S1 discipline).
6. **Fingerprint confidence gates routing.** Low-confidence fingerprinting
   MUST fall back to generic discovery and MUST NOT silently force a
   specialized adapter (02 §12.1). `MANUAL_UNSUPPORTED` never means "try an
   unspecified bypass" (ARC-08/09).
7. **Capability honesty.** FTS5 is used when present; when absent the product
   falls back to substring search with an explicit capability warning and never
   claims BM25/FTS is active (01 §45).
8. **No Slice-3+ scope.** No durable frontier/capacity coordinator expansion, no
   generic HTTP crawler/sitemap breadth, no Adapter Lab/recipes, no
   browser/authenticated sources, no scheduler, no contacts/exports, no
   merge-UI/undo flow, no revalidation/cache rework beyond what Slice 2 itself
   consumes.
9. `SCHEMA_VERSION`/`LATEST_SCHEMA_VERSION` move together; Slice 0/1 gates
   (`scripts/verify_slice0.py`, `scripts/verify_slice1.py`) stay green.

## 1. Work packages

| Pkg | Scope | Primary contract surfaces | Ship artifact |
|---|---|---|---|
| S2.0 | Baseline verification, this plan, Slice-2 contract/acceptance skeleton; re-scope Slice-1 *boundary* pins that Slice 2 intentionally supersedes | `tests/contract/test_slice2_contract.py` | plan + green skeleton |
| S2.1 | Richer `ResultEnvelope`/evidence + ACQ-09 contract fields + origin resolver (02 §11.3/§32, ACQ-09) | `acquisition/result.py`, `acquisition/origin.py`, `acquisition/atsendpoints.py`, `adapters/contract.py`, migration **v11** | unit+integration |
| S2.2 | Company model, multi-location, canonical provenance selection + origin-identity dedup stage (01 §33/§38/§39, 03 RUN-11/12/14A/15) | `pipeline/companies.py`, `pipeline/locations.py`, `pipeline/provenance.py`, migration **v12** | unit+integration |
| S2.3 | Deterministic content cleaning + FTS5 search (01 §34/§45, 06 #59) | `pipeline/contentclean.py`, `search/`, migration **v13** | unit+integration |
| S2.4 | ATS fingerprinting + strategy router + low-confidence safe fallback (02 §12.1/§12.2, ARC-08) | `adapters/fingerprint.py`, `adapters/router.py`, `runtime/provisioning.py`, migration **v14** | unit+integration |
| S2.5 | Greenhouse adapter + deterministic fixtures + full E2E through Source/Binding/RunSourcePlan/Observation | `adapters/greenhouse.py`, `tests/fixtures/greenhouse/`, driver generalization | integration + E2E |
| S2.6 | Lever adapter + fixtures | `adapters/lever.py` | integration |
| S2.7 | Ashby adapter + fixtures | `adapters/ashby.py` | integration |
| S2.8 | Cross-provider acceptance: fingerprint → route → acquire → observation → company/location/provenance → cleaned/indexed searchable result | `tests/integration/test_slice2_acceptance.py` | E2E gate |
| S2.9 | Slice-2 automated gate, migration verification from the accepted Slice-1 schema, packaging/native preparation, corrective review, status record | `scripts/verify_slice2.py`, `docs/reviews/slice-2-*.md` | report |

**REQUIRED PAUSE after S2.5.** S2.6+ starts only on explicit `continue`.

### Execution discipline

TDD per package: failing test first for behavior-bearing code → implement →
focused tests → full regression (`tests/unit tests/contract tests/integration`)
→ one coherent commit per package. Never weaken a security/recovery invariant
to make a test pass.

## 2. Package details

### S2.0 — baseline, plan, contract/acceptance skeleton

* Verify the working tree is clean at the accepted base and that the accepted
  HEAD is an ancestor of the Slice-2 branch.
* Add `tests/contract/test_slice2_contract.py` with the durable Slice-2
  invariants (not package-by-package assertions):
  - adapters never touch the DB or the network (no `sqlite3`, no
    `urllib`/`http.client`/`socket`/`httpx`, no `INSERT`/`UPDATE`);
  - only `pipeline/` writes `INSERT INTO jobs`;
  - no canonical write outside the fence (`fenced_commit` remains the only
    terminal commit for request-owned outputs);
  - every built-in adapter is registered in `adapters/registry.BUILTIN_ADAPTERS`
    and has a validated manifest + an `adapter_definitions`-compatible
    identity; no dynamic import path;
  - migration discipline: sequential versions, released bytes pinned across
    Slice-1 **and** Slice-2 pin tables, `LATEST_SCHEMA_VERSION == SCHEMA_VERSION`;
  - destination policy is still derived only from the source entry host, and
    no Slice-2 module may construct an `InternalGrant`;
  - Slice-2 must not create Slice-3+ objects (recipes, navigation plans,
    fixtures-as-UI, contacts, exports, scheduler, frontier/lease expansion).
* Re-scope (do not delete) the two Slice-1 *boundary* pins that Slice 2
  legitimately supersedes — `test_no_fts5_or_search_expansion` and
  `test_single_builtin_adapter` — into Slice-2-aware form, keeping every
  Slice-1 architectural invariant (provenance-first, security shell, route
  gating, dependency lock discipline, migration pinning) exactly as-is.
* Skeleton `scripts/verify_slice2.py` is **not** added here; it is the S2.9
  deliverable so the gate is written against the finished surface.

### S2.1 — richer ResultEnvelope/evidence + origin resolver

`ResultEnvelope` (02 §11.3) gains the missing §11.3 members and the evidence
plane:

* `body_ref`, `structured_payload_ref` (retained representation identities;
  bodies are not duplicated into every consumer), `evidence_refs`,
  `security_policy_result`, `cache_representation_ref`, `contract_version`;
  existing fields unchanged.
* `ValidatedResult` becomes the ACQ-09 `ValidatedResultEnvelope` shape:
  `contract_version`, `result_envelope_ref`, `validated_page_class`,
  `validation_evidence`, `security_policy_result`, `cache_representation_ref`,
  with the *unchecked* `ResultEnvelope` still rejected by the parse gate.
* `ParseOutcome` gains `closure_or_missing_evidence[]` (ACQ-09) and
  `evidence_refs[]`; a parser still cannot emit normal observations from an
  invalid class (ACQ-02).
* `acquisition/atsendpoints.py` — one versioned, data-only ATS endpoint table
  (host patterns + endpoint shape + job/board id extraction pattern +
  canonical application-link template) shared by the origin resolver and (in
  S2.4) the fingerprint classifier. It is the single owner of "known ATS
  endpoint patterns" (02 §12.1 evidence list).
* `acquisition/origin.py` — origin resolver (02 §32) implemented **without any
  I/O of its own**: it consumes the observation candidates plus the already
  recorded `redirect_chain`/`final_url`, applies tracking cleanup through
  `net/urlnorm` (proven-parameter list stays versioned), extracts
  `origin_provider`/`origin_board`/`origin_job_id`, proposes the direct
  application URL candidate, and returns confidence + evidence. Below
  threshold ⇒ explicitly `UNRESOLVED` (never a guess).
* Migration **v11**: richer evidence columns on `fetch_attempts`/
  `parse_attempts` and origin-resolution outcome on `job_sources`
  (`origin_provider`, `origin_board`, `origin_job_id`,
  `origin_resolution_confidence`, `origin_resolution_evidence_json`,
  `origin_resolved_at`) + the append-only `acquisition_evidence` evidence
  spine (`03` §30). Canonical `jobs.origin_*` (03 §52) is rolled up from the
  selected provenance in S2.2, never written by a collector.

As implemented (S2.1, deviations recorded deliberately):

* `PlanningContext` and `ParseContext` live in `adapters/contract.py` — the
  single versioned adapter-contract module (`CONTRACT_VERSION =
  PARSE_CONTRACT_VERSION = 2`) — rather than a new `acquisition/contract.py`,
  so the adapter protocol has exactly one owner.  The driver constructs both
  contexts; adapters receive them read-only.
* `ValidatedResultEnvelope` is the real type and `ValidatedResult` remains an
  alias, so accepted Slice-1 call sites and fixtures stay valid.  The ACQ-02
  gate is `__post_init__`: *constructing* the validated envelope with any page
  class outside `NORMAL_PARSE_CLASSES = {VALID_LIST, VALID_JOB, EMPTY}` raises,
  which makes "parser handed unvalidated content" unrepresentable instead of
  merely discouraged.
* `ResultEnvelope.finalize()` (called by `httpexec.finish_and`) derives
  `body_hash`, `normalized_content_hash`, `body_ref`, `structured_payload_ref`.
  Content-class normalization: JSON re-serialized with sorted keys, HTML
  stripped of scripts/styles/tags with whitespace collapsed, anything else (or
  unparseable content) hashed over raw bytes — "we could not normalize" must
  never look like "nothing changed".
* Migration v11 does **not** re-add `fetch_attempts.body_ref` (v6) or
  `job_sources.origin_url` (v7): the latter stays reserved for canonical URL
  selection (01 §39), so the resolver never overloads it and the resolved URL
  is carried in `origin_resolution_evidence_json` plus the
  `acquisition_evidence` row.
* Origin resolution is pure over already-recorded evidence
  (`redirect_chain_json`, `final_url`, the observation's own link candidates).
  A durable bounded unwrap loop is ROAD-04 / Slice 3, not Slice 2.
  `origin_url` is taken from the *highest-priority* matching candidate
  (`canonical_job_url → application_url → final_url → recorded hops →
  raw_source_url → discovery_url`); additional matches raise confidence
  (0.95 corroborated) but never silently re-point the recorded URL.
  Below `ORIGIN_CONFIDENCE_THRESHOLD = 0.75` (or on provider conflict) the
  resolution is `UNRESOLVED` and **all** `origin_*` presence columns stay
  NULL; the partial signals survive only as evidence.  Persistence is
  `COALESCE`-based, so a later unresolved sighting can never erase an earlier
  resolved origin (RUN-21 direction).
* The driver constructs a `PlanningContext` for `adapter.plan` and a
  `ParseContext` for `parse`/`next_cursor` (the latter carries the
  request-scoped `idempotency_namespace`).  Fields whose durable store does
  not exist yet (`policy_snapshot_ref`, `budget_snapshot_ref`,
  `query_revision_ref`) are passed as `None` rather than fabricated: Slice 3
  owns frontier/budget snapshots, Slice 4 owns query revisions.
* `ParseOutcome.evidence_refs` is accepted and persisted
  (`parse_attempts.evidence_refs_json`), as is `closure_or_missing_evidence`;
  the Slice-1 `json_api_feed` parser legitimately reports empty lists on both,
  and S2.5's provider parser is the first producer.

* Slice-1 schema tests were re-scoped, not weakened: `v1..v10` byte pins
  remain in `test_schema_slice1.py` **and** are re-pinned by
  `test_schema_slice2.py::test_slice1_released_bytes_are_untouched`, while the
  "head equals 10" exactness moved into the executing slice's own schema test
  (Slice 2 now pins `LATEST_SCHEMA_VERSION == 11` and requires every appended
  step to be registered exactly once).  Both `verify_slice0.py` and
  `verify_slice1.py` pass unchanged at v11.

### S2.2 — companies, multi-location, canonical provenance selection

* `pipeline/companies.py` — company resolution per 01 §33.1. Signals:
  normalized name, domain, application-URL host, ATS board, careers URL,
  structured organization metadata. **Weak evidence never merges**: an
  attach requires a strong signal (same registrable domain, or same
  `(ats_provider, ats_board)`, or same normalized name *plus* compatible
  host). Resolution decisions are recorded as evidence
  (`company_resolution_events`, non-destructive).
* `pipeline/locations.py` — multi-location normalization into `job_locations`
  (01 §33.2): `raw_text`, `country`, `region`, `city`, `remote`,
  `timezone_min/max`, `source_location_id`, `confidence`. Deterministic
  versioned rule set for ATS location objects, `location` strings
  ("Berlin, Germany / Paris, France"), `applicantLocationRequirements` and
  `jobLocationType`. The canonical model keeps **no single `location_text`**;
  presentation derives from the set.
* `pipeline/provenance.py` — canonical provenance selection (01 §39) replaces
  the Slice-1 strategy proxy with the normative class ordering
  `EMPLOYER_STRUCTURED_ATS/API → EMPLOYER_CAREERS_PAGE →
  AGGREGATOR_WITH_RESOLVED_ORIGIN → AGGREGATOR_WITHOUT_RESOLVED_ORIGIN`,
  tie-broken by strategy quality then evidence recency, then by stable id
  (deterministic). This controls presentation only: all presences and links
  remain inspectable.
* Entity resolution gains the normative **stage 2** merge
  (`origin_provider, origin_board, origin_job_id`) and keeps the reuse guard
  (01 §38 stage 2, RUN-15): a shared origin identity attaches presences to the
  same canonical job only when company/title/time-window evidence is
  compatible; otherwise it is recorded for review and kept separate.
  Meaningful location disagreement must not merge (01 §38 stage 5).
* Migration **v12**: `job_sources.source_quality_class`, presence origin
  quality inputs, `company_resolution_events`, `company_identifiers`
  (strong-signal index), `job_locations` lookup index, `jobs` canonical
  presentation columns the projection needs (`location_summary` is
  deliberately **not** added; `remote_mode`/`remote_worldwide`/
  `employment_type`/`experience_level` are projected from evidence).

As implemented (S2.2, deviations recorded deliberately):

* `pipeline/locations.py` owns `location-rules-v1` (strings, ATS dicts,
  Greenhouse `applicantLocationRequirements`, Lever `jobLocationType`) and the
  DB projection `project_job_locations`.  Unrecognized places are kept at
  confidence 0.3, never dropped; no country is inferred from a bare city and
  no UTC offset is recorded unless the evidence carried an IANA zone (offsets
  are resolved against the recorded observation instant).  `remote_worldwide`
  requires *explicit* worldwide wording — a bare `"Remote"` leaves
  `remote_mode=REMOTE` with `remote_worldwide=0`, which is what keeps
  eligibility's "no country proven" case honestly `LIKELY` (Slice-1 semantics
  preserved deliberately, not silently redefined).
* `pipeline/companies.py` owns `company-resolution-v1`: strong identifiers
  only (`ATS_BOARD` = `PROVIDER/board`, `APP_HOST`, `ORG_DOMAIN`,
  `CAREERS_HOST`), a company row's own `domain` is the **employer's** host
  (ATS platform hosts are recorded as identifiers, never as the company
  domain), a bare normalized name never attaches (it creates a new company
  with reason `WEAK_EVIDENCE_NO_MERGE`), a strong attach with a differing
  display name records `NAME_CONFLICT_REVIEW` instead of renaming anyone, and
  missing company fields are filled with `COALESCE` only.  Every decision
  lands in `company_resolution_events` + `company_identifiers`.
  No public-suffix list is added (no new runtime dependency): the host key is
  the full lowercased host minus a leading `www.`, so sibling subdomains do
  *not* merge — the conservative direction, recorded as a deferral.
* `pipeline/provenance.py` is now the single owner of §39 ordering
  (`canonical-provenance-selector-v2`); `canonical.select_canonical_provenance`
  delegates to it so the accepted Slice-1 import keeps working with one
  implementation.  The class is computed once per presence from recorded
  evidence (`content_kind`, employer-side host match, §32 origin status) and
  stored on `job_sources`, so selection is durable and replayable rather than
  re-derived.  `canonical.select_canonical_provenance` delegating alias keeps
  the accepted Slice-1 import valid while `pipeline/provenance.py` stays the
  single ordering implementation.
* Employer-vs-aggregator is decided by: the operator's `source_family`
  declaration (`EMPLOYER_CAREERS`, `EMPLOYER_SITE`, `EMPLOYER_API`,
  `ATS_BOARD`, `ATS_PROVIDER_API` — the spec leaves the family vocabulary
  open, so this set is the product's), or the posting link living on the
  source's own host **and** §32 not locating the posting's real home
  elsewhere.  A self-hosted board that mirrors an ATS posting is therefore an
  aggregator with resolved origin, never employer quality.
* §38 stage 2 (shared resolved origin identity across sources) attaches the
  new source's presence to the existing canonical job when company/title/
  location evidence is compatible, and refuses on a meaningful location
  disagreement — recorded as `entity_resolution_events.reason_code =
  LOCATION_DISAGREEMENT` with the guard evidence attached.  Cross-source
  stages never merge two canonical jobs, so no merge ledger is required here;
  the reversible merge/undo workflow (and its `job_merges` table) stays with
  its own later slice.
* Migration v12 stores the §39 quality inputs per presence, `company_
  identifiers`, `company_resolution_events`, the `job_locations(country, city)`
  lookup index, and three projection version columns on `jobs`
  (`location_rules_version`, `company_resolution_version`,
  `provenance_selector_version`).  `location_summary` was deliberately *not*
  added: the canonical model keeps the set.
* The location set and categorical fields are re-projected only when the fresh
  presence owns the presentation (`winner.id == fresh_presence_id`), using the
  existing `LOCATION_CHANGED` change class — a lower-quality or older arrival
  cannot churn canonical state (RUN-21).
* Slice-2's schema harness was strengthened: appended steps are now verified by
  **digest**, not only by key presence (a stale pin had slipped past the
  S2.0 harness when v12 gained a column).

### S2.3 — deterministic content cleaning + FTS5

* `pipeline/contentclean.py` — `clean(raw) → (markdown, text, lang, hash)`
  behind `CONTENT_CLEANING_VERSION`. Deterministic and idempotent
  (clean(clean(x)) == clean(x)), HTML-entity aware, strips script/style and
  event-handler residue, keeps structural Markdown, neutralizes executable URL
  schemes inside retained links, drops tracking parameters from retained
  links, and records the cleaning version on the canonical row so a later
  cleaner revision never silently rewrites history.
* `search/` — FTS5 index over `title`, company, `description_text`,
  locations and available fact text with BM25 relevance baseline (01 §45);
  structured filters (salary, eligibility, listing status, disposition,
  application status, date, source, company, profile score) stay **outside**
  FTS. Index maintenance is host-owned, idempotent and revision-checked
  (`job_search_state.indexed_content_hash`), so re-indexing never duplicates
  or loses rows.
* Capability honesty: `search_capability` records `FTS5_ACTIVE` vs
  `SUBSTRING_FALLBACK` (+ warning). Search responses always carry the active
  mode. Doctor reports the index state; the packaged Windows build already
  probes FTS5.
* Migration **v13**: `job_search_docs`, `job_search_state`,
  `search_capability`; the FTS5 virtual table + triggers are provisioned by
  capability-gated idempotent code (never by an unconditional migration step),
  so a non-FTS5 host still migrates cleanly and reports the fallback.
* `GET /api/search` (session-gated, read-only) exposes the query, mode,
  filters and hits; new routes live in `service/s2_routes.py` so Slice-1's
  route contract stays exact, and Slice-2 gates them with the same
  session+CSRF dependencies.

### S2.4 — ATS fingerprinting + strategy router

* `adapters/fingerprint.py` — deterministic, evidence-first classifier
  (02 §12.1) over already-acquired content only: host/domain pattern, script
  URLs, iframe hosts, HTML markers, canonical links, JSON keys, public API
  paths, JSON-LD and known ATS endpoint patterns. Output
  `family, confidence, evidence[], recommended_adapter_id` with a versioned
  rule table. No I/O, no DB.
* `adapters/router.py` — strategy router: candidate specialized bindings from
  the fingerprint, ordered by the ARC-08 preference
  (`PROVIDER_NATIVE → FEED_OR_PUBLIC_STRUCTURED_ENDPOINT → STRUCTURED_PAGE →
  HTTP_HTML → PLAYWRIGHT_PUBLIC → PLAYWRIGHT_AUTHENTICATED →
  MANUAL_UNSUPPORTED`), filtered by what the host can actually enforce
  (Slice 2 supports `HTTP` execution class only — a browser-class candidate is
  reported `UNSUPPORTED_EXECUTION_CLASS`, never silently substituted).
  Low confidence (< `ROUTE_CONFIDENCE_THRESHOLD`) ⇒ **no specialized route**;
  the decision records `GENERIC_DISCOVERY_FALLBACK` and keeps the
  discovery-probe identity intact.
* `runtime/provisioning.py` — host-side, idempotent provisioning of the
  immutable objects the router decided on: built-in `adapter_definitions`,
  `Source`, `SourceAdapterBinding` + `BindingRevision` (config snapshot,
  strategy, execution class, permission pin) and the fingerprint/route
  decision evidence rows. Adapters never write these rows.
* Migration **v14**: `ats_fingerprints`, `source_route_decisions` (+
  candidate/decision indexes). Both are append-only evidence.

### S2.5 — Greenhouse adapter + fixtures + end-to-end path

* `adapters/greenhouse.py` — built-in code adapter for the public Greenhouse
  board API: `ENUMERATE` over `/v1/boards/{board}/jobs[?content=true]`,
  optional typed `DETAIL` child tasks (ACQ-04) for per-job content when the
  binding does not enable inline content, and `DETAIL` parsing into
  observations (title, source-native id, locations/offices, departments,
  `updated_at`/posted time, cleaned content, `absolute_url`). Detail targets
  are built from the pinned board token, **never** from arbitrary URLs found
  in page content.
* Deterministic sanitized fixtures under `tests/fixtures/greenhouse/`: valid
  list, valid job, empty board, closed/removed job, multi-location, changed
  template, malformed/required-field-missing, and a rate-limit/challenge
  shape — used for parser, classifier and provenance regression (02 §26).
* E2E through the existing contracts only: `Source` → `BindingRevision` →
  `RunSourcePlan` → host execution against a loopback fixture server →
  `ResultEnvelope` → validity gate → parse → `JobObservation` → canonical
  job + company + locations + cleaned content + FTS row + provenance
  selection → Inbox-visible job with a safe direct apply link; plus idempotent
  re-run, restart recovery with no duplicate observations, and typed
  failure/classification behavior for challenge and 429 shapes.
* `pipeline/driver.py` generalization (required, architecture-preserving):
  registry-based built-in adapter construction (config from the pinned
  binding revision) and the ACQ-02 request-type↔task mapping
  (`LIST_FETCH→ENUMERATE`, `DETAIL_FETCH→DETAIL`, `SOURCE_HEALTH_CHECK→HEALTH`,
  `SOURCE_DISCOVERY→DISCOVER`, `SOURCE_CRAWL→CRAWL`, `ADAPTER_SMOKE→SMOKE`);
  the run cannot terminalize while its own accepted child work is still open.

As implemented (S2.5, deviations recorded deliberately):

* `adapters/greenhouse.py` is registered as the second built-in adapter
  (`greenhouse` 1.0.0, `HTTP` only, auth `NONE`, `listing_identity_sufficient
  = True`) and is constructed **only** through `adapters/registry.py`'s new
  `build_adapter(adapter_id, config)`; `pipeline/driver.py` no longer names any
  adapter class (pinned by `test_driver_selects_adapters_only_through_the_registry`).
  Endpoint shapes come from `acquisition/atsendpoints.py` — the adapter
  hardcodes no provider host (pinned by
  `test_provider_endpoint_shapes_come_from_the_versioned_table`), so the
  fingerprint classifier, the origin resolver and the adapter cannot disagree
  about what a Greenhouse URL is.
* `GreenhouseConfig` is fail-closed and validated at construction: `board`
  (`[a-z0-9][a-z0-9_-]{0,63}`, control characters refused, reserved route words
  refused — it is interpolated into a URL path), `api_base_url` (bare http(s)
  origin: scheme + host [+ port], no path/query/fragment/credentials),
  `include_content`, `detail_fetch`, `max_detail_requests` (≤ the adapter stop
  policy), `company_name`, `careers_url`, and host caps `timeout_s ≤ 30` /
  `max_bytes ≤ 2 MiB` that binding config may lower but never raise.  Unknown
  config keys are refused, not ignored.
* Detail targets are built from the pinned board token plus a **validated
  source-native numeric id** (`^\d{1,20}$`, no stripping, no coercion).  Only
  `target_reference` is read from the task payload: a board, host or URL
  appearing in content is ignored by construction, and a traversal-shaped id
  (`../../etc/passwd`) survives only as bounded review evidence — never as a
  fetch target or an identity.
* **Deviation (deliberate, stricter than the package text):** `posted_at` is
  derived only from the provider's own *publication* stamps
  (`first_published_at` → `job_post_information.date_published`).  The package
  text listed "`updated_at`/posted time"; mapping a last-modified stamp to a
  posting date asserts something the provider never said, and because a
  presence keeps the first value it was given, that guess would then shadow the
  real publication time arriving on the detail pass.  When the provider states
  no publication time the field is absent (02 §22) and `discovered_at` /
  `first_seen_at` still record when the host saw the posting.
* `jobscraper.timeutil` was added to the adapter import allowlist: a provider
  adapter must emit durable UTC RFC 3339 timestamps (03 §50) and must not
  reimplement that format.  `timeutil` is pure formatting/parsing — no I/O, no
  database, no destination policy, no grant authority — so it widens no
  boundary the contract suite guards.  `urllib`, `socket`, `sqlite3` and
  `subprocess` remain forbidden in adapters.
* Driver generalization (architecture-preserving): registry-built adapters with
  config from the pinned binding revision; the ACQ-02 request-type↔task mapping
  is durable data in `adapters/contract.py`
  (`ACQ02_REQUEST_TASK_MAP` + `HOST_NATIVE_REQUEST_TYPE_NAMES` +
  `task_kind_for_request_type`, pinned complete-and-exact against
  `runtime.requests`); separate host budgets (`MAX_PAGES_PER_RUN = 50`
  unchanged, new `MAX_DETAIL_REQUESTS_PER_RUN = 200`) whose exhaustion yields
  `BUDGET_EXHAUSTED` coverage, never absence authority; and child-work-aware
  terminalization — a plan is `SATISFIED` only when enumeration is proven
  terminal, nothing degraded it, and every request it accepted is closed.
* `DETAIL_FETCH` joins the coverage barrier (contributing requests) only when
  the binding contract does **not** declare listing identity sufficient; the
  Greenhouse board API returns full membership in one response, so its detail
  children enrich content without holding absence authority hostage.
* Only `DETAIL` child tasks from an `ENUMERATE` pass are dispatched (crawl
  breadth is ROAD-04).  Anything else an adapter proposes is recorded as
  durable `REVIEW` evidence (`CHILD_TASK_NOT_DISPATCHED`) rather than silently
  dropped.  `acquisition_evidence.kind` stays inside the released v11 CHECK
  set — closure/missing evidence and refused plans use `REVIEW`/`FAILURE` — so
  **S2.5 adds no migration** (schema stays v14).
* Host-pipeline completions S2.5 required (each minimal, no architecture
  change, each with its own focused test):
  - `pipeline/companies.py`: strong identifiers *without* an employer name no
    longer attempt a name-less insert (a NOT NULL crash that took the whole
    fenced commit down).  The decision is recorded as
    `NO_SIGNAL`/`IDENTIFIERS_WITHOUT_NAME`; the identifiers are re-asserted by
    the first sighting that does carry a name.  `companies.name` is never
    fabricated from a board slug.
  - `pipeline/canonical.py`: `jobs.posted_at` is *filled* from the winning
    presence while unknown and never rewritten afterwards (no change class
    exists for a posted-time rewrite, so it must not happen).
  - `pipeline/coverage.py`: new `open_or_resume_coverage` continues an
    unfinished generation of the same plan+scope, or opens a deterministically
    named one (`…#pass-N`) when the earlier pass finalized its own.  Re-driving
    an interrupted run previously raised a UNIQUE constraint error inside the
    driver — a recovery path that crashes is not a recovery path.
  - `pipeline/driver.py`: the adapter's typed `FailureRecord` is now durable on
    `scrape_requests.last_failure_kind`/`last_failure_json` (not only on the
    parse attempt), and a `FAILURE`/`PARTIAL` parse no longer masquerades as a
    terminal empty enumeration (an accepted-S1 latent bug: a failed parse used
    to be read as "board is empty", which is exactly the direction that grants
    absence authority).  A plan that finds nothing left to claim closes from
    durable evidence instead of reporting `FAILED` for work another pass
    already completed.
* `runtime/provisioning.py` gained `config=` (canonical-JSON, part of revision
  identity: re-pointing a board is a **new** immutable revision, never an edit)
  and pins the revision it authorized as `source_adapter_bindings.current_
  revision_id` — without that pointer the host's own run planner
  (`POST /api/runs`) cannot select a provisioned binding at all.  New
  `ensure_builtin_adapter_definition` derives the durable `adapter_definitions`
  row from the registered manifest (append-only: an existing identity row is
  reused byte-for-byte, never rewritten), so the durable record and the code it
  authorizes cannot disagree.
* Fixtures (`tests/fixtures/greenhouse/`, 15 files + README) are sanitized,
  deterministic and offline; the E2E serves them over loopback through a
  fixture HTTP server, so the run exercises the real host executor, destination
  policy and validity gate.  Covered shapes: valid list, list with inline
  content, empty board, truncated board (`meta.total` > returned), changed
  template, malformed body, required-fields-missing (incl. traversal id and
  `javascript:` URL), removed job (404), detail id mismatch, multi-location,
  hostile detail links (`javascript:`/`data:`/`<script>`), 429 and 403
  challenge.
* E2E (`tests/integration/test_greenhouse_e2e.py`, 20 tests) proves the whole
  spine twice over: through the driver directly and through the service surface
  (`POST /api/runs` → Inbox → job detail), including origin resolution
  (`GREENHOUSE`/`acme`/job id with evidence and rejected candidates),
  `EMPLOYER_STRUCTURED_ATS` quality, company + identifier rows, multi-location
  and remote-eligibility projection, cleaned content (script residue and
  tracking parameters removed), FTS5 search, safe apply links, idempotent
  re-drive, restart recovery with no duplicate observations, and the typed
  429/challenge/404/mismatch behaviours.
* Unchanged on purpose: a rate-limited or challenged request still commits as
  `SUCCEEDED` with its typed `page_class` and degrades the run to `PARTIAL`
  (accepted Slice-1 request semantics; retry/backoff policy is not redefined
  here), and `remote_worldwide` stays conservative when a bare `"Remote"`
  location accompanies an explicit worldwide applicant requirement (Slice-1
  eligibility semantics preserved).

### S2.6 / S2.7 — Lever, Ashby (after `continue`)

Same shape as S2.5: `adapters/lever.py` (`/v0/postings/{company}` list +
per-posting application URL) and `adapters/ashby.py`
(`/posting-api/job-board/{org}?includeCompensation=true`), each with
deterministic fixtures (list, job, empty, closed, multi-location, rate-limit)
and the same contract path; no new I/O surface, no new write path.

### S2.8 — cross-provider integration/acceptance

One acceptance suite proving the whole Slice-2 chain end-to-end for all three
providers plus a generic careers URL: fingerprint → route → acquire →
observation → company/location/provenance → cleaned/indexed searchable
result; multi-source same-origin merge (aggregator + employer ATS) selecting
employer provenance without destroying the aggregator presence; low-confidence
route falling back safely; FTS mode honesty; and the security negatives
(redirect to loopback/private, oversized body, `javascript:` link, hostile
content) still denied.

### S2.9 — gate, migration verification, packaging prep, corrective review

* `scripts/verify_slice2.py`: contract suite, Slice-2 E2E, full regression,
  `pip check`, Doctor, plus migration verification **from the accepted
  Slice-1 schema (v10)** to `LATEST_SCHEMA_VERSION` with data preservation and
  FK/integrity gates.
* Packaging/native preparation: `build/jobscraper.spec` data-file check,
  `scripts/verify_packaged_build.py`/`scripts/native_acceptance.py` Slice-2
  assessment, and an honest status record. Packaged Windows/native evidence is
  recorded as **pending** unless it was really executed; Slice 2 is never
  claimed PROMOTED from Linux/dev/ordinary CI evidence.

## 3. Contract surfaces Slice 2 changes (declared explicitly)

| Surface | Change | Preserved |
|---|---|---|
| `ResultEnvelope` / `ValidatedResult` / `ParseOutcome` | §11.3 completeness + ACQ-09 evidence fields | parser gate, no unchecked raw input |
| `pipeline/driver.py` | registry-built adapters, ACQ-02 task mapping, child-work-aware terminalization | host-owned I/O, no transaction across network waits, fence-only commits |
| `pipeline/entity.py` | normative origin-identity merge stage added under the reuse guard | stage-1 native identity, split-not-merge bias |
| `pipeline/canonical.py` | provenance selection delegates to §39 class ordering | presentation-only selection, RUN-21 ordering |
| `schema_sql.py` | appends `v11…v14` | released bytes pinned, forward-only |
| `service` routes | new `s2_routes.py` (search, read-only) | Slice-1 route set and gating unchanged; `_PUBLIC` unchanged |
| Slice-1 contract pins | two *slice-boundary* pins re-scoped to Slice-2 reality | every Slice-1 architectural invariant |

## 4. Explicit deferrals (with justification)

* Generic crawler breadth, sitemap, frontier/lease capacity, revalidation/`304`
  cache semantics, coverage-generation union beyond Slice 1: ROAD-04.
* ExtractionRecipe, Adapter Lab, locator telemetry, promotion/rollback:
  ROAD-06. Slice 2 keeps its adapters built-in code adapters.
* Browser/authenticated acquisition and `BROWSER*` execution classes:
  ROAD-07 — the router reports them unsupported rather than executing them.
* Contacts/skills fact indexing in FTS: `job_facts` is ROAD-05 and contacts are
  ROAD-08; the FTS surface is extensible and the absence is reported.
* User-facing merge/undo flow: Slice 2 adds only the normative origin-identity
  resolution stages; the merge ledger/undo UX is ROAD-05.

## 5. Corrective review of S2.0–S2.2 (fixed, not merely reported)

A line-by-line review of the committed Slice-2 work found five defects and one
regression. All six are fixed here, each with a test that fails without the fix;
none changed a migration byte, so schema v11/v12 digests stay valid.

1. **Unresolved origin could mint a company identity.** `companies.signals_from`
   tested `str(status).upper().endswith("RESOLVED")`, which is true for
   `UNRESOLVED` — a resolution that said "insufficient evidence" would have
   created an `ATS_BOARD` key and merged companies on it. Now an explicit
   comparison against the resolver's own `RESOLVED` value.
2. **`companies.last_posting_at` could move backwards.** It was overwritten with
   every attach, so a late arrival carrying older evidence made a company look
   stale. Now `MAX(COALESCE(last_posting_at, ?), ?)`.
3. **Durable evidence JSON could be stored unparseable.** Both the driver's
   `_record_evidence` and ingest's `ORIGIN_RESOLUTION` detail bounded size by
   slicing `json.dumps(...)` at 60 000 characters, which truncates mid-token:
   the row then fails `json.loads` for every reader (review UI, export, and the
   per-presence origin projection). `pipeline/evidence.bounded_json` is now the
   single owner of the bound: it shrinks the *document*, preserves the shape,
   and adds an explicit `_truncated` marker plus the original length.
4. **Field-evidence hash did not describe the stored row.** The full excerpt was
   hashed while `excerpt[:500]` was persisted, so the hash could not be
   reproduced by a reader. `excerpt_and_hash` derives both from the same bytes.
5. **`sources.canonical_host` was declared but never read.** The employer-vs-
   aggregator input derived the source's host from `entry_url` only. A source
   registered with a canonical host would have been classified from the wrong
   identity, so the recorded host is now preferred (bare-host and URL spellings
   both accepted) with the entry URL as fallback. Nothing writes that column yet;
   the read is forward-compatible, and it is noted as such at the call site.
6. **Regression: the winning presence's re-projection had been dropped.** S2.2
   narrowed `refresh_canonical_presentation` to normalize only the freshest
   presence and to set the winner's projection to `None` otherwise, which
   deleted the accepted Slice-1 behavior of re-normalizing the winner's retained
   observation payload. `job_observations.raw_payload_ref` *does* hold the
   observation fields, so the path was live, not dead weight: it is restored as
   `_latest_observation_projection`, re-normalizing with the observation's own
   `observed_at` (so timezone offsets still derive from the evidence time, not
   the wall clock) and leaving the projection untouched when no payload is
   retained. Locations are rewritten only when the projected set genuinely
   disagrees, so a weaker presence's *silence* never erases the employer's set.
   This amends the S2.2 note "re-projected only when the fresh presence owns the
   presentation" in §2.
7. **Contract harness blind spot (test-only).** The single-writer scan for
   `jobs`/`job_sources`/`job_locations`/`companies`/`job_observations` matched
   `INSERT INTO`/`UPDATE` only, so a second *delete* path would have passed; it
   now matches `DELETE FROM` too, and a new test forbids any `DELETE FROM` for
   the immutable evidence tables (`job_observations`, `field_evidence`,
   `fetch_attempts`, `parse_attempts`, `acquisition_evidence`).
