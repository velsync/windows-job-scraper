# Windows Job Scraper v0.3.1.3 — Product & Workflow Specification

**Version:** Windows Job Scraper v0.3.1.3 — Final Corrected Modular Specification Set  
**Date:** 2026-09-05  
**Status:** **NORMATIVE — implementation specification**  
**Target:** Windows 11 · local-first · single-user  
**Core stack:** Python 3.12+ · FastAPI · SQLite + FTS5 · Playwright/Chromium

> This file is one member of the canonical modular specification set. Normative ownership is defined by `00_architecture_overview_and_authority.md`. If duplicated explanatory text conflicts with the normative owner, the normative owner wins.


## PROD-00. Normative ownership

This file owns user-facing domain behavior: profiles, Inbox, disposition, eligibility, scoring, companies, jobs, applications, contacts, search and exports.

Runtime storage mechanics live in `03_durable_runtime_and_persistence.md`.

# 1. Product definition

Windows Job Scraper is a local job-search operating system.

Its user loop is:

```text
discover source
  ↓
classify/fingerprint
  ↓
acquire
  ↓
validate result
  ↓
extract
  ↓
persist immutable observation/evidence
  ↓
normalize
  ↓
entity resolution / reversible deduplication
  ↓
understand eligibility/facts
  ↓
score
  ↓
triage
  ↓
apply
  ↓
follow up
  ↓
learn
```

A user must be able to:

- create one or more search profiles;
- collect jobs from structured APIs/feeds, ATSs, ordinary public pages, JavaScript-heavy pages, and legitimately authorized authenticated sources;
- preserve the discovery URL, canonical job URL, direct application URL, source-native ID, and origin-resolution evidence;
- understand whether a job is geographically/work-authorization eligible for a profile;
- see normalized salary information without treating unknown salary as zero;
- understand why a job scored highly or poorly;
- merge duplicates without losing source observations;
- undo incorrect merges;
- search the local corpus quickly with FTS5;
- review a focused Inbox of new/changed/reopened opportunities;
- shortlist, dismiss, snooze, or archive opportunities;
- open the best direct application link;
- track applications independently from listing status;
- attach notes and document references;
- set next-action reminders;
- detect meaningful listing changes, disappearance, closure, reopening, and repost evidence;
- resume interrupted collection after crashes or Windows restart;
- see source and strategy health;
- add or repair suitable custom sources through Adapter Lab without editing Python;
- export jobs and a redacted diagnostics bundle;
- run within measured resource and disk budgets on the target Windows machine.

---


# 7. Domain state planes

Three independent state planes MUST remain separate.

## 7.1 Listing lifecycle — system owned

```text
ACTIVE
UNCERTAIN
EXPIRED
CLOSED
WITHDRAWN
```

## 7.2 User disposition / Inbox workflow — user owned

```text
NONE
SHORTLISTED
DISMISSED
SNOOZED
ARCHIVED
```

Eligibility is **not** a workflow state. It is a profile-relative evidence-derived verdict.

## 7.3 Application lifecycle — user owned

An application record is separate from the canonical job.

Recommended statuses:

```text
PREPARING
APPLIED
SCREENING
INTERVIEWING
OFFER
ACCEPTED
REJECTED
WITHDRAWN
GHOSTED
CLOSED
```

A job can be `listing_status=CLOSED` while its application remains `INTERVIEWING`.

No single `status` column may combine listing, Inbox/disposition, and application state.

---


# 33. Company and multi-location model

## 33.1 Companies

```text
companies
---------
id
name
normalized_name
domain
careers_url
ats_provider
ats_board
country
notes_md
watch
blocklist
first_seen_at
last_posting_at
created_at
updated_at
```

Resolution signals:

- normalized name;
- domain;
- application URL host;
- ATS board;
- careers URL;
- structured organization metadata.

Weak evidence must not aggressively merge companies.

## 33.2 Locations

A job may have multiple locations.

```text
job_locations
-------------
id
job_id
raw_text
country
region
city
remote
timezone_min
timezone_max
source_location_id
confidence
```

The canonical model does not assume one `location_text`.

---


# 34. Content normalization and fact extraction

Content pipeline:

```text
raw/structured description
→ deterministic cleaning
→ Markdown
→ plain text
→ language
→ hashes
→ facts/evidence
```

Suggested fields:

```text
description_md
description_text
description_lang
description_hash
```

Fact schema:

```text
job_facts
---------
id
job_id
fact_type
value_json
confidence
evidence_text
evidence_start
evidence_end
rule_id
rule_version
observed_at
```

Facts may include:

- seniority;
- employment type;
- skills;
- work authorization;
- timezone requirement;
- remote scope;
- language;
- experience requirement.

Rules should be versioned data where practical.

---


# 35. Search profiles

Search profiles are first-class and all user-specific matching logic is data.

Edits create an immutable `profile_revision` (and immutable rule/config snapshots it references) before new evaluation work is planned. The mutable profile row points to the current revision; old run/evaluation records retain their original revision references.

Suggested model:

```text
search_profiles
---------------
id
name
is_default
keywords_json
negative_terms_json
locations_json
home_country
acceptable_regions_json
eligible_countries_json
remote_rules_json
timezone_window_json
salary_floor_ref
salary_currency_ref
seniority_allow_json
employment_type_allow_json
languages_json
must_keywords_json
should_keywords_json
must_not_keywords_json
source_selection_json
weights_json
rules_json
min_score_inbox
schedule_json
created_at
updated_at
```

A single job may have different:

- eligibility;
- score;
- Inbox relevance

for different profiles.

---


# 36. Eligibility engine

Eligibility is downstream of collection and normalization.

Adapter code does not decide whether a job is personally suitable.

Inputs:

- normalized locations;
- ATS location objects;
- JSON-LD `applicantLocationRequirements`;
- `jobLocationType`;
- country/region phrases;
- timezone evidence;
- work-authorization evidence;
- EOR/contractor evidence;
- profile preferences.

Verdicts:

```text
ELIGIBLE
LIKELY
UNCLEAR
UNLIKELY
INELIGIBLE
```

Schema:

```text
job_eligibility
---------------
job_id
profile_id
profile_revision_id
rules_revision_id
job_content_revision
normalization_version
evaluator_version
verdict
confidence
reason_codes_json
evidence_json
rule_version
evaluated_at
```

Hard invariant:

> Absence of restriction text does not mean worldwide eligibility.

Unknown remains `UNCLEAR`.

Hard filters and scoring preferences are distinct. Explicit ineligibility may exclude a job from Inbox; a merely unknown salary or weak positive signal should affect score rather than fabricate a hard fact.

---


# 37. Salary normalization

Parse and preserve:

- original salary text;
- range or single value;
- currency;
- period;
- gross/net hints;
- equity/bonus language;
- confidence.

Suggested fields:

```text
salary_original_text
salary_min
salary_max
salary_currency
salary_period
salary_gross_net
salary_annual_min_ref
salary_annual_max_ref
salary_ref_currency
salary_confidence
```

Reference conversion uses a local rate table. Network refresh is optional.

Unknown salary is a distinct state, never numeric zero.

---


# 38. Reversible deduplication and entity resolution

Deduplication is staged and evidence-backed.

Recommended stages:

1. same `(source_id, source_job_id)` → strong identity **unless the source-ID-reuse guard detects incompatible temporal/entity/content evidence**;
2. same `(origin_provider, origin_board, origin_job_id)` → strong merge candidate **only when the same temporal/entity/content reuse guard passes**;
3. same sufficiently job-specific canonical application URL → merge;
4. same normalized company + title + location set + compatible time window + strong content similarity → merge;
5. same company/title with meaningful location disagreement → cluster, do not merge;
6. ambiguous → review candidate.

Merge ledger:

```text
job_merges
----------
id
kept_job_id
absorbed_job_id
stage
match_score
reason_code
evidence_json
merged_at
undone_at
undone_by
```

Absorbed jobs/observations are not physically destroyed for ordinary dedup.

Merge/undo MUST define ownership for dependent user state. The implementation uses reversible aliases/relationships or an equivalent complete migration ledger for at least:

- `job_profile_state` / disposition and snooze state;
- applications and application events;
- notes/documents/reminders;
- durable Inbox events;
- canonical/source aliases created by the merge.

Conflicting dispositions are never silently discarded. Distinct application records are never collapsed merely because their jobs merge. Post-merge user edits retain a recorded logical owner so undo can deterministically restore or re-associate them. Undo restores independent canonical visibility **and** defined ownership of dependent state.

Cross-source provenance and original event identities are never discarded.

---


# 39. Canonical source selection

Displayed canonical fields prefer the highest-quality current provenance:

```text
employer structured ATS/API
→ employer careers page
→ aggregator with resolved employer origin
→ aggregator without resolved origin
```

This controls presentation, not evidence deletion.

All source and application links remain inspectable.

---


# 41. Explainable scoring

Scoring is deterministic and profile-relative.

Each contribution includes:

```text
rule
points
evidence
rule_version
```

Example:

```text
+20 eligible: EU
+15 title fit
+10 WordPress
+8 salary above floor
-12 seniority above preference
-8 salary unknown
```

Store:

```text
job_scores
----------
job_id
profile_id
profile_revision_id
rules_revision_id
job_content_revision
normalization_version
scorer_version
score
breakdown_json
rule_version
scored_at
```

Optional AI/embeddings may later provide secondary suggestions or semantic ranking, never the authoritative primary score.

---


# 42. Inbox

The Inbox is a profile-relative triage queue.

The canonical Inbox predicate is explicitly grouped:

```text
trigger = NEW_ELIGIBLE_APPEARANCE | MEANINGFUL_CHANGE | REOPENED | SNOOZE_EXPIRED | PROFILE_REVISION_ELIGIBLE

eligible_for_inbox =
  trigger_is_due
  AND score >= profile.min_score_inbox
  AND eligibility != INELIGIBLE
  AND disposition NOT IN {DISMISSED, ARCHIVED}
  AND (disposition != SNOOZED OR snoozed_until <= now)
```

`NEW_ELIGIBLE_APPEARANCE` means the first time the job becomes Inbox-eligible for that profile, which may occur for an old canonical job after a deliberate profile/rule revision. `MEANINGFUL_CHANGE` and `REOPENED` are separate trigger identities. Dismiss/archive are sticky until explicit user change. While snoozed, ordinary new/change/reopen triggers are suppressed/coalesced under policy and MUST NOT surface before the declared expiry behavior. Each snooze occurrence has a stable occurrence/trigger ID so a later snooze is not deduplicated against an earlier one.

User actions:

- shortlist;
- dismiss;
- dismiss with reason;
- snooze;
- archive;
- open source;
- open direct application URL;
- create application record.

Keyboard triage SHOULD be supported.

Hide/dismiss reasons become feedback data. Suggested rule changes may be offered, but never auto-applied.

---


# 43. Application workflow

Suggested schema:

```text
applications
------------
id
job_id
profile_id
status
applied_at
applied_via_url
resume_doc_id
cover_letter_doc_id
contact_id
salary_asked
notes_md
next_action_at
next_action_text
closed_at
outcome
outcome_reason
created_at
updated_at
```

Events:

```text
application_events
------------------
id
application_id
at
kind
detail_json
```

If a job with an active application closes/expires, record an application event and surface it to the user.

Documents are references:

```text
documents
---------
id
kind
label
path
sha256
created_at
```

The app does not automatically upload or submit documents.

---


# 44. Contact enrichment

Contact enrichment is optional downstream enrichment, not part of scraping identity.

Priority:

1. explicit contact in job listing;
2. employer careers/contact page;
3. recruiting/team page;
4. public professional/company contact page;
5. optional user-configured provider using the user's credentials/API key.

Suggested schema:

```text
contacts
--------
id
company_id
job_id_nullable
name
role
email
phone
profile_url
company_contact_url
contact_type
source_url
source_name
found_at
confidence
verified
```

Rules:

- never guess email addresses;
- preserve contact provenance;
- `verified` only when evidence supports verification;
- no bulk personal-profile scraping;
- no bulk outreach subsystem.

---


# 45. FTS5 and structured search

SQLite FTS5 indexes at least:

- title;
- company;
- description;
- locations;
- skills/facts;
- contact names/roles.

Structured filters remain outside FTS:

- salary;
- eligibility;
- listing status;
- disposition;
- application status;
- date;
- source;
- company;
- profile score.

BM25 is the text relevance baseline.

The packaged Windows build MUST verify FTS5 support.

Development fallback to simpler substring search is acceptable only with an explicit capability warning. The application must not claim FTS/BM25 is active when it is not.

---


# 54. UI areas

## Inbox

- profile selector;
- new/changed/reopened jobs;
- score + breakdown;
- eligibility + evidence;
- shortlist/dismiss/snooze/archive;
- direct apply;
- keyboard triage.

## Jobs

- FTS search;
- structured filters;
- source provenance;
- change history;
- contacts;
- notes;
- merge/undo visibility.

## Applications

- status pipeline;
- next action;
- reminders;
- documents;
- outcome.

## Companies

- open/historic roles;
- ATS/careers identity;
- contacts;
- source health;
- watch/blocklist.

## Sources

- source desired/admin state;
- bindings;
- strategy;
- access mode;
- health rollup;
- last run;
- auth state;
- robots state;
- cadence.

## Adapter Lab

- fingerprint;
- structured-data view;
- recipe teaching;
- navigation teaching;
- fixture capture;
- candidate diff;
- promote/rollback;
- locator telemetry.

## Runs

- active/recent runs;
- queue depth;
- retries;
- leases;
- current cursor;
- failures;
- resume/cancel;
- worker health.

## Health / Doctor

- database/migration health;
- backup health;
- browser worker health;
- source/binding health;
- resource usage;
- FTS capability;
- diagnostics export.

---


# 55. Local API surface

Exact routes may vary, but functionality should map to narrow service boundaries.

Representative read routes:

```text
GET /health
GET /api/jobs
GET /api/jobs/{id}
GET /api/inbox
GET /api/sources
GET /api/sources/{id}
GET /api/runs
GET /api/applications
GET /api/companies
```

Representative mutation routes:

```text
POST /api/runs
POST /api/runs/{id}/cancel

POST /api/profiles
PATCH /api/profiles/{id}

POST /api/profiles/{profile_id}/jobs/{job_id}/disposition
POST /api/jobs/{id}/merge
POST /api/merges/{id}/undo

POST /api/applications
PATCH /api/applications/{id}

POST /api/sources
PATCH /api/sources/{id}
POST /api/sources/{id}/login
POST /api/sources/{id}/smoke

POST /api/adapter-lab/sessions
POST /api/adapter-lab/candidates/{id}/promote
POST /api/adapter-lab/versions/{id}/rollback
```

Every mutation passes localhost request-authentication and CSRF/equivalent checks. Except for a minimal non-sensitive liveness endpoint, private reads, SSE/event streams, exports/downloads and administrative diagnostics also require an authenticated browser session under the security bootstrap contract in `04_security_and_authentication.md`.

---


## PROD-01. Required per-profile job state

User disposition is profile-relative and MUST be persisted independently for each `(job_id, profile_id)` pair.

Canonical logical table:

```text
job_profile_state
-----------------
job_id
profile_id
disposition
dismissed_reason
snoozed_until
first_inbox_at
last_inbox_at
triaged_at
archived_at
created_at
updated_at

PRIMARY KEY(job_id, profile_id)
```

This allows the same canonical job to be:

```text
Profile A → SHORTLISTED
Profile B → DISMISSED
```

without leaking state across profiles.

Inbox entry/re-entry behavior MUST use this table plus current score/eligibility/change evidence.

## PROD-02. Inbox event semantics

A profile-relative Inbox entry SHOULD be represented as durable state/event data rather than being inferred only from the current `jobs` table.

At minimum the implementation must distinguish:

- first eligible appearance for this profile;
- meaningful change;
- reopening;
- repost candidate;
- snooze expiry;
- user dismissal/archive;
- whether the same underlying change has already been surfaced.

A source re-observation alone must not repeatedly create a new Inbox item.


Durable resurfacing identity is required. Define:

```text
job_profile_inbox_events
------------------------
id
job_id
profile_id
event_kind
trigger_history_id
trigger_content_revision
trigger_repost_relation_id
trigger_listing_state
created_at
surfaced_at
acknowledged_at
dedupe_key
detail_json
```

`dedupe_key` MUST deterministically identify the underlying profile-relative trigger so the same change/reopen/repost/snooze-expiry event is not surfaced repeatedly across refreshes or restarts.

`job_profile_state.last_inbox_at` is current-state convenience; it is not a substitute for the durable Inbox-event ledger.

### Inbox/disposition transition requirements

For each `(job_id, profile_id)`, transition handling MUST be deterministic and durable:

| Current disposition | Ordinary new/change/reopen trigger | Snooze expiry | Profile revision becomes eligible |
|---|---|---|---|
| `NONE` / `SHORTLISTED` | may emit one deduplicated Inbox event when predicate passes | n/a unless previously snoozed | may emit first-eligible event |
| `SNOOZED` and unexpired | suppress/coalesce; do not emit | emit at most one expiry occurrence when predicate passes | remain suppressed until expiry unless user explicitly unsnoozes |
| `DISMISSED` | suppress | n/a | suppress until explicit user reversal |
| `ARCHIVED` | suppress | n/a | suppress until explicit user reversal |

Refreshing, restart, rescoring, or duplicate delivery of the same trigger creates zero additional Inbox events. Event identity includes the relevant canonical job/profile plus trigger occurrence/revision identity, not only `event_kind`.

Local user mutations from multiple dashboard tabs use an explicit row revision/optimistic-concurrency token (or an equivalently auditable last-write policy). Silent stale overwrites are not allowed.

## PROD-03. Source-ID reuse guard in dedup

A source-native identifier is strong identity, not infallible identity.

Before automatically treating a reused `(source_id, source_job_id)` as the same job, detect incompatible evidence such as:

- a long closed interval followed by a semantically unrelated posting;
- materially incompatible title/company;
- provider evidence that requisition IDs are recycled;
- incompatible creation/posted times;
- incompatible origin identity.

If the guard fires, split or require review rather than corrupting historical identity.

## PROD-04. Export contract

CSV/JSON export is a product feature and MUST be explicit.

Required behavior:

```text
ExportContract
--------------
schema_version
selected_scope
filters/profile/query
explicit_limit_or_none
row_count
generated_at
format
```

Rules:

- export **all** rows matching the selected scope unless the user explicitly requests a limit;
- never silently truncate;
- report exported row count;
- preserve source/direct application URLs;
- optionally include provenance/version fields;
- UTF-8 output;
- deterministic schema version;
- failure must not produce a deceptively complete-looking partial export.

### CSV safety

Because job titles, companies, descriptions and contacts are untrusted external data, spreadsheet-targeted CSV exports MUST neutralize formula-injection prefixes where required by the chosen CSV policy.

## PROD-05. Safe displayed links

Only approved URL schemes may be rendered as clickable external links.

At minimum:

```text
https
http
```

must be handled explicitly.

`javascript:`, `data:` and other active schemes MUST NOT be emitted from scraped source fields as clickable job/application links.

## PROD-06. Application side-effect boundary

Opening the best direct application URL is supported.

Automatic submission is not.

Application tracking records what the user did; acquisition/browser source automation MUST NOT silently perform the application itself.

## PROD-07. Canonical listing availability

The user-facing canonical listing status is derived from source-presence evidence using the rules owned by the Runtime/Persistence specification.

A single aggregator disappearance must not close a job that remains active on a trusted employer/ATS source.

## PROD-08. Product acceptance invariant

The minimum useful Windows product is:

```text
create profile
→ run one legitimate source through the real provenance spine
→ see normalized job
→ see score/eligibility
→ Inbox
→ shortlist/dismiss
→ open exact direct application link
→ create/update application
→ restart app
→ state is preserved
```
