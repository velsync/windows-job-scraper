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
