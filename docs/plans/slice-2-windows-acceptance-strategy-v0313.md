# Slice 2 Windows Packaged/Native Acceptance Strategy — v0.3.1.3

Status: **APPROVED PRE-PROMOTION ACCEPTANCE STRATEGY**  
Scope: Slice 2 packaged/native Windows promotion only.  
Authority: `docs/spec/v0.3.1.3/`, `docs/plans/slice-2-worker-implementation-plan-v0313.md` S2.9, and the already accepted Windows acceptance discipline in `docs/plans/slice-0-windows-acceptance-strategy-v0313.md`.

## 1. Purpose and promotion boundary

Slice 2 is implemented through S2.9 and has automated/corrective evidence, but it is **not promoted as a whole** until one exact packaged Windows candidate passes this strategy and a controlling promotion-closure record is committed.

This gate proves the Windows package actually contains and can execute the Slice-2 product surfaces that are reachable in the current product: the three graduated provider adapters, canonical company/location/content state, search capability, restart persistence, and post-workload diagnostics. It does not duplicate every platform-neutral parser/security assertion already owned by `scripts/verify_slice2.py`.

The accepted Slice-0/1 native harness is not rewritten for this gate. It remains the foundation regression authority. Slice 2 adds a separate bounded W2 companion harness.

## 2. Candidate freeze

The candidate is frozen only after the native-preparation changes have been reviewed and integrated into the active authoritative lineage.

Before packaging, record and preserve:

- exact Git commit SHA;
- clean working tree;
- Python/runtime and dependency-lock state;
- package tree SHA-256 and package build ID emitted by `scripts/verify_packaged_build.py`.

All packaged/native checks below MUST exercise the **same package directory and same build ID**. Rebuilding creates a new candidate and requires a new evidence root.

## 3. Pre-native gates

All of the following are prerequisites, not substitutes for native acceptance:

1. `python scripts/verify_slice2.py` — PASS.
2. Full repository CI at the frozen candidate — Ubuntu and Windows PASS.
3. `python scripts/verify_packaged_build.py --workdir <isolated-workdir>` — PASS.
4. The generated `package-verification.json` reports `result: PASS` and binds the package to a deterministic `build_id` / `build_sha256`.
5. No unresolved critical Slice-2 corrective finding remains.

If any prerequisite is red, stop. Do not run or claim native promotion against that candidate.

## 4. Native matrix

Run both harnesses against the same verified `dist/JobScraper` directory.

### Foundation regression — W0 + W1

Use the already accepted harness:

```text
python scripts/native_acceptance.py --target exe --exe <package-dir> --slice all --evidence-dir <evidence>/foundation
```

Required result: **W0 18/18 PASS + W1 7/7 PASS; zero FAIL; zero NOT_RUN.**

This proves the Slice-0 Windows safety shell and Slice-1 product vertical remain valid under the Slice-2 package candidate.

### Slice-2 packaged/native checks — W2

Use:

```text
python scripts/native_acceptance_slice2.py --target exe --exe <package-dir> --candidate-commit <sha> --build-id <build-id> --evidence-dir <evidence>/w2
```

Required result: **W2 6/6 PASS; zero FAIL; zero NOT_RUN.**

| Check | Required proof |
|---|---|
| W2-01 | Packaged Greenhouse provider path executes through the target service/run pipeline and produces the expected canonical jobs, source presences, company association, locations, cleaned content, and provider observations. |
| W2-02 | Same proof for the packaged Lever provider path. |
| W2-03 | Same proof for the packaged Ashby provider path. |
| W2-04 | Authenticated packaged `/api/search` returns the expected provider jobs and reports search capability honestly: `FTS5_ACTIVE` + BM25 only when FTS5 is active, otherwise explicit `SUBSTRING_FALLBACK` warning and no BM25 claim. |
| W2-05 | Slice-2 canonical/company/location/binding state survives a real target-process restart. |
| W2-06 | Packaged Doctor is healthy after the Slice-2 provider/search/restart workload. |

Overall native requirement: **31/31 PASS (W0 18 + W1 7 + W2 6), zero FAIL, zero NOT_RUN.**

## 5. Fixture and authority rules

W2 uses deterministic harness-hosted loopback provider fixtures. It MUST NOT depend on public Internet availability, live ATS data, DNS variability, third-party rate limits, or anti-bot behavior.

The harness may provision the initial Source/Binding test authority directly because operator-facing source-management UI/API is not a Slice-2 product surface. That setup is test authority only. The target application itself must own run planning, request execution, provider-adapter execution, canonical persistence, search, restart behavior, and Doctor behavior.

The durable generic `SOURCE_DISCOVERY` path remains a required automated Slice-2 invariant, but it is not duplicated as W2 native product acceptance because the current product has no operator-facing discovery route/UI that can drive it through the packaged target. Its durability, security and recovery semantics remain pinned by `scripts/verify_slice2.py` and the Slice-2 corrective suites.

## 6. Packaging assessment

No new non-Python runtime resource is required by the graduated Slice-2 providers or search route. The built-in registry statically imports `generic_discovery`, `greenhouse`, `lever`, and `ashby`; therefore PyInstaller module collection is reachable from the existing package graph. W2-01..W2-04 are the authoritative packaged proof that those code paths were actually collected and are executable.

Do not add speculative hidden-import/resource entries merely to make the package look Slice-2-aware. If the real package run exposes a missing module/resource, correct only the proven packaging gap, rebuild to a new build ID, and rerun the required gates.

## 7. Evidence layout

For build `<build-id>`, preserve at minimum:

```text
artifacts/slice2/<build-id>/
  package-verification.json
  foundation/
    native-acceptance.json
    doctor.json
    process-inventory-before.txt
    process-inventory-after.txt
    ...other W0/W1 harness evidence...
  w2/
    native-acceptance.json
    slice2-doctor.json
    process-inventory-before.txt
    process-inventory-after.txt
```

The W2 evidence record must contain the exact candidate commit and build ID. Evidence from different builds must never be combined into one promotion claim.

## 8. Failure and rerun policy

Any `FAIL` or `NOT_RUN` blocks Slice-2 promotion. Preserve the failed evidence. Classify the root cause before editing. A genuine product/packaging defect receives a bounded corrective and a new candidate/build. An environmental inability to execute remains `NOT_RUN`; it is not converted to PASS by narrative waiver.

After any behavior-bearing or packaging corrective, rerun the affected automated gates, full CI, package verification, and the full W0+W1+W2 native matrix against the new candidate unless a controlling authority explicitly narrows the rerun.

## 9. Promotion closure

Passing this strategy makes the candidate **eligible for architecture review**, not self-promoting. Final Slice-2 promotion requires a committed closure record that identifies:

- exact candidate commit;
- exact build ID and package SHA-256;
- package-verification result;
- W0/W1/W2 counts and evidence paths;
- CI/automated-gate evidence;
- zero unresolved blocking findings.

Until that closure is reviewed and committed, living documentation must continue to say Slice 2 packaged/native promotion is pending.
