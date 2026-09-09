# Slice 2 S2.7 Review — Ashby provider-native adapter — 2026-09-09

## Verdict

**S2.7 (Ashby Job Posting API code adapter + deterministic fixture corpus +
router/registry/provisioning contract path) COMPLETE.** Full local gate
(`1252 passed, 5 skipped`), Slice 0 and Slice 1 automated gates PASS, CI run
`34398720026` **SUCCESS on both platforms** (`Python 3.12, ubuntu-latest`
2m42s; `Python 3.12, windows-latest` 5m1s). No invariant was weakened;
S2.8 (cross-provider acceptance) and S2.9 (`verify_slice2.py`) remain
untouched.

This record covers the S2.7 package only. **S2.8/S2.9 remain blocked** and
must not begin until a separate explicit continuation decision.

## Authority and scope

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3** (02 ACQ-02/03/04/09, §19,
  §22, §31/§32; 03 §40; RUN-13; acceptance §72.11)
- Accepted base: `77a9def27322598ed5855acf971f35c652bc4f92`
  (pre-S2.7 host-invariant hardening seal)
- S2.7 implementation commit: `bde2fd8a485fc1d251d3ce751542c2172064d476`
  on branch `arena/01a0877b-windows-job-scraper`
  (24 files, +3624/−9)
- CI: run `34398720026`, `vite` n/a — `.github/workflows/ci.yml`
  "Automated test gate", push event on the session branch
- Regression count vs. accepted baseline: **0** (baseline 1078 passed /
  1 skipped at `77a9def`; now `1252 passed / 5 skipped`; the 4 additional
  skips are Windows-only host security tests skipped on the Linux sandbox,
  unrelated to S2.7 — `git diff 77a9def2..bde2fd8 -- tests/windows
  tests/unit/test_paths.py` is empty)

### Branch note

The S2.7 plan and prior records name `arena/01a08691-windows-job-scraper`.
This execution session is hard-bound by Arena to
`arena/01a0877b-windows-job-scraper` (commit/push only there), so the
implementation commit lives on the session branch. `01a08691` (HEAD =
accepted base `77a9def`) can fast-forward to `bde2fd8` when wanted.

### Workspace provenance note (disclosed per execution discipline)

During inspection a complete S2.7 working-tree package (adapter, fixtures,
suites, wiring) appeared, temporally racing earlier reads in this session.
Provenance could not be established, so it was **not trusted**: the package
was audited from zero against the specification and the S2.5/S2.6 corrected
standards, and every pass claim was re-derived independently (sabotage
red→green pairs, full suite, gates, CI). The commit seals that verified
state; this record is the audit trail of the verification, not of blind
adoption.

## Provider contract facts (researched 2026-09-09; live-verified)

Sources: live `GET https://api.ashbyhq.com/posting-api/job-board/ashby`
(fetch 2026-09-09, full document inspected — Ashby's own public board),
live `GET /posting-api/job-board/ashby/job/{id}` → **401 Unauthorized**,
and the official Job Postings API docs
(`https://developers.ashbyhq.com/docs/public-job-posting-api`).

1. **One endpoint, one document.** Unauthenticated
   `GET /posting-api/job-board/{org}?includeCompensation=true` returns a
   single JSON document `{"jobs": [...], "apiVersion": "1"}` containing the
   **entire published board membership with every field inline**
   (`descriptionHtml`/`descriptionPlain`, `location` + `secondaryLocations[]`
   with structured `address.postalAddress`, `isRemote`, `workplaceType`,
   `employmentType`, `publishedAt`, `isListed`, `jobUrl`, `applyUrl`, and
   `compensation{}` when requested). **No pagination, no cursor, no
   declared total** — one recognized response is the whole membership.
2. **No public per-job detail endpoint** (`/posting-api/job-board/{org}/job/{id}` → 401).
   Detail-fetch capability would be a false claim; the adapter declares no
   `detail_parse`, plans no `DETAIL` task, and sets
   `listing_identity_sufficient = True` honestly.
3. **Board token case-insensitivity** (live-verified: `/job-board/Ashby` and
   `/job-board/ashby` return the same document; `jobUrl` echoes the
   request's spelling) — the pinned token is canonicalized to lowercase
   exactly like the sibling providers.
4. **`isListed: false`** = direct-link-only; **not** public-board
   membership (excluded with typed review evidence, never observed).
5. **`publishedAt`** is the last *publication* time → legitimate
   `posted_at` (contrast Lever's `createdAt`, which stays evidence-only).
6. **`jobUrl` = hosted posting page; `applyUrl` = `{jobUrl}/application`** —
   both arrive inline and are accepted only under the board+id gate.
7. **Compensation** comes only with `includeCompensation=true`;
   `scrapeableCompensationSalarySummary` is the salary signal kept verbatim.
8. **Employment vocabulary**: `FullTime|PartTime|Intern|Contract|Temporary` —
   `"PartTime"` concatenated, which drove the one host-normalization
   addition below.
9. **Contract stamp**: `apiVersion == "1"` is the only version this parser
   was reviewed against (see PARTIAL rules).

## Semantics implemented (the decisions)

| Question | Decision |
|---|---|
| Listing identity sufficiency | Suffice from enumeration alone: full membership + source-native ids inline, no detail endpoint exists. `listing_identity_sufficient = True`; exactly one `LIST_FETCH` ever; **zero `DETAIL_FETCH` assertions in the e2e**. |
| PARTIAL vs FAILURE | Rejected listed member, or `apiVersion != "1"` (either direction: value present-but-different, wrong type, or absent) → `PARTIAL` (good observations persist; membership proof broken; host durably degrades the generation, `absence_inference_allowed = 0`). Changed template / all members invalid / malformed body → `PARSE_MARKER_MISSING` FAILURE with ACQ-09 evidence refs — never `SUCCESS_EMPTY`. Recognized empty board (incl. unlisted-only) → `SUCCESS_EMPTY`, terminal. |
| Closure evidence | Absence-only: a posting leaves the document → later COMPLETE generation judges it `UNCERTAIN` with `last_absence_coverage_id` naming the proving generation (RUN-13, 03 §40). A PARTIAL generation judges nothing; the next clean one does. |
| Health probe | HEALTH/SMOKE plan the same pinned board URL and parse recognition-only (`HEALTH_PROBE_RECOGNIZED`, listed count); unrecognized shape → typed failure; never observations. |
| Cursor | None, always: `next_cursor` returns `None` for terminal, partial, probe, and failure outcomes alike. There is no page two to propose and a bounded PARTIAL must never be laundered via a cursor (S2.6 F1 lesson — here structurally impossible, still pinned by tests). |
| Fingerprint markers | Pre-staged at S2.4 (`acquisition/fingerprint.py` markers, `atsendpoints.py` ASHBY spec with strict board+UUID-id patterns, `router.py` ASHBY family candidate, `_IMPLEMENTED_STRATEGIES`, driver widening map). S2.7 flips the set: `"ashby"` registered in `BUILTIN_ADAPTERS` + `EXPECTED` contract set; the not-yet-implemented exemplar in corrective tests moves ASHBY→WORKDAY with docstrings updated to keep checking the invariant (a *graduated* provider must route; an ungraduated family must never become runnable). |
| Links | `jobUrl`/`applyUrl` accepted only when the versioned endpoint table reads them as **this board's, this posting's** hosted URL (`applyUrl` = posting URL + `/application`, query-free gate — parity with the reviewed Lever F2 gate; a `?utm_source=` tracked variant is raw evidence, while the product apply link is **always** the derived table URL `https://jobs.ashbyhq.com/{board}/{id}` so hostile/tracked content never becomes link authority). Traversal ids, look-alike hosts, other boards, other postings, active schemes → bounded review evidence. |
| Fixtures | Deterministic `tests/fixtures/ashby/` (13 files + README): valid board (3 postings: OnSite+compensation, PartTime Hybrid multi-location via `secondaryLocations`, Remote with hostile `applyUrl` and `javascript:`/script/`data:` residue), removed (closure), empty, unlisted variants, rejected-member, changed-template, malformed, api-v2, 429 rate-limit, 403 challenge. Synthetic corpus; provider-shape notes recorded with live-verification dates. |

## One host-normalization addition

`pipeline/normalize.py` `EMPLOYMENT_TYPES += {"parttime": "PART_TIME"}`)
— the accepted Slice-1 single-writer normalization layer did not admit
Ashby's documented concatenated spelling (`PartTime`), so provider-stated
employment types silently became `null`. One-line additive vocabulary
admission (normalization owns the vocabulary; the adapter must not coerce).
Red-first via the e2e spine (fixture job 2), then green. No other normalize
behaviour changed (`git diff 77a9def2..bde2fd8 -- pipeline/normalize.py` is
exactly the one key).

## TDD narrative (verification evidence)

Red-first was confirmed at package level (pre-implementation collection
import error; no `ashby` module). During unit development three defect
classes surfaced and were fixed red→green before any PASS claim:
1. `review_evidence` packing bug in the rejected-member path
   (`*(_refusal_review(refused),)` nested an empty tuple →
   `TypeError: tuple indices must be integers or slices, not str`);
2. two over-strict test expectations diverging from the sibling adapters'
   board-slug grammar (64 chars and trailing-hyphen accepted, matching
   `^[a-z0-9][a-z0-9_-]{0,63}$` of Greenhouse/Lever) — fixed in tests;
3. tracked `applyUrl` (`.../application?utm_source=feed`) handling pinned
   to the reviewed Lever-F2 parity (raw evidence preserved, derived link
   authority).

Independent sabotage probes (by diffing the sealed source, not the test
suite): flipping the `isListed is False` gate → exactly the 6
membership/exclusion tests failed; widening the `apiVersion` gate →
exactly the 9 coverage-authority tests failed; removing the `parttime`
alias → the e2e spine's `PART_TIME` assertion failed. Restored
byte-identical (sha256-verified) each time, then green. Hostile fixture
shape: `jobUrl` honest / `applyUrl` hostile (one variant of each covered
end-to-end: refused-with-evidence vs. accepted-as-candidate).

## Gate results (exact outputs)

- Targeted suites: `pytest tests/unit/test_ashby_adapter.py
  tests/unit/test_router.py tests/unit/test_provisioning.py
  tests/unit/test_s2_4_correctives.py tests/contract/test_slice2_contract.py`
  → **235 passed**.
- Ashby suites: `pytest tests/unit/test_ashby_adapter.py
  tests/integration/test_ashby_e2e.py` → **173 passed**.
- Full suite: `1252 passed, 5 skipped` (zero regressions; every prior test
  either passes unchanged or was consciously retargeted with its docstring
  updated — see files list).
- Slice 0 automated gate: **PASS** (`tests PASS, pip_check PASS, doctor PASS`).
- Slice 1 automated gate: **PASS** (`contract PASS, acceptance_e2e PASS,
  tests PASS, pip_check PASS, doctor PASS`).
- Migration state: **UNCHANGED** —
  `git hash-object src/jobscraper/db/migrations.py` =
  `161704673ea3ac50e93a7e68c9f4bc593b096bf4` (no new migration; no new
  write path; no new I/O surface: the e2e drives the same durable
  request/envelope/validity/observation pipeline as S2.5/S2.6).
- CI: run `34398720026` — **ubuntu-latest 2m42s ✓, windows-latest 5m1s ✓**
  (Python 3.12, exact dev lock on both platforms).

## Files in the commit

New: `src/jobscraper/adapters/ashby.py`;
`tests/unit/test_ashby_adapter.py`;
`tests/integration/test_ashby_e2e.py`; `tests/fixtures/ashby/` (14 files);
this review record.

Modified (wiring + conscious retargets; nothing else):
`src/jobscraper/adapters/registry.py`,
`src/jobscraper/adapters/router.py`,
`src/jobscraper/pipeline/driver.py`,
`src/jobscraper/pipeline/normalize.py`,
`tests/contract/test_slice2_contract.py`,
`tests/unit/test_router.py`,
`tests/unit/test_provisioning.py`,
`tests/unit/test_s2_4_correctives.py`.

S2.8/S2.9 surfaces: untouched (no `verify_slice2.py`, no cross-provider
acceptance changes). Greenhouse and Lever adapters: byte-identical to the
accepted base.

## E2E matrix executed (20 scenarios, all through the real driver + loopback server)

Full-spine (one request → canonical jobs, coverage COMPLETE +
`AUTHORITATIVE_FULL_SOURCE` + `pages_completed = 1`, field-evidence
locators, origin resolution, derived apply link, search/FTS visibility);
empty board SUCCEEDED; unlisted-only success-empty; unlisted evidence not
membership; rejected-member durable PARTIAL → next clean generation regains
COMPLETE + ID3 UNCERTAIN (real adapter through the host invariant);
apiV2 never grants absence; 429/403 challenge/changed-template/malformed/
all-invalid typed pages with no normal parse; hostile-content deny-scheme
sweep across durable columns; pinned board/origin reachability + host-policy
widening (api host only for reviewed entries); redrive idempotence;
second-run re-observation without duplication; crash-before-outcome-commit
resume SATISFIED; zero `DETAIL_FETCH` assertion; service surface
(provision → `POST /api/runs` → Inbox, apply links, hostile fields absent,
FTS search, profile keyword wiring).

## Deferred findings

- **DF-1 — `parse_salary` first-number K-suffix.** The accepted Slice-1
  range pattern expands the K-suffix only on the *second* number of a
  range, so a provider-verbatim `"$150K - $210K"` normalizes to
  `min=150000, max=150000` (max collapses). Provider shape is real
  (live boards emit exactly this shape, e.g. `"$130K - $224K"`), and the
  raw string is preserved verbatim as evidence + `salary_original_text`;
  the adapter deliberately does not rewrite evidence to please the host
  parser. Slice-1 normalization authority: change proposed for S2.9 with a
  red test (unit: `parse_salary("$150K - $210K")` max assertion; then
  drop the DF-1 comment + `salary_max == 150000` marker assertion in the
  S2.7 e2e spine, flipping it to `210000`).
- **DF-2 (carried, not worsened) — `FeedApiAdapter._failure` lacks
  `evidence_refs`**. Pre-existing S2.6-deferred item; recorded for S2.9;
  deliberately not touched here.
- **Live-surface caveat.** Fixture shapes were live-verified on 2026-09-09;
  the deterministic corpus is the regression authority (02 §26). If Ashby
  later stamps `apiVersion != "1"` on real boards, the reviewed behaviour
  is the conservative one already pinned: observations persist, coverage
  PARTIAL, absence refused — operators see degradation, never wrong
  authority.

**Stop.** S2.7 closed. Do not start S2.8 or S2.9 from this record.
