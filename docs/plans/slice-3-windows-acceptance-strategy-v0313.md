# Slice 3 Windows Packaged/Native Acceptance Strategy — v0.3.1.3

Status: **PLANNING COMPLETE — NATIVE EXECUTION NOT YET PERFORMED**  
Date: 2026-09-10  
Scope: Slice 3 ROAD-04 native risk checkpoint and final packaged/native promotion gate.  
Authority: v0.3.1.3, `docs/plans/slice-3-worker-implementation-plan-v0313.md`, and the accepted W0/W1/W2 discipline.  
Slice-2 promotion baseline: `docs/reviews/slice-2-native-promotion-closure-2026-09-10.md`.

## 1. Purpose

Slice 3 introduces correctness behavior whose core logic is mostly platform-neutral but whose failure modes include real Windows process termination, SQLite/WAL locking, service restart, packaged import/resource collection and wall-clock/process-lifecycle behavior.

The strategy therefore uses **two native stages**:

1. **N3-A development risk checkpoint after S3.4** — catches Windows-specific ownership/restart defects before the crawler/data-lifecycle work is built on top of them. It is not promotion.
2. **W3 final packaged/native gate after S3.13** — proves the exact frozen Slice-3 package survives real target-process interruption and preserves collection truth.

Do not run a full native package matrix after every S3.x package. Per VER §67, parser/cursor/rate/coverage semantics belong primarily in deterministic automated/offline tests; native testing focuses on genuinely OS/package/process-specific behavior.

## 2. N3-A intermediate native checkpoint

### Entry conditions

S3.0–S3.4 have separate coherent commits/reviews and all focused/full currently-required automated tests are green. The checkpoint may exercise a development/package build from their combined head.

### Required scenarios

| Check | Required proof |
|---|---|
| N3-A-01 | Two independent Windows SQLite connections/process tasks contend for one eligible request; exactly one claim/attempt wins. |
| N3-A-02 | Let a lease expire and attempt heartbeat + terminal commit before the reclaimer runs; both are rejected and no request-owned output is committed. |
| N3-A-03 | Kill/restart the service with a RUNNING request; fresh service epoch records prior attempt abandoned and requeues/fails only according to durable attempt budget. Prior owner cannot commit. |
| N3-A-04 | Cancel a run with acquisition work and already-accepted local work; no new source-network claim occurs after cancellation while host-native local obligations remain durable/claimable. |
| N3-A-05 | Persist a valid Retry-After/cooldown, restart the service, and prove the source/binding is not dispatched before the durable cooldown expires. |
| N3-A-06 | After the workload, effective SQLite `journal_mode`, `foreign_keys`, `synchronous`, busy-timeout expectations and Doctor remain healthy. |

All six must PASS. Any FAIL blocks S3.5. Preserve evidence rather than narratively waiving a failure.

### What N3-A does not prove

It does not prove crawler scope, sitemap, cache/304 membership, coverage/absence, fallback aggregation, temporal projection ordering or full crash-recovery. Those do not exist yet and are W3/automated obligations later.

## 3. Safe batching between native gates

Implementation remains one sub-slice at a time. Native execution may be batched as follows:

```text
S3.0 → S3.1 → S3.2 → S3.3 → S3.4
                                ↓
                              N3-A
                                ↓
S3.5 → S3.6 → S3.7 → S3.8 → S3.9 → S3.10 → S3.11 → S3.12 → S3.13
                                                                    ↓
                                                            final W3 gate
```

This is safe because S3.5–S3.11 are primarily deterministic contract/state-machine behavior and must already have exhaustive offline fault-injection tests. S3.12 then integrates restart semantics, and S3.13 freezes the automated candidate before native testing.

A new native checkpoint should be inserted earlier only if a package introduces a proven Windows-only primitive or changes launcher/process/packaging behavior not covered by N3-A. Do not create native ceremony for ordinary pure Python parser/state transitions.

## 4. Final candidate freeze

After S3.13 corrective review and before W3:

- freeze exact candidate commit SHA;
- require clean working tree;
- run `python scripts/verify_slice3.py` successfully;
- require full repository CI at exact SHA on Ubuntu and Windows;
- run packaged build verification in a fresh isolated workdir;
- record Python, dependency lock, PyInstaller, Playwright/browser metadata inherited by the package;
- record deterministic package build ID and package SHA-256;
- require zero unresolved blocking Slice-3 findings.

Every W0/W1/W2/W3 packaged/native check must exercise the **same unchanged package directory/build identity**. A rebuild is a new candidate.

## 5. Pre-native final gates

Required before W3:

1. focused Slice-3 test suites — PASS;
2. full repository test suite — PASS;
3. `scripts/verify_slice3.py` — PASS;
4. migration from actual promoted Slice-2 schema v14 — PASS with v1–v14 pins unchanged;
5. `PRAGMA integrity_check`, `foreign_key_check`, application consistency and effective PRAGMA verification — PASS;
6. Ubuntu CI — PASS at candidate SHA;
7. Windows CI — PASS at candidate SHA;
8. packaged-build verification — PASS with exact build ID/SHA-256;
9. no unresolved critical/blocking corrective finding.

Failure of any prerequisite stops promotion testing.

## 6. Foundation regression under the Slice-3 package

The final package must rerun the accepted foundation matrices rather than assuming earlier package results transfer:

- W0 Slice-0 foundation: **18/18 PASS**;
- W1 Slice-1 product MVP: **7/7 PASS**;
- W2 Slice-2 provider/search/restart: **6/6 PASS**.

Required foundation subtotal: **31/31 PASS; zero FAIL; zero NOT_RUN**.

Use the existing accepted harnesses unchanged unless a real interface evolution requires a reviewed compatibility update. Do not weaken prior acceptance to accommodate Slice 3.

## 7. W3 Slice-3 packaged/native matrix

Recommended W3 companion harness: `scripts/native_acceptance_slice3.py`.

The harness should use deterministic loopback fixture servers and target the packaged service. It must not depend on the public Internet, ATS uptime, live DNS variability, anti-bot behavior or external credentials.

### W3 checks

| Check | Required packaged/native proof |
|---|---|
| W3-01 | **Lease/fence/service-epoch recovery.** Start target work, kill target/service process while request is RUNNING, restart exact package, prove prior attempt is abandoned/retried under fresh epoch and a stale attempt cannot heartbeat or commit. |
| W3-02 | **Retry-After/circuit persistence.** Fixture returns retryable rate response with Retry-After; restart package during cooldown; no premature redispatch; later eligible retry succeeds and durable attempt history remains intact. |
| W3-03 | **Cursor/frontier restart.** Multi-page fixture is interrupted after a durable page/cursor checkpoint; restart resumes compatible cursor/frontier exactly once, does not refetch already-succeeded logical work solely because of restart, and reaches the correct union. |
| W3-04 | **Sitemap + bounded generic HTTP crawl.** Packaged crawler parses deterministic sitemap/index fixture, stays within allowed host/path/budget, de-duplicates frontier URLs and produces the expected observations through the ordinary provenance spine. |
| W3-05 | **304 retained membership across restart.** Initial authoritative list contains A/B; later compatible 304 after target restart retains A/B as seen using durable compatible cache membership; no empty/absence transition and no fabricated content revision. |
| W3-06 | **Coverage/absence safety across interruption.** Interrupt an in-progress multi-page generation before finalization; restart completes/continues the same durable generation without applying premature absence. A later valid complete authoritative generation applies only correct same-scope transitions exactly once. |
| W3-07 | **Fallback group/run truth across restart.** Primary binding fails in fallback-eligible way; terminate/restart before or during fallback activation; exactly the next pinned rank activates, successful fallback makes logical group SATISFIED, unused later fallback is SKIPPED_NOT_NEEDED, run final result is correct. |
| W3-08 | **Cancellation + local obligation recovery.** Accept an observation, create deterministic host-native obligations, request cancellation/terminate before local processing finishes, restart; no new source I/O occurs, accepted observation materializes locally exactly once, and run/canonical state remains honest. |
| W3-09 | **Temporal/multi-source availability persistence.** Deliver trusted employer/ATS active evidence and aggregator absence/older conflicting evidence in reverse completion order across a restart boundary; current canonical listing remains evidence-ordered and cannot be falsely closed by the aggregator/older completion. |
| W3-10 | **Post-workload package health/resources.** Doctor/SQLite integrity/PRAGMAs are healthy; runtime leaves no leaked target workers; capture process inventory, WAL/disk growth, cancellation responsiveness and relevant resource metrics for VER-15. |

Required W3 result: **10/10 PASS; zero FAIL; zero NOT_RUN**.

Recommended overall final native requirement: **41/41 PASS** = W0 18 + W1 7 + W2 6 + W3 10, with zero FAIL and zero NOT_RUN.

If implementation evidence shows two W3 checks can be combined without losing independent proof, the harness may execute them in one scenario, but the result record must still report each required proof separately.

## 8. Automated-only proofs that W3 must not replace

Even a green W3 package is insufficient unless automated tests prove at least:

- duplicate enqueue and same-URL/different-strategy identity;
- concurrent claim/fence race matrices beyond the one native case;
- heartbeat boundary at exact expiry;
- deterministic retry budget/jitter bounds;
- all crawler loop/trap stop policies;
- sitemap malformed/XXE/size/depth controls;
- cache compatibility dimensions and pruning behavior;
- PARTIAL idempotency and zero absence authority;
- multi-page seen-union and detail-sufficiency finalization;
- explicit same-scope absence membership;
- overlapping generation reverse completion;
- all fallback/run truth-table combinations;
- stale presence/canonical/evaluation projection rejection;
- full RUN-19 crash-point fault injection;
- current quarantine/permission/cancellation override over pinned plans;
- SSRF/safe-method/page-validity/redaction regressions.

W3 tests the package/OS integration of these semantics, not every combinatorial branch.

## 9. Evidence layout

For build `<build-id>` preserve at minimum:

```text
artifacts/slice3/<build-id>/
  package-verification.json
  foundation/
    native-acceptance.json          # W0/W1
  w2/
    native-acceptance.json          # W2 regression
  w3/
    native-acceptance.json
    slice3-doctor.json
    process-inventory-before.txt
    process-inventory-during.txt
    process-inventory-after.txt
    resource-measurements.json
    ...scenario-specific fixture/restart evidence...
```

The W3 record must bind exact candidate SHA, build ID and package SHA-256. Never mix evidence from different package builds.

N3-A evidence may live under a development-only location such as `artifacts/slice3/checkpoints/<commit>/n3-a/`; it is useful engineering evidence but does not substitute for final W3 against the frozen package.

## 10. Failure/rerun policy

- Any final `FAIL` or `NOT_RUN` blocks promotion.
- Preserve failed evidence and classify root cause first.
- Behavior-bearing/packaging correction creates a new candidate/build unless the package identity demonstrably did not change.
- After a genuine runtime correctness change, rerun the owning automated suites, `verify_slice3.py`, full CI, package verification and the affected native matrices; default to full W0/W1/W2/W3 for a new final build.
- Environmental inability remains `NOT_RUN`; narrative waiver is not PASS.
- A docs-only closure after all accepted evidence does not require package rebuild.

## 11. Promotion closure

Passing W3 makes the frozen candidate eligible for final Slice-3 promotion review. A controlling closure must record:

- exact candidate commit;
- exact build ID + package SHA-256;
- automated `verify_slice3.py` result;
- Ubuntu + Windows CI identity/results;
- package verification;
- W0/W1/W2/W3 counts;
- evidence paths;
- migration/schema identity;
- zero unresolved blocking findings;
- whether any behavior/package change occurred after the accepted native run.

Until that closure is committed, Slice 3 must be described as implemented/accepted as appropriate but **not fully promoted**.