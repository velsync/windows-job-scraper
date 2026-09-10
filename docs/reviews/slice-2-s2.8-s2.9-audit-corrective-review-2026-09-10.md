# Slice 2 — S2.8/S2.9 end-to-end audit and corrective review — 2026-09-10

## Verdict

**S2.8 and S2.9 are acceptance-ready on the automated evidence; packaged
Windows/native acceptance remains PENDING and is not claimed.** The audit
found and fixed **two proven defects** (F1, F2) plus **one missing
acceptance-case set** (F3) and **one gate-coverage gap** (F4) in the §12.1
durable `SOURCE_DISCOVERY` path. No defect was found in the discovery
path's refusal, budget, security-evidence or probe-history behaviour itself —
those were **verified correct and were previously unpinned**; they are now
pinned.

All gates green: contract **73**, Slice-2 E2E component **31**, full
regression **1327 passed / 5 skipped**, `pip check` clean, migration
v10→latest **PASS** (blob `161704673ea3ac50e93a7e68c9f4bc593b096bf4`
**unchanged**), Doctor **PASS**; Slice 0 and Slice 1 gates **PASS**.

Two sabotage probes confirm the new coverage is load-bearing rather than
vacuous (see "Sabotage verification").

## Lineage and the fork this audit resolved

This work continues the Slice-2 corrective lineage from the S2.9 tip
`e04fd3ab5532361e15110235fe482b809b66bef3`. **Two sibling implementations of
the S2.8 generic-discovery corrective existed on origin**, both parented on
`e04fd3a`, and the audit had to choose one:

| Branch | Placement | CI | Local |
|---|---|---|---|
| `repair/s2.8-discovery-corrective-20260910` (`6035eb6`) | `runtime/discovery.py` + `adapters/generic_discovery.py` | **SUCCESS** (run `34446064016`) | **1301 passed / 5 skipped** |
| `repair/s2.8-durable-discovery-provider-policy-2026-09-10` (`0d872cf`) | `pipeline/discovery.py` + driver/policy widening | **FAILURE on every push** | **2 failed / 1310 passed** |
| `repair/s2.7-health-version-parity-2026-09-10` (`7722f33`) | S2.6/S2.7 health-probe correctives + records | **SUCCESS** (run `34442326188`) | new tests 10/10 |

Ancestry was checked explicitly: `7722f33` **is** an ancestor of the red
branch but **is not** an ancestor of `6035eb6`, and `6035eb6` is not an
ancestor of the red branch — the two S2.8 attempts are true siblings, not
superseding revisions.

The red branch was not merely flaky. Its own change that widened the
provider API host failed on *unreviewed* entry hosts, caught by its own
test — a **policy over-permission bug**, not a timeout:

```
FAILED tests/integration/test_ashby_e2e.py::test_host_policy_widens_a_reviewed_ashby_entry_host_to_the_api_host_only
FAILED tests/integration/test_lever_e2e.py::test_host_policy_widens_a_reviewed_lever_entry_host_to_the_api_host_only
2 failed, 1310 passed, 5 skipped
```

**Decision (user-delegated, "which do you recommend?"): the union of the
green correctives.** `6035eb6` (the named branch, S2.8 discovery) and
`7722f33` (S2.6/S2.7 health-probe correctives) were merged into the session
branch; the red S2.8 attempt was **dropped as superseded and self-broken**.
Its two file sets do not overlap, so the merge was clean.

### Branch note (disclosed per execution discipline)

This execution session is hard-bound by Arena to
**`arena/01a0882c-windows-job-scraper`**. Prior records name
`arena/01a08691-…`, `arena/01a0877b-…`, `arena/01a087f1-…` and
`arena/01a0882c-…`; the corrective `repair/*` branches above are not session
branches. Per instruction, **all commits and the push live on
`arena/01a0882c-…` only** — no `repair/*` branch was moved, force-pushed or
committed to; their commits were merged in, preserving their original SHAs
so the CI evidence above still refers to real commits.

## Authority and scope

- Authoritative specification: **v0.3.1.3** — 02 §12.1 (fingerprint →
  candidate → confidence+evidence → router; "Discovery itself is not
  pre-queue I/O … durable `SOURCE_DISCOVERY` request and a built-in immutable
  generic discovery binding/revision … Crash/restart resumes from that
  durable identity; later specialized bindings do not rewrite the probe
  history"), 02 §12.2 (strategy order, `MANUAL_UNSUPPORTED`), 03 §16/§18 +
  RUN-07/RUN-09 (claim/lease, restart reclamation), 04 §5.1 + SEC-02/03/09
  (host-owned destination policy, scheme gate, content never widens
  authority), 06 §72.7/§72.22/§72.50.
- Plan: `docs/plans/slice-2-worker-implementation-plan-v0313.md` §S2.8 (E2E
  gate incl. "the security negatives … and hostile content still denied"),
  §S2.9 (gate, migration verification, packaging prep), §0 rules 3, 4, 6, 8.
- Non-goals honoured: **no architecture redesign, no Slice-3+ scope.** The
  audit added one loader function required by §12.1, rewired the existing
  gate to the existing production primitive, and pinned behaviour; it did not
  introduce a crawler, frontier, recipe, browser or scheduler surface.

## Findings

### F1 — no durable resume path (severity: MEDIUM — §12.1 restart guarantee was not real) — FIXED red-first

**Observed.** `execute_source_discovery(conn, queued, …)` required the
in-memory `QueuedDiscovery` value, and the module exposed no way to rebuild
it from committed rows. §12.1 requires that "crash/restart resumes from that
durable identity", so a genuine process death left the probe
uncontinuable — the committed Source/binding/plan/request could not be turned
back into work.

The pre-existing "restart" test did not catch this because it **carried the
Python object across the reopen** (`queued` was still in scope after
`db.close()`), so it proved that SQLite persists, not that the identity is
resumable.

**Fix (narrow, no schema change).** Added `load_queued_discovery(conn, *,
request_id=None, run_id=None)`, which re-derives the continuation from
`scrape_requests` ⋈ `run_source_plans` and re-validates the pinned plan, the
provisional Source, the binding/revision and the adapter identity before any
I/O is planned. A request that is still `RUNNING` is **refused** with a
message pointing at restart reclamation rather than silently re-executed, so
03 §18/RUN-07 keeps sole authority over orphaned ownership.

**Tests (red-first).** `test_queued_discovery_survives_process_restart_before_claim`
now `del`s the in-memory value and resumes from durable rows only; the new
`test_crashed_probe_resumes_after_restart_reclamation` claims the request,
"dies", runs `recover_interrupted_requests`, and resumes — asserting the
stale `RUNNING` request is refused before reclamation, that the RUN-07
backoff is honoured, that exactly **one** network probe happens in total, and
that the attempt ledger shows `ABANDONED` then `SUCCEEDED`. Red before the
fix (`ImportError`/unresumable), green after.

### F2 — the S2.8 E2E gate contradicted the corrective and bypassed the production path (severity: HIGH — the gate did not exercise what it claimed) — FIXED

**Observed.** Three mutually reinforcing problems:

1. **Dead production code.** `queue_source_discovery` /
   `execute_source_discovery` were called by **nothing but their own test**
   (`grep` over `src/`, `tests/`, `scripts/`). The §12.1 boundary existed as
   an unwired library function.
2. **The E2E gate fabricated its own probe.** `test_slice2_acceptance.py`'s
   `_probe` built an `ExecutionPlanEnvelope` with invented identities
   (`req-acceptance-probe`, `run-acceptance-probe`, `src-acceptance-probe`, …)
   that exist nowhere durably, and performed the first careers-page fetch as
   a raw host-executor call — i.e. the very "pre-queue I/O" §12.1 forbids —
   while its docstring asserted the now-false premise *"Slice 2 has no
   generic-discovery binding yet"*.
3. **The gate disagreed with the corrective.** Scenario 3 asserted
   `"generic" not in BUILTIN_ADAPTERS` and
   `source_adapter_binding_revisions == 0`, while the corrective registered
   `generic_discovery` as a built-in and the contract suite **requires** it.
   The suite passed only because the production path was never wired in.

**Fix (no production behaviour change beyond F1).**
`tests/integration/test_slice2_acceptance.py` step 1 is now the production
durable probe: `_durable_probe` calls `queue_source_discovery` +
`execute_source_discovery` and asserts the ordering claim with an observed
network counter — the handler counts requests, and the suite proves **no
byte left the process before the durable request existed**, and that the
Source, binding, binding revision, pinned plan and `SOURCE_DISCOVERY`
request rows all exist first. The chain consumes the production-computed
fingerprint/route decision instead of recomputing them, and specialization is
asserted to land on the probe's own Source.

Fingerprint and route-decision writers are append-only, so
`_provision_from_route` **stops** re-recording the probe's evidence (that
would duplicate it); the specialization now appends only the provider
binding.

The raw-fetch `_probe` was **kept, renamed `_executor_probe`**, and its
docstring corrected: it is the instrument for the 04 §5.1 security negatives
that must assert the *acquisition boundary's own* shields independently of
request plumbing. Removing it would have destroyed real coverage; leaving its
false docstring in place would have repeated the defect.

Scenario 3's assertions were reconciled to the corrective reality: the
`generic_discovery` **probe** binding is expected (that is the §12.1
requirement), no **specialized** binding may be fabricated from a family-less
fingerprint, and `GENERIC_DISCOVERY_NOT_IMPLEMENTED` stays as the honest
unsupported evidence for generic **job enumeration** (ROAD-04) — the two
distinct meanings of "generic discovery" are now separated explicitly in
`router._generic_fallback`'s docstring, whose stale "S2.5 has not graduated
that adapter yet" premise was corrected.

Dead helpers and imports left behind by the rewire
(`_fingerprint_and_route`, `classify_content`, `plan_routes`,
`record_fingerprint`, `record_route_decision`, `DiscoveryOutcome`) were
removed rather than left as noise.

### F3 — §12.1 negatives were unpinned (severity: MEDIUM — plan §S2.8 requires them) — COVERAGE ADDED

Plan §S2.8 requires the acceptance gate to include the security negatives
"redirect to loopback/private, oversized body, `javascript:` link, **hostile
content**". For the discovery path those were **empirically correct but
untested**. Verified by direct probe, then pinned in the new
`tests/integration/test_slice2_discovery_boundary_audit.py` (16 tests):

| Case | Verified behaviour | Now pinned |
|---|---|---|
| 404 / 403-challenge / 429 / login / `application/octet-stream` / malformed JSON / JS shell | classified; **no fingerprint, no route decision**, run `PARTIAL`, durable `PAGE_VALIDITY`; no coverage or observation fabricated | yes (parametrized) |
| Body over the byte budget | hard-stopped, `POLICY_REJECTED`/`BODY_TOO_LARGE`, durable `SECURITY_POLICY` evidence `DENIED:BODY_TOO_LARGE`, never parsed | yes |
| `javascript:` / `file:` target | refused **before any row exists** — no Source, no request, so no network authority is ever conferred | yes |
| `ftp://` target | accepted as a Source identity, denied at the boundary (`SCHEME_FORBIDDEN`) with durable security evidence, no classification | yes |
| Hostile page naming private/link-local/foreign hosts | content **cannot** widen `allowed_hosts` or construct a grant; exactly one request; loopback grant stays the exact literal | yes |
| Later specialized binding | same Source; probe request/attempt/evidence/fingerprint/route rows byte-identical; both bindings coexist, provider binding becomes current, probe revision intact | yes |
| Re-queueing one careers URL | same Source identity, new request provenance, no duplicate Source/binding | yes |

### F4 — S2.9 gate did not cover the discovery boundary (severity: LOW) — FIXED

`scripts/verify_slice2.py`'s `slice2_e2e` component ran only
`test_slice2_acceptance.py`, so the durable-discovery suites were merely
incidental members of the general regression bucket. The component now runs
all three files (**31 passed**), and the script's header and the gate's
documented claim map were updated to match.

## Sabotage verification (the new coverage is load-bearing)

Each probe was applied to the live tree and reverted, with `sha256sum -c`
confirming byte-identical restoration:

| Probe | Expected | Result |
|---|---|---|
| S1: discovery stops recording its fingerprint/route evidence | E2E gate must fail | **RED** — 5 failed (scenarios 1×3, 3, 4) |
| S2: S2.8 F1 NULL-safe predicate reverted (`family IS ?` → `family = ?`) | family-less fallback scenario must fail | **RED** — scenario 3 `ProvisioningError` |
| F1: `load_queued_discovery` absent | restart tests must fail | **RED** — `ImportError` at collection (red-first) |

## Gate results (exact, this session; Python 3.11.2 dev sandbox)

```
contract suite .......................... 73 passed
Slice-2 E2E component (3 files) .......... 31 passed
full regression (pytest tests) ........... 1327 passed, 5 skipped
```

- `python scripts/verify_slice2.py` → **SLICE 2 AUTOMATED GATE: PASS**
  `{"contract": "PASS", "slice2_e2e": "PASS", "tests": "PASS",
  "pip_check": "PASS", "migration_v10_to_latest": "PASS", "doctor": "PASS"}`
  (migration: `applied [11, 12, 13, 14]`, all 24 seeded rows preserved
  across 13 tables).
- `python scripts/verify_slice0.py` → **PASS**; `python scripts/verify_slice1.py`
  → **PASS**.
- **Migration state UNCHANGED**: `git hash-object
  src/jobscraper/db/migrations.py` = `161704673ea3ac50e93a7e68c9f4bc593b096bf4`;
  `src/jobscraper/db/` untouched by this package.
- Test-count ledger: union baseline **1311/5** → after audit **1327/5**
  (**+16**, all in the new audit module; the acceptance suite count is
  unchanged at 12 scenarios, but its step-1 now drives production code).
- Dev environment rebuilt from `requirements/dev.lock.txt` (Python 3.11.2,
  `pip check` clean, no new packages). CI on Python 3.12 is authoritative.

## Records correction (append-only)

The sealed S2.8 review (`docs/reviews/slice-2-s2.8-review-2026-09-10.md`,
commit `169d880`) states that the careers-page probe "remains a host-executor
fetch (not a run request) because Slice 2 has no generic-discovery binding"
and that "a durable generic-discovery binding remains future-slice work
(ROAD-04 direction) and is not claimed here." **That statement is superseded
and is corrected here**: 02 §12.1 requires the durable
`SOURCE_DISCOVERY` request and the built-in immutable generic discovery
binding *before the first probe*, so the deferral was a contract mismatch,
not a valid postponement. What remains ROAD-04 is generic **job
enumeration** (crawler breadth) — a different thing from the first-probe
planner. The sealed record is left byte-identical per append-only discipline;
this record is the correction of record.

## Files in this package

New: `tests/integration/test_slice2_discovery_boundary_audit.py` (16 tests);
this record.
Modified: `src/jobscraper/runtime/discovery.py` (F1:
`load_queued_discovery` + `_DISCOVERY_RESUME_SQL` + `__all__`);
`src/jobscraper/adapters/router.py` (docstring only — the two meanings of
"generic discovery");
`tests/integration/test_slice2_acceptance.py` (F2: durable step-1 probe,
observed-network ordering proof, run-scoped assertions, reconciled
scenario 3, dead-code removal);
`tests/integration/test_slice2_discovery_durability.py` (F1: true-restart
resume); `scripts/verify_slice2.py` (F4: e2e component + claim map).

Merged into the session branch (preserving original SHAs):
`6035eb6`… and `7722f33`… as recorded above. No change to
`verify_slice0.py` / `verify_slice1.py`, no spec-authority edit, no migration
or schema edit, no adapter behaviour change.

## Remaining limitations (honest)

- **Packaged Windows/native acceptance: PENDING.** `scripts/verify_packaged_build.py`
  and `scripts/native_acceptance.py` Slice-2 execution cannot run in this
  sandbox (the 5 skipped tests are the Windows-native/packaging skips).
  Slice 2 is **not** claimed PROMOTED from Linux/dev/CI evidence.
- **No operator-facing entry point for the probe.** `queue_source_discovery`
  is the Slice-2 building block; the "add a careers URL" product flow that
  would call it arrives with the sources UI in a later slice. The primitive is
  now exercised end-to-end by the E2E gate, but no shipped route calls it yet —
  stated plainly rather than implied.
- **Resume requires the restart authority.** A crashed *mid-flight* probe
  resumes only after `recover_interrupted_requests` runs under the fresh
  service epoch, and honours the RUN-07 retry backoff. This is intended
  (single-writer ownership), and is now pinned by test.
- Upstream `repair/*` branches were not updated; the merged work lives on the
  session branch only.

**Stop.** S2.8/S2.9 audited and corrected. Any further Slice-3+ work requires
a separate explicit continuation decision.
