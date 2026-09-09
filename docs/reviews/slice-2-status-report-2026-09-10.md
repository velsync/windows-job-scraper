# Slice 2 Status Report — 2026-09-10

This is the Slice-2 status record on top of the S2.9 review
(`docs/reviews/slice-2-s2.9-review-2026-09-10.md`). It maps the Slice-2
work-package table (plan v0313 §1) to 06 §72 final acceptance honestly:
Slice-2-owned items are stated as COMPLETE on the evidence actually
executed; browser/Adapter Lab/merge-UI/recipe-promotion/authenticated
sources and the packaged-Windows/native acceptance evidence are marked
later-slice or **pending** — never claimed promoted from Linux/dev/
ordinary-CI evidence alone.

## Automation gates (this session, Python 3.11.2 dev sandbox; CI on 3.12 authoritative)

- **Slice 2 automated gate: PASS**
  `{"contract": "PASS", "slice2_e2e": "PASS", "tests": "PASS",
  "pip_check": "PASS", "migration_v10_to_latest": "PASS", "doctor": "PASS"}`
  (`scripts/verify_slice2.py`).
- Full suite `pytest tests`: **1298 passed, 5 skipped** (baseline 1280/5;
  +18 tests from S2.9; zero regressions).
- Slice 0 gate: **PASS**. Slice 1 gate: **PASS**.
- Migration: **UNCHANGED** (`git hash-object
  src/jobscraper/db/migrations.py` = `161704673ea3ac50e93a7e68c9f4bc593b096bf4`);
  **no new migration**; v10→latest verification read-only over released
  steps.

## Work-package status map

| Pkg | Deliverable | Status |
|---|---|---|
| S2.0 | plan + contract/acceptance skeleton, slice-boundary pins | COMPLETE |
| S2.1 | richer `ResultEnvelope`/evidence + ACQ-09 fields + origin resolver, migration v11 | COMPLETE |
| S2.2 | company model, multi-location, provenance selection + origin-identity dedup, migration v12 | COMPLETE |
| S2.3 | deterministic cleaning + FTS5 search, migration v13 | COMPLETE |
| S2.4 | ATS fingerprinting + router + provisioning, migration v14 | COMPLETE |
| S2.5 | Greenhouse adapter + fixtures | COMPLETE |
| S2.6 | Lever adapter + fixtures | COMPLETE |
| S2.7 | Ashby adapter + fixtures | COMPLETE |
| S2.8 | cross-provider acceptance E2E gate | COMPLETE (12 scenarios) |
| S2.9 | automated gate, migration verification, packaging/native prep, corrective review, status | **COMPLETE (automation) / native & packaged evidence PENDING** |

## DF closure

| Finding | Fix | Evidence |
|---|---|---|
| DF-1 `parse_salary` first-number K-suffix range collapsed max | `normalize.py` `_RANGE_RE` per-endpoint currency+K | red-first `test_normalize_salary.py` (7); Ashby e2e spine `salary_max == 210000` |
| DF-2 `FeedApiAdapter._failure` dropped `evidence_refs` | `feed_api.py` `_evidence_refs` + `_failure(result, …)` | red-first `test_feed_api_adapter.py` (11) |

No new migration, no `acquisition_evidence` CHECK value added (v11 set
unchanged). Prior corrective findings (S2.6 F1–F4, S2.7 F1/G1/G2, S2.8 F1)
remain sealed and were not reverted (F1 `family IS ?` in
`runtime/provisioning.py` re-verified intact).

## 06 §72 final-acceptance mapping for Slice-2-owned scope

Slice 2 delivers (through the real driver + durable pipeline, proven by the
acceptance E2E and the full regression): collect from retained remote feeds
(§72.4) and from Greenhouse/Lever/Ashby (§72.5); preserve discovery/canonical/
origin/application URLs (§72.6); add a company careers URL with ATS
fingerprint evidence (§72.7); one company across observations (§72.8);
multi-location jobs (§72.9); normalized salary with unknown-salary distinct
(§72.11 — DF-1 fix makes provider K-suffix ranges honest, never fabricated);
deterministic score breakdowns (§72.12); FTS5 search with honest
SUBSTRING_FALLBACK (01 §45, §72.13); source observations + adapter-version
provenance (§72.14); merge duplicates without deleting provenance
(origin-identity merge, §72.15); observe a run pinning a specific
binding/adapter version and config pinning (§72.17); binding-specific and
rolled-up source health (§72.21); distinguish valid source-empty from parser
failure (§72.22); rate limiting/backoff without challenge bypass (§72.23);
revalidation without treating one failed scrape as closed (absence authority,
§72.24); and the exact best application link derived from the endpoint table
(§72.34). Inbox-visible eligible jobs (§72.10, §72.12) are drained by Slice 2
obligations; the full Inbox workflow (§72.33), applications ledger (§72.35),
and export (§72.39–40) are Slice-1 surfaces preserved, not re-scoped.

## Honest non-claims (later-slice or pending)

- **Packaged Windows / native acceptance: PENDING.** The sandbox cannot
  build/execute a packaged Windows build. `verify_packaged_build.py` and
  `native_acceptance.py` Slice-2 execution, the packaged resource-manifest
  verification, and all native Windows evidence are recorded as **pending**
  (never run here, never claimed). Slice 2 is **not** claimed PROMOTED.
- **Not Slice-2 scope (later slices):** launch + browser dashboard (§72.1–2,
  Slice 0), profile workflow beyond the Inbox-eligibility slice (§72.3,
  §72.33), undo-merge (§72.16), resume from a compatible cursor across a
  *packaged* run / stale-worker lease-loss (§72.18–19, hardening/ROAD), recipe
  fallback / promotion / rollback (Adapter Lab §72.26–29, ROAD-06),
  authenticated/browser acquisition + isolation (§72.30–32, ROAD-07),
  application lifecycle + reminders (§72.35–36), diagnostics/export redaction
  breadth (§72.38–40), verified backup upgrade on packaged targets (§72.41 —
  the migration gate here is over the released SQL steps, read-only), and the
  full resource/disk budget measurement on the target Windows system
  (§72.43).
- No Slice-3+ objects (recipes, navigation plans, frontier/coordinator,
  contacts/exports, browser/authenticated classes) were created or claimed.

## Files

`docs/reviews/slice-2-s2.9-review-2026-09-10.md` and this record are the
S2.9 deliverables alongside `scripts/verify_slice2.py`, the DF-1/DF-2 fixes
and their red-first tests (see the S2.9 review for the full file list and
exact gate outputs).
