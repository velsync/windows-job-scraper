# Slice 2 S2.8 Review — cross-provider acceptance / E2E gate — 2026-09-10

## Verdict

**S2.8 (cross-provider integration/acceptance, plan v0313 §S2.8) COMPLETE.**
One acceptance suite — `tests/integration/test_slice2_acceptance.py` — now
functions as the Slice-2 E2E gate: 12 scenarios prove the whole chain
(fingerprint → route → provision → acquire → observation → company/location/
provenance → cleaned/indexed searchable result) for all three graduated
providers, the same-origin merge with employer provenance, the honest generic
and low-confidence fallbacks, FTS mode honesty in all three capability states
(FTS5 / substring / drift), and the security negatives. Full local gate
**1280 passed, 5 skipped** (baseline 1267/5 + 12 acceptance + 1 red-first
unit test; zero regressions), Slice 0 and Slice 1 automated gates **PASS**,
migration bytes **UNCHANGED**.

The acceptance pass uncovered **one real defect (F1)** in a sealed S2.4
surface — the honest generic fallback decision was unrecordable for a
family-less fingerprint. Fixed narrowly with its own red-first test; the fix
is the only behavior-bearing change in this package.

**S2.9 remains untouched and blocked** (no `scripts/verify_slice2.py`, no
migration-from-v10 verification, no packaging/native prep, DF-1/DF-2
untouched). This record does not authorize S2.9.

## Authority and scope

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3** (02 §12.1/§12.2/§12.3, §19, §22,
  §26, §31/§32, ACQ-02/03/04/09; 03 §40; 04 §5.1, SEC-02/03/09; 01 §45;
  06 §72.5/§72.7/§72.8/§72.9/§72.13/§72.14/§72.15)
- Plan: `docs/plans/slice-2-worker-implementation-plan-v0313.md` §S2.8
  (deliverable gate "E2E gate")
- Accepted base: `4de7e3a6c8a814c9473de59188798f24c8c7c0cc`
  (S2.7 corrective seal; CI run `34402176201` SUCCESS Ubuntu + Windows,
  Python 3.12) — verified with `git cat-file -t` after re-fetching
  `refs/heads/arena/01a0877b-windows-job-scraper` from origin (the sandbox
  had reverted to a shallow single-commit clone of `df1879c4`, the known
  hazard; `.git/config` fetch refspec checked first, then the specific ref
  fetched, then `git reset --hard 4de7e3a6…`).

### Branch note (disclosed per execution discipline)

The prior records name session branches `arena/01a08691-windows-job-scraper`
and `arena/01a0877b-windows-job-scraper`. This execution session is
hard-bound by Arena to **`arena/01a087f1-windows-job-scraper`** — a third,
different branch id. Per instruction this is documented rather than fought:
all work and the push live on `arena/01a087f1-…` only; no other branch was
created, moved, or force-pushed. The prior session branch
(`arena/01a0877b-…`, tip = accepted base `4de7e3a6…`) can fast-forward to
this package's commit when wanted.

## What the gate proves (scenario ↔ claim map)

`tests/integration/test_slice2_acceptance.py` (1370 lines, 12 tests) is
written as the slice's e2e deliverable: `scripts/verify_slice2.py` (S2.9)
will aggregate exactly this file as its e2e acceptance component, the role
`test_slice1_acceptance.py` plays for `verify_slice1.py`. The module
docstring carries the full scenario↔authority map; summary:

1. **Cross-provider full chain** (parametrized greenhouse/lever/ashby) — the
   operator's careers URL is probed through the host executor under the
   host's own policy (`driver.source_policy`), fingerprinted
   (`classify_content`, evidence-first), routed (`plan_routes` →
   `PROVIDER_NATIVE` candidate), durably provisioned with the fingerprint +
   route-decision evidence rows and the pinned immutable revision, and the
   driver run delivers canonical jobs with origin resolution (RESOLVED,
   replayable evidence), one strongly-identified company, multi-location
   projection, cleaned content, `EMPLOYER_STRUCTURED_ATS` provenance,
   COMPLETE/absence-authoritative coverage, drained obligations (Inbox), and
   FTS5-searchable results with a safe derived apply link.
2. **Same-origin merge** — the employer's Greenhouse board and a third-party
   `json_api_feed` aggregator observe the same postings: one canonical job
   per posting (§38 stage 2 attach), employer presence wins presentation
   (§39), aggregator presence stays ACTIVE and inspectable with its own
   discovery URL, tracking parameters are cleaned before origin resolution,
   and a hostile `javascript:` apply candidate in the feed survives only as
   immutable evidence — every product-emitted link is safelink-approved and
   the winner's derived link is what `best_application_url` shows.
3. **Generic careers URL** — no ATS evidence → `GENERIC_DISCOVERY_FALLBACK`
   with no runnable candidate, the honest `GENERIC_DISCOVERY_NOT_IMPLEMENTED`
   unsupported record, durable fingerprint/decision evidence, and nothing
   runnable fabricated (no binding, no revision, no invented adapter).
4. **Low-confidence hunch** — a weak single-marker Greenhouse page (0.60 <
   0.70) records the family as evidence and still emits no specialized
   candidate; no binding is provisioned from a hunch.
5. **FTS mode honesty** — FTS5_ACTIVE with no warning when FTS5 is real;
   SUBSTRING_FALLBACK with the explicit warning (never claiming BM25) when
   the host lacks FTS5 — same chain, same query surface, same hits; plus the
   drift state (recorded FTS5_ACTIVE, objects missing) degrades the answer
   honestly instead of claiming BM25 over nothing.
6. **Security negatives** — run-level redirect to a private address denied at
   the redirect hop (durable `SECURITY_POLICY` evidence, run PARTIAL, no
   parse, no observations, no absence authority); a private-range literal
   destination denied by address classification before any connection; a
   redirect to another loopback identity (`localhost` vs the granted literal)
   denied by the exact-host rule; an oversized body capped and typed
   `BODY_TOO_LARGE`, never parsed; a `javascript:` probe URL refused at the
   scheme gate with no I/O.

Fixture provenance: the provider payloads are the reviewed deterministic
corpora (`tests/fixtures/{greenhouse,lever,ashby}/`, live-verified
2026-09-09 per their READMEs — no fixture files were added or modified).
The careers pages and aggregator feed are synthetic marker-bearing documents
defined inline in the suite (they exercise the classifier and the feed
adapter, not any provider's real HTML), and the suite's `NOW`/`LATER`
constants are deliberately pinned in the past (2026-09-09T12:00/14:00Z) so
driver claims stamped with the real UTC clock always sort after the seeded
instants — the sibling suites' convention.

## Finding F1 — the honest generic fallback was unrecordable (severity: MEDIUM — the fallback evidence path crashed)

**Observed (red-first).** `provisioning.record_route_decision` verifies its
causal fingerprint with
`SELECT 1 FROM ats_fingerprints WHERE source_id = ? AND family = ? AND confidence = ?`.
A generic careers page classifies to `family=None` — exactly 02 §12.1's
fallback case and this package's acceptance scenario 3 — and `family = NULL`
matches no row in SQL, so recording the router's honest
`GENERIC_DISCOVERY_FALLBACK` decision raised `ProvisioningError: route
decision requires a matching persisted fingerprint first`. The acceptance
scenario could not run to green: the durable fallback record the plan
requires was unrepresentable through the host's own evidence writer (and
therefore through `provision_source_and_binding(fingerprint=…, decision=…)`
as well).

**Fix (narrow, no schema change).** `family = ?` → `family IS ?` (SQLite's
NULL-safe equality), with a comment naming the finding. Released migration
bytes untouched; no other statement or surface changed.

**Tests.** Red-first
`tests/unit/test_provisioning.py::TestRecordRouteDecision::`
`test_a_family_less_fallback_decision_can_be_recorded` — confirmed failing
(`ProvisioningError`) against the pre-fix module, green after; the
acceptance scenario
`test_a_generic_careers_url_falls_back_honestly_without_a_fabricated_route`
exercises the same path end-to-end and fails when the fix is reverted
(sabotage-verified below).

## Corrective review pass on this package (before sealing)

An adversarial re-read of the finished suite plus **seven sabotage probes**
against sealed surfaces (each verified red on the right tests, then restored
byte-identical, sha256-checked):

| Probe (sealed surface) | Expected red | Result |
|---|---|---|
| Router threshold 0.70 → 0.50 | low-confidence hunch test | RED ✓ |
| §38 stage-2 origin attach disabled (`entity.py`) | merge test | RED ✓ |
| Per-hop redirect gate removed (`httpexec.py`) | both redirect-denial tests | RED ✓ |
| FTS completeness guard removed (`search/query.py`) | FTS honesty test | RED ✓ (after the drift state was added to the test — see below) |
| §39 class ordering flipped (aggregator promoted) | merge test | RED ✓ |
| Fingerprint classifier core disabled | all three full-chain tests | RED ✓ |
| F1 fix reverted (`family = ?`) | generic-fallback acceptance + F1 unit test | RED ✓ |

The probes also produced two corrective tightenings of the package itself:

1. The FTS honesty scenario originally pinned only the two *provisioning*
   states; the sabotage exposed that the *drift* guard (recorded
   `FTS5_ACTIVE`, objects missing) was untested at acceptance level. The
   drift state is now the scenario's third part (the unit suite already
   pinned it; the gate now stands alone).
2. The oversized-body assertion was tightened to the cap plus one read
   chunk (`2_000_000 + 64 KiB`), and the `_probe` helper's signature wart
   (a required-but-unused `path` when `url_override` is given) was removed.

A marker-only fingerprint sabotage (Greenhouse HTML markers deleted) stayed
green **by design**: the careers pages carry independent script-URL and
canonical-link evidence, so single-signal removal does not flip a
multi-signal classifier — the marker table itself is pinned by
`tests/unit/test_fingerprint.py`. The classifier-core sabotage above is the
load-bearing check.

## Gate results (exact outputs, this session)

- Acceptance + unit suites:
  `pytest tests/integration/test_slice2_acceptance.py tests/unit/test_provisioning.py`
  → **39 passed** (12 acceptance + 27 provisioning).
- Full suite: `pytest tests` → **1280 passed, 5 skipped**
  (baseline at `4de7e3a6…` re-verified first as **1267 passed, 5 skipped** —
  1 Linux skip in `test_paths.py` + 4 Windows-native skips; +12 acceptance,
  +1 F1 unit test; zero regressions).
- Slice 0 automated gate: **PASS**
  (`{"tests": "PASS", "pip_check": "PASS", "doctor": "PASS"}`).
- Slice 1 automated gate: **PASS**
  (`{"contract": "PASS", "acceptance_e2e": "PASS", "tests": "PASS",
  "pip_check": "PASS", "doctor": "PASS"}`).
- Migration state: **UNCHANGED** — `git hash-object
  src/jobscraper/db/migrations.py` =
  `161704673ea3ac50e93a7e68c9f4bc593b096bf4`.
- Dev environment: `/home/user/.venv-311` recreated from
  `requirements/dev.lock.txt` (Python 3.11.2, exact lock, `pip check` clean,
  no new packages). CI on Python 3.12 is authoritative.

## Files in this package

New: `tests/integration/test_slice2_acceptance.py`; this review record.
Modified: `src/jobscraper/runtime/provisioning.py` (F1 fix: one predicate,
NULL-safe `IS ?`); `tests/unit/test_provisioning.py` (F1 red-first test +
`plan_routes` import).

## Deferred findings

- **DF-1** (`parse_salary` K-suffix range) and **DF-2**
  (`FeedApiAdapter._failure` evidence_refs): untouched, reserved for S2.9 as
  recorded. The acceptance suite keeps the DF-1 marker observable through
  the Ashby corpus only indirectly (salary honesty is pinned by the S2.7
  suites); this package adds no new salary normalization surface.
- **New deferral from this package: none.** The one defect found (F1) was
  fixed in this package, not deferred.
- The careers-page probe remains a host-executor fetch (not a run request)
  because Slice 2 has no generic-discovery binding; that limitation is the
  router's own durable, honest record (`GENERIC_DISCOVERY_NOT_IMPLEMENTED`)
  and stays the truth the gate pins. A durable generic-discovery binding
  remains future-slice work (ROAD-04 direction) and is not claimed here.

## S2.9 untouched confirmation

No `scripts/verify_slice2.py`, no migration verification from v10, no
packaging/native preparation, no changes to `verify_slice0.py` /
`verify_slice1.py`, no spec-authority edits, no changes to sealed adapter
behavior beyond the documented F1 fix, DF-1/DF-2 untouched.
`git diff 4de7e3a6…` scope is exactly the four files listed above.

**Stop.** S2.8 closed. S2.9 must not start until a separate explicit
continuation decision.
