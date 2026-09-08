# Slice 2 S2.2 Corrective Review — 2026-09-08

Status: **CORRECTED / AUTOMATED CROSS-PLATFORM GATE PASS**

Scope: bounded corrective review of S2.2 only. S2.3 was not started by this corrective.

Authority: v0.3.1.3, especially `01_product_and_workflow.md` §33.1 and §38, `03_durable_runtime_and_persistence.md` RUN-15/RUN-21, and the Slice-2 worker plan. This record is later corrective authority for the S2.2 implementation notes where they conflict with the pre-corrective wording in `docs/plans/slice-2-worker-implementation-plan-v0313.md`.

Starting implementation HEAD: `ff55ee3cc51713186c0c723bf6286175d48b0a15`.

## Findings and corrections

### 1. Shared ATS infrastructure was incorrectly usable as company identity

S2.2 originally allowed `APP_HOST` / `CAREERS_HOST` values such as `boards.greenhouse.io` to enter the globally unique `company_identifiers` strong-signal index. Different employer tenants on the same ATS therefore could attach to the same company merely because they shared provider infrastructure.

Corrective behavior:

- `(ats_provider, ats_board)` remains a strong company-resolution key.
- Employer-owned application/careers/organization hosts remain strong keys.
- Shared ATS provider hosts are retained in input/evidence snapshots but are **not** employer-unique company identifiers and cannot carry a company merge.
- The current Greenhouse/Lever/Ashby provider-host set is derived from the versioned `acquisition.atsendpoints.ATS_ENDPOINT_SPECS` authority rather than duplicated manually; existing non-Slice-2 platform exclusions remain conservative.
- Company resolution rules are bumped from `company-resolution-v1` to `company-resolution-v2`, so historical decisions stay distinguishable under ARC-10.

Regression coverage proves both the concrete two-Greenhouse-tenant failure mode and exclusion of every provider host currently declared by `ATS_ENDPOINT_SPECS`.

### 2. Cross-source origin attachment inherited another source's identity generation

A successful §38 stage-2 `MATCHED_ORIGIN` previously copied the matched source presence's `source_identity_generation`. That generation is the reuse history of a different `(source_id, source_job_id)` identity.

Corrective behavior: a newly attached cross-source presence begins its own source-local native-identity history at generation `1`. Same-source reuse behavior remains unchanged.

### 3. Durable entity-resolution stage labels were misleading

`entity_resolution_events.stage` previously collapsed stage-2 origin and stage-3 canonical-URL decisions into stage-1/reuse labels even though the evidence JSON carried the actual stage.

Corrective behavior:

- origin identity → `origin_identity_stage2`
- canonical job URL → `canonical_url_stage3`
- same-source native identity → `native_identity_stage1`
- native-ID reuse refusal/split → `native_identity_reuse_guard`

The durable stage column now agrees with the decision/evidence that produced it, including review/refusal outcomes at stage 2.

## TDD evidence

The corrective was implemented test-first.

- Initial regression commit `b64078eeea94c2d8485973e6fc5ebdc73eb6017e` failed CI before the production fixes, pinning the three reviewed defects.
- Expanded provider-host coverage commit `ca7d89cf9af47a11c25605639b17af46e0374b1a` also failed CI before the authority-derived host correction, proving the earlier hard-coded exclusion set was incomplete.
- Final behavior-bearing corrective commit before this record: `dfe45fafad6ea3ea982318c325d8d97e385c8609`.
- GitHub Actions Python 3.12 Ubuntu: full automated suite PASS; dependency consistency PASS.
- GitHub Actions Python 3.12 Windows: full automated suite PASS; dependency consistency PASS; production-lock pywin32 proof PASS; pinned Chromium install PASS; inert browser-worker smoke PASS.

## Invariants preserved

No released migration bytes changed. Schema remains v12. No runtime dependency was added. No adapter/network/DB authority was widened. No second canonical write path was introduced. No security grant or destination-policy logic changed. S2.3/S2.4/S2.5 behavior was not implemented.

## Superseded S2.2 implementation-note details

Where the Slice-2 plan's S2.2 “As implemented” prose says `company-resolution-v1` or says ATS platform hosts are recorded as strong company identifiers, this corrective record controls instead:

- current company resolver version: `company-resolution-v2`;
- shared ATS platform hosts are evidence only, not company-merge identifiers.

Package ordering and all other Slice-2 plan authority remain unchanged. The next implementation package is **S2.3 — deterministic content cleaning + FTS5**.