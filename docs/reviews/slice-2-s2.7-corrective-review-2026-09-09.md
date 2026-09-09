# Slice 2 S2.7 Corrective Review — 2026-09-09

## Verdict

**S2.7 corrective pass applied; one genuine finding fixed (F1, evidence
mislabel in the health probe), plus two coverage closings (G1/G2), all
with red-first tests. Ready for architecture review.**

This record covers the corrective wave on top of the sealed S2.7 package.
**S2.8/S2.9 remain blocked** and must not begin until a separate explicit
continuation decision.

## Authority and scope

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3**
- S2.7 implementation under review: `bde2fd8a485fc1d251d3ce751542c2172064d476`
  (CI run `34398720026`, SUCCESS Ubuntu + Windows, Python 3.12) plus the
  sealed record `docs/reviews/slice-2-s2.7-review-2026-09-09.md` (`c2b10da`)
- Branch: `arena/01a0877b-windows-job-scraper`

## Method

1. Adversarial re-read of `src/jobscraper/adapters/ashby.py` against 02 §19,
   §22, §31/§32, ACQ-02/03/04/09, RUN-13 and 03 §40, with explicit
   comparison of every emitted value against the sibling adapters
   (Greenhouse/Lever) and against its **host consumers**, not against the
   test suite.
2. Host-consumer tracing for the risky emissions:
   - `job_location_type` / `is_remote` → `pipeline/normalize.py` →
     `pipeline/locations.py::normalize_locations` — the host *documents*
     `job_location_type` as the "(Lever)" remote side-channel
     (`locations.py:335–376`): when it is `REMOTE` every location record is
     coerced `remote: 1`, folded into the same set, never preferred over
     explicit rows. Ashby's structured `isRemote` boolean driving this
     side-channel is **parity with Lever**, not a Greenhouse-G1 divergence
     (Greenhouse has no structured remote signal at all). **H9 closed, no
     finding.**
   - `jobUrl`/"job page" URL gates → same `_reviewed_posting_url` shape as
     Lever F2 (fragment handling identical: config origins fail-closed on
     fragments; content links preserve them as raw evidence — parity across
     all three adapters). **H19 closed, no finding.**
   - Health/SMOKE probe → driver claim loop (`pipeline/driver.py:537–560`
     and `ACQ02_REQUEST_TASK_MAP`): `SOURCE_HEALTH_CHECK`/`ADAPTER_SMOKE`
     are dispatched generically; non-enumeration requests are never linked
     into `coverage_contributing_request` (`driver.py:555–563`), so a probe
     can **never** grant or break absence authority by construction. The
     probe is additionally unreachable from a "health-only run" in
     production (the run planner only enqueues `LIST_FETCH` for the three
     provider adapters), so the probe path matters exactly when it rides
     an enumeration — which is what G1 pins.
3. Mechanical audit: stdlib `trace` line coverage of the adapter over the
   unit + e2e suites (locked venv — no coverage package allowed).
4. Every finding pinned red-first before fixing; adapter restored/verified
   byte-identical where probes used sabotage.

## Findings

### F1 — HEALTH/SMOKE probe evidence mislabeled membership (severity: LOW — evidence honesty)

**Observed.** `_parse_probe` returned
`{"reason": "HEALTH_PROBE_RECOGNIZED", "listed_postings": len(jobs)}` — the
raw array length of the document, named *listed*. On a board whose document
contains `isListed: false` postings (e.g. the `unlisted` fixture), the probe
would report those direct-link-only postings as listed members: durable
evidence stating something the membership gate itself denies.

**Why it matters.** The probe is recognition-only by design, but every
number it records must be exactly what it names — probe evidence is
health-operative input, and "listed" is a defined membership concept for
this provider (§12.3 semantics). A mislabeled counter is the S2.6-F4 class
(evidence saying what it does not mean), here at LOW severity because no
absence or attribution decision reads it (driver probes never link
coverage).

**Fix.** Review evidence now records both, exactly:
`"postings_total": <array size>` and `"listed_members": <count of members
with isListed not False>` — same gate as enumeration, no other parsing.

**Tests.** `TestHealthProbe::test_a_recognized_board_shape_is_a_healthy_probe`
(now asserts both keys on the full board) and the new
`test_a_probe_counts_listed_membership_exactly` (`board_jobs_unlisted.json`
→ `postings_total == 2`, `listed_members == 1`). All three red before the
fix (KeyError on the new keys), green after. No other parse surface
changed; the health outcome kind, ordering, and evidence refs are
untouched.

### G1 — Declared `health` capability never driver-exercised (coverage closing, not a defect)

The `health` capability is declared in the manifest (for all three
providers), the driver dispatches `SOURCE_HEALTH_CHECK` generically
(`task_kind_for_request_type` → `AdapterTaskKind.HEALTH`), but **no suite
for any adapter had ever run a probe through the real dispatch path**.
Observed while tracing claim semantics: on first run the new test exposed
only harness assumptions (`scrape_requests` has no `requested_url` column —
the fetch URL lives in `fetch_attempts` keyed by `request_id`;
`parse_attempts` orders by `parsed_at`), never a driver or adapter
misbehavior.

**Test.** e2e `test_a_health_probe_rides_the_same_pins_and_leaves_coverage_untouched`:
provision the acme board, enqueue `LIST_FETCH` **plus**
`SOURCE_HEALTH_CHECK` in the same run, `execute_run` →
`SOURCE_HEALTH_CHECK SUCCEEDED` with `page_class VALID_LIST`; the probe
fetched exactly the pinned board URL (config-only); its parse attempt is
`SUCCESS_EMPTY` with exact `HEALTH_PROBE_RECOGNIZED` counters; it emits
zero observations while the board parse emits three; the generation stays
`COMPLETE` with `absence_inference_allowed = 1`; and
`coverage_contributing_request` contains **only** the `LIST_FETCH` —
a probe can never be part of the enumeration proof.

### G2 — Refusal-branch coverage closing (11 uncovered lines → 4 reachable, additive unit tests)

Trace showed 11 unexecuted adapter lines. 7 are typed-refusal branches;
each got a unit test proving fail-closed behavior (previously green now,
red-pinned for any future relaxation):

- `board` / `api_base_url` / `company_name` non-string values
  (`AshbyConfig(board=42)`, `api_base_url=True`, `company_name=["Acme"]`,
  etc.) → `ValueError`;
- provider `jobUrl` that is **unparseable** (`"https://exa mple.com/…"`) →
  refused-through-the-safe-URL-gate with `UNSAFE_URL_REFUSED` evidence,
  never weakened into something fetchable;
- a posting without `jobUrl`/`applyUrl` keys → honest absence: no link
  fields, no refusal drama, derived apply link still present;
- `address.postalAddress` projection: nulls/booleans are holes, numbers
  stay numeric, strings are trimmed.

The remaining 3 unreachable lines are defensive nets unreachable through
`net.urlnorm` (host-less origins always raise `UrlNormalizationError` first
— verified by probing the grammar) and one `# pragma: no cover` fallback;
they mirror the sibling adapters line-for-line and were left untouched
(corrective reviews do not make cosmetic drift from reviewed twins).

## Non-findings checked and closed

- **`isListed` non-boolean handling** (kept as member + evidence): matches
  the documented provider contract (field always present, boolean);
  over-inclusion can never create absence authority for a *removed* job
  (removed ⇒ absent from the document ⇒ not observed).
- **`apiVersion` gate both directions**: present-but-different, wrong type,
  absent — all PARTIAL (unit-parametrized); e2e `apiv2` proves the durable
  degradation.
- **Cross-board / cross-posting re-attribution** (S2.6-F2 analogue):
  param-matrix over other boards, other postings, API-shaped URLs,
  look-alike hosts, traversal, active schemes — all refused-into-evidence
  with the derived link still authoritative; e2e deny-scheme sweep across
  durable columns.
- **PARTIAL → COMPLETE laundering** (S2.6-F1 analogue): structurally
  impossible here (no cursor, `next_cursor` is `None` for every outcome
  kind including PARTIAL — pinned by unit tests) *and* e2e-pinned via the
  real driver ([PARTIAL, COMPLETE] generations with `absence_inference_allowed
  = [0, 1]`).
- **Salary honesty**: DF-1 (`parse_salary` first-K limitation) stands as
  recorded; adapter preserves the provider string verbatim.
- **Board-token grammar**: unchanged from the sibling adapters (64-char
  slug + trailing hyphen behavior pinned by contract tests).
- **Purity**: no I/O imports, no provider-host literal in the module, plan
  ignores task-payload URLs, parsing is context-insensitive (all pinned).

## Gate results (exact outputs, this wave)

- Ashby suites: `pytest tests/unit/test_ashby_adapter.py
  tests/integration/test_ashby_e2e.py` → **188 passed** (167 unit + 21 e2e).
- Full suite: **1267 passed, 5 skipped** (baseline 1252/5 + net +14 unit
  +1 e2e; zero regressions — every prior test either passes unchanged or
  was consciously adjusted with its own red-first run).
- Migration state: **UNCHANGED** (`git hash-object
  src/jobscraper/db/migrations.py` = `161704673ea3ac50e93a7e68c9f4bc593b096bf4`).
- Greenhouse/Lever/feed adapters, both slice gates, router/registry/driver:
  **untouched** (`git diff` scope is exactly the three Ashby test/adapter
  files plus this record).

## Sandbox incident note (honesty of the trail)

Mid-review the sandbox suffered its known rollback hazard (git object db
and untracked files reverted; the earlier CI `gh` stream was also observed
emitting phantom run/job ids — final CI state is therefore asserted only
from clean `gh run list` reads after the dust settled). All corrective
edits were re-verified by re-running the suites **after** the rollback;
the commit seals the re-verified worktree state.

**Stop.** S2.7 corrective closed: F1 fixed, G1/G2 pinned. S2.8/S2.9
untouched.
