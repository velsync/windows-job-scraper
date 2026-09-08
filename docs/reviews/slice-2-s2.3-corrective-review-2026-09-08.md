# Slice 2 S2.3 Corrective Review — 2026-09-08

Status: **CORRECTED / ACCEPTED FOR CONTINUED SLICE-2 IMPLEMENTATION**

Scope: bounded reconciliation and independent corrective review of S2.3 only. S2.4 was not started by this corrective.

Authority: v0.3.1.3, especially `01_product_and_workflow.md` §34 and §45, `03_durable_runtime_and_persistence.md` RUN-21, the Slice-2 worker plan, and the S2.2 corrective record. This record is later corrective authority for S2.3 where an earlier implementation report or pre-corrective branch state conflicts with the reviewed behavior below.

Authoritative corrected S2.2 base: `24d194bb57ac3d43dafd27120fe58438a82ecc42`.

Reconciled S2.3 implementation commit: `07e7a8a823a02367828c2550486fdb26ba39159f`.

Final behavior-bearing reviewed HEAD: `67840e82f6106ce6eb8452b64f5686cb91113c64`.

## Reconciliation

The originally reported S2.3 work was based on stale pre-corrective S2.2 lineage. It was therefore not accepted in place. The S2.3 implementation tree was transplanted onto the authoritative corrected S2.2 HEAD without importing unrelated stale-branch files, then independently reviewed and corrected on `repair/s2.3-reconcile-search`.

The resulting reviewed lineage is a clean descendant of `24d194bb57ac3d43dafd27120fe58438a82ecc42`; no force-push or history rewrite of the accepted S2.2 lineage was used.

## Findings and corrections

### 1. S2.2 entity-resolution evidence had regressed during the transplant

The overlapping `pipeline/ingest.py` implementation initially reintroduced stale durable resolution-stage labels.

Corrective behavior preserves the reviewed S2.2 mapping:

- origin identity → `origin_identity_stage2`
- canonical job URL → `canonical_url_stage3`
- same-source native identity → `native_identity_stage1`
- native-ID reuse refusal/split → `native_identity_reuse_guard`

Commit `1fcf5e9ea7bfec72277933737a59ce3131e983a7` restores this invariant. The S2.2 company and origin-resolution corrections also remain intact, including `company-resolution-v2`, shared ATS platform-host exclusion from strong employer identity, and source-local generation `1` for new cross-source origin attachment.

### 2. Search provisioning could claim FTS5 over an incomplete or stale derived index

Two correctness holes existed around S2.3 provisioning:

- an existing v12 corpus could migrate to v13 with empty `job_search_docs` / `job_search_state` and therefore not be immediately searchable;
- if FTS synchronization triggers disappeared while durable search docs changed, recreating the triggers alone could reactivate `FTS5_ACTIVE` while the FTS virtual table still reflected stale content.

Corrective behavior:

- provisioning deterministically backfills/reconciles `job_search_docs` and `job_search_state` from current canonical jobs before claiming active FTS;
- the FTS5 virtual table is treated as search-owned derived state and is rebuilt/reconciled from the durable search-document corpus before `FTS5_ACTIVE` is recorded;
- `FTS5_ACTIVE` remains contingent on the required FTS table plus INSERT/UPDATE/DELETE synchronization triggers being present and usable;
- degraded surfaces report `SUBSTRING_FALLBACK` rather than silently returning incomplete BM25 results;
- re-provisioning after degraded operation converges the FTS index to the current durable corpus.

Regression coverage pins both v12→v13-style backfill and stale-index recovery after trigger loss.

### 3. Ordinary Markdown without links was not reliably recognized

The cleaner's Markdown path was too link-centric: structural Markdown such as headings, lists and emphasis could be treated as plain text when it contained no Markdown link.

Corrective behavior recognizes explicit block and inline Markdown structure while still avoiding accidental interpretation of ordinary plain text. Structural Markdown remains in the stored Markdown representation and is removed from the derived plain-text representation.

### 4. Cleaner idempotence was incomplete for structural Markdown replay

The final audit found an additional §34 defect after the first corrected CI pass. HTML list cleaning produced Markdown `- One\n- Two` with plain text `One\n\nTwo`; re-cleaning that stored Markdown yielded `One\nTwo`, changing both `text` and `content_hash`.

A new RED regression at commit `739beed9563c5aa3a76cb68df8f6c949a77e4077` proved the defect: Ubuntu reported exactly one failure with `635 passed, 5 skipped` before the production fix.

Corrective behavior makes Markdown list replay preserve the established plain-text block boundaries, so re-cleaning the canonical Markdown representation is byte-stable for both `text` and `content_hash`. The fix is contained in final behavior commit `67840e82f6106ce6eb8452b64f5686cb91113c64`; no migration bytes or previously accepted Slice-1 text semantics were changed.

### 5. One S2.2 regression test encoded nondeterministic ID ordering

A company-resolution regression test assumed lexical ordering of independently generated evidence IDs. That ordering is not a product invariant and produced intermittent cross-platform CI failure unrelated to behavior.

The test was corrected to assert the durable semantic relationship rather than random identifier ordering. Production behavior was not changed for this finding.

## S2.3 accepted behavior

The reviewed S2.3 package now provides:

- deterministic/versioned content cleaning behind `CONTENT_CLEANING_VERSION`;
- HTML entity handling, script/style removal, structural Markdown retention, executable-link neutralization, and tracking-parameter removal;
- stable cleaned Markdown/plain-text/hash replay for the pinned structural cases;
- migration v13 bookkeeping: `jobs.content_cleaning_version`, `job_search_docs`, `job_search_state`, and `search_capability`;
- FTS5 virtual table and synchronization triggers provisioned only by capability-gated host code, never by unconditional migration SQL;
- durable substring fallback with explicit mode/warning when the FTS surface is unavailable or incomplete;
- BM25 search only when the full FTS surface is verified active;
- host-owned idempotent search-document synchronization using `job_search_state.indexed_content_hash`;
- session-gated read-only `GET /api/search` through the Slice-2 route module;
- Doctor reporting that is honest for current, degraded, and pre-v13 schema states.

Migration v13 remains append-only and digest-pinned. No new runtime dependency was added.

## Deliberate structured-filter deferral

The Slice-2 plan states that structured filters stay outside FTS. S2.3 implements the structured SQL-side filters whose authoritative fields already have meaningful producers. It does **not** fabricate salary/profile-relative filtering from unavailable or heterogeneous values.

Specifically, raw `jobs.salary_min` / `salary_max` values are period-dependent and are not a valid common numeric basis, while the annualized reference fields `salary_annual_min_ref` / `salary_annual_max_ref` do not yet have the authoritative producer needed for reliable filtering. Profile-relative score semantics likewise belong to the later profile/scoring workflow that owns those values.

Those filters remain an explicit deferred capability until their owning producers exist. Search responses and capability reporting must not claim unsupported semantics in the meantime.

## TDD and verification evidence

The corrective was implemented test-first where behavior changed.

- Existing corrected S2.2 regressions first exposed the transplanted stale stage-label behavior.
- New S2.3 regressions pinned pre-existing-corpus backfill, stale FTS recovery after trigger loss, ordinary Markdown recognition, and structural cleaner replay idempotence.
- The structural-idempotence RED commit `739beed9563c5aa3a76cb68df8f6c949a77e4077` produced `1 failed, 635 passed, 5 skipped` on Ubuntu before the production fix.
- Final behavior-bearing commit: `67840e82f6106ce6eb8452b64f5686cb91113c64`.
- GitHub Actions run `34276037221` on Python 3.12:
  - Ubuntu: `636 passed, 5 skipped`; dependency consistency PASS.
  - Windows: `641 passed`; dependency consistency PASS.
  - Windows production-lock pywin32 primitive import proof PASS.
  - Pinned Playwright Chromium install PASS.
  - Inert browser-worker smoke PASS (`WJS-Inert-Smoke`).

## Invariants preserved

- Corrected S2.2 company/origin-resolution behavior remains in force.
- Released Slice-1 migration bytes remain untouched.
- Slice-2 migration v13 remains digest-pinned; the corrective did not rewrite migration bytes.
- FTS virtual tables are derived search state, not canonical/evidence ownership; immutable evidence and canonical ownership rules were not weakened.
- No new network, adapter, plugin, credential, destination-policy or second canonical-write authority was introduced.
- No S2.4 fingerprint/router/provisioning behavior and no S2.5 Greenhouse adapter behavior was implemented.
- Slice 2 as a whole is still not promoted.

## Handoff

S2.3 is **accepted as the reviewed implementation package** on the active Slice-2 lineage once this record and the corresponding `AGENTS.md` status update are fast-forwarded onto `arena/01a0814f-windows-job-scraper`.

The next bounded implementation package is **S2.4 — ATS fingerprinting + strategy router**. It may begin only from the active branch containing this corrective record. S2.4 must not be interpreted as Slice-2 promotion; S2.5 and final Slice-2 acceptance remain later work.
