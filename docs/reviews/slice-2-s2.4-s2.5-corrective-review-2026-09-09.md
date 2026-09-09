# Slice 2 S2.4/S2.5 Corrective Review — 2026-09-09

## Verdict

**ACCEPT — S2.4/S2.5 corrective pass is accepted.**

This verdict is limited to the corrective scope through **S2.5**. **S2.6 remains blocked** and must not begin until a separate explicit continuation decision.

## Authority and scope

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3**
- Corrective branch: `repair/s2.4-s2.5-correctives-20260909`
- Clean accepted code/test tree: `433179ea57a96bda6ac2570f8ea017fcd5a6e994`
- Post-seal CI on that tree: workflow run `34352296885` — **SUCCESS**
- Cross-platform corrective acceptance run: `34350853174` — **SUCCESS**

The corrective pass intentionally stops before S2.6. No S2.6, Lever implementation, or later Slice-2 work is accepted by this review.

## Accepted corrective lineage

The review preserves the previously accepted corrective packages and extends them with the runtime/evidence corrections required by the audit:

1. `80d8a8603b742bdfcba460a2e4e3b2da50c48062` — structural content sanitizer corrective package.
2. `d0925273d79de016d6cb9197463f22f1d93dcb76` — PSL-backed classification / closed-set label corrective package.
3. `721e3ef8f2fc19f6ef21a26b86112952f95c5dbe` — S2.5 Greenhouse runtime corrective package.
4. S2.4 evidence-boundary corrections are present in the sealed accepted tree and are covered by dedicated regression tests.

## S2.5 Greenhouse completion semantics

The accepted implementation now enforces the required distinction between listing coverage and remaining detail/run work:

- A Greenhouse listing response derives a deterministic expected member set when the response provides reliable completion evidence.
- Missing or unreliable `total_count` evidence yields `PARTIAL`, not `COMPLETE`.
- A mismatch between expected and discovered members yields `PARTIAL`.
- `COMPLETE` is allowed only when reliable count evidence and discovered membership agree exactly.
- COMPLETE listing evidence persists the expected-member proof needed for later absence authority.
- Detail requests do not inflate listing-page completion counters.
- Listing coverage may remain COMPLETE while detail requests continue to drain.
- A run cannot become terminal while required requests or processing remain pending.
- Same-run retry after a detail failure remains possible.

These corrections preserve the v0.3.1.3 rule that absence authority requires COMPLETE coverage and that run terminality cannot outrun dynamically created work.

## S2.4 evidence linkage and causality

The accepted S2.4 evidence boundary now rejects orphaned or causally invalid evidence:

- fingerprint evidence requires an existing source;
- route decisions require an existing source;
- a route decision requires a fingerprint when a decision is being persisted;
- the route decision must match the fingerprint family and confidence;
- the matching fingerprint must already exist durably for that source;
- provisioning preflights evidence validity before source/binding mutation;
- invalid evidence paths fail before durable provisioning mutation;
- valid source → fingerprint → decision → provisioning linkage remains accepted.

Dedicated corrective tests cover these invariants, including orphan-evidence rejection and pre-mutation failure behavior.

## Content cleaning and classification

The previously accepted structural sanitizer and PSL-backed classification behavior remain intact. Stale test expectations encountered during the corrective pass were reconciled to the stronger accepted contracts; production guards were not weakened to satisfy old fixtures.

The clean accepted tree contains the reconciled durable tests and does **not** contain the temporary expectation-reconciliation script used by the acceptance harness.

## Migration immutability

Released migration bytes were not changed.

`src/jobscraper/db/migrations.py` has blob SHA:

`161704673ea3ac50e93a7e68c9f4bc593b096bf4`

This is the same blob SHA verified against the accepted S2.4 corrective lineage before the final seal. The released v14 migration therefore remains byte-preserved across this corrective pass.

## Cross-platform verification

### Ubuntu 24.04 / Python 3.12.14

Corrective acceptance job: `102463487189` — **SUCCESS**

- Full pytest: **880 passed, 5 skipped**
- Slice 0 regression: **880 passed, 1 skipped**
- Slice 0 automated gate: **PASS**
- Slice 1 contract suite: **73 passed**
- Slice 1 E2E acceptance: **3 passed**
- Slice 1 full regression: **880 passed, 1 skipped**
- Slice 1 automated gate: **PASS**
- Cumulative S2.0–S2.5 corrective surface: **402 passed**

### Windows Server 2025 / Python 3.12.10

Corrective acceptance job: `102463487285` — **SUCCESS**

- Full pytest: **885 passed**
- Slice 0 regression: **881 passed**
- Slice 0 automated gate: **PASS**
- Slice 1 contract suite: **73 passed**
- Slice 1 E2E acceptance: **3 passed**
- Slice 1 full regression: **881 passed**
- Slice 1 automated gate: **PASS**
- Cumulative S2.0–S2.5 corrective surface: **402 passed**

The Linux/Windows count difference is explained by platform-specific tests that are skipped on Ubuntu and executed on Windows.

## Slice-2 verifier sequencing

`scripts/verify_slice2.py` is intentionally **not** created or claimed as passed in this corrective review.

The authoritative Slice-2 worker plan reserves that verifier and the final Slice-2 packaging/native gate for **S2.9**, after S2.6–S2.8. Because this corrective pass is explicitly required to stop before S2.6, creating the S2.9 verifier here would violate the approved sequencing.

For this pre-S2.9 acceptance, the cumulative S2.0–S2.5 surface was therefore validated directly on both Ubuntu and Windows, with **402/402 tests passing on each platform**.

## Clean-tree seal

The cross-platform acceptance workflow materialized the clean accepted commit:

`433179ea57a96bda6ac2570f8ea017fcd5a6e994`

The repair branch was then fast-forwarded to that commit. Integrity checks confirmed:

- branch HEAD matched the clean accepted commit;
- `.github/workflows/_corrective_acceptance_gate.yml` was absent;
- `scripts/_apply_full_suite_expectation_reconciliation.py` was absent;
- migration blob SHA remained unchanged;
- normal repository CI on the clean accepted commit completed successfully.

## Final disposition

**ACCEPT S2.4/S2.5 CORRECTIVE PASS.**

The accepted result is a clean, cross-platform-green S2.0–S2.5 state with the audited S2.4 evidence boundary and S2.5 Greenhouse completion/terminality defects corrected, prior sanitizer/classification corrections preserved, released migration bytes unchanged, and temporary corrective machinery removed.

**S2.6 remains blocked pending a separate explicit instruction to continue.**
