# Slice 2 S2.6 Corrective Review — 2026-09-09

## Verdict

**S2.6 (Lever provider-native adapter) — corrective pass applied; four
findings fixed, all covered by red-then-green regression tests. Ready for
architecture review.**

This record covers the S2.6 package only. **S2.7 (Ashby) remains blocked**
and must not begin until a separate explicit continuation decision.

## Authority and scope

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3**
- Accepted base: `82763c9e0c5893d8844fe300c66fcb4df680ed91` (S2.4/S2.5
  corrective acceptance seal)
- S2.6 implementation commit under review: `7c0073f9be5fd163e427bfe41d65c235333e6135`
  (CI run `34379108355` — SUCCESS on Ubuntu + Windows, Python 3.12)
- Branch: `arena/01a08691-windows-job-scraper`

The review re-read `src/jobscraper/adapters/lever.py` against 02 §19,
§22, §32, ACQ-02/03/04/09, RUN-13 and 03 §40, and exercised the host driver
with adversarial payload shapes the shipped fixture corpus did not contain.

## Method

1. Full re-read of the adapter, the registry/router/driver graduation
   edits, and the S2.6 unit + e2e suites.
2. Adversarial probes through the **real driver** (loopback server, real
   SQLite, real coverage/obligation pipeline), not adapter-only calls.
3. Host-consumer tracing: for every value the adapter emits, which host
   component consumes it and with what authority (origin resolution, company
   attribution, coverage finalization, stop-policy bounding).
4. Every finding was first pinned by a failing test against the pre-fix
   adapter (`git stash` of `lever.py`), then fixed, then re-run.

## Findings

### F1 — Rejected listed member could be laundered into COMPLETE coverage (severity: HIGH — invariant breach)

**Observed.** With `page_size=2` against a site whose page 1 contains one
valid posting and one member without a usable `id`, and whose page 2 is a
clean short page, the driver run **SUCCEEDED** and the coverage generation
finalized `COMPLETE`, `terminal_enumeration_proven=1`,
`absence_inference_allowed=1`.

**Cause.** The adapter correctly returned `PARTIAL` for page 1 (ACQ-03) but
`continuation_required=True` *and* a continuation cursor. The host driver
treats a proposed cursor as `CONTINUE` and only marks coverage degraded on
the `PARTIAL` signal path, which a cursor proposal pre-empts. Page 2 then
terminated cleanly and finalized the *same* generation as COMPLETE.

**Why it matters.** ACQ-03: "absence inference is forbidden for that
coverage record". RUN-13: no terminal enumeration may be derived from a
unit that did not prove membership. A COMPLETE generation is what
subsequent absence/expiry decisions consume. This path would have allowed a
provider whose listing contained one unreadable member to cause the host to
assert *knowledge of the whole set*.

**Fix (adapter, `next_cursor`).** A `PARTIAL` outcome never proposes a
cursor. The page stays `continuation_required` so the host records a
bounded PARTIAL result (existing driver path); good observations from the
page remain persisted. Greenhouse parity note: the S2.5 adapter is
single-page (`max_pages=1`) and therefore never faced this; the offset
walk is Lever-specific.

**Tests.** `TestCursor::test_a_full_page_with_a_rejected_member_proposes_no_cursor`
(unit); `test_a_rejected_member_on_an_earlier_page_is_never_laundered_into_complete`
(e2e, new fixture `postings_list_rejected_member_paged.json`). Both red
against `7c0073f`, green after.

### F2 — Provider `hostedUrl` accepted for another site or another posting (severity: HIGH — attribution integrity)

**Observed.** `_reviewed_hosted_url` gated only on *host* membership in the
endpoint table's hosted hosts. `https://jobs.lever.co/evilcorp/{ID1}` and
`https://jobs.lever.co/acme/{OTHER_ID}` were both accepted as
`canonical_url_candidate`. Traced downstream: `resolve_origin` prefers
`canonical_job_url` first and returned `RESOLVED, origin_board=evilcorp,
confidence 0.9` — i.e. content re-attributed the job to another employer,
and the `companies` row / `LEVER/evilcorp` identifier would follow.

**Why it matters.** 02 §32 origin resolution is host-owned and the
provider payload is content. The fixture corpus happened to be honest, so
the e2e suite could not see it.

**Fix (adapter).** `hostedUrl`/`applyUrl` are accepted only when the
endpoint table (`identify_url`, `kind == HOSTED`, `job_specific`) reads
them as **the pinned site's, the currently parsed posting's** job URL.
`applyUrl` (`…/{id}/apply`, which the table deliberately does not treat as
job-specific) is admitted as an evidence field via its parent path under
the same gate, query-free. Path traversal, the API host, look-alike hosts
and foreign ids are refused with `UNSAFE_URL_REFUSED` review evidence. The
product application link was already derived from the endpoint table +
pinned site and is unchanged.

**Tests.** `TestListParse::test_a_provider_link_for_another_site_or_posting_is_refused`
(5 parametrized cases) and
`test_this_postings_hosted_and_apply_links_are_accepted_on_every_reviewed_host`
(`jobs`, `eu.jobs`, `hk.jobs`). 3/5 refusal cases were red against
`7c0073f` (the other two were already refused by the host gate).

### F3 — Failure outcomes dropped their ACQ-09 evidence references (severity: MEDIUM — traceability)

**Observed.** Every `FAILURE` `ParseOutcome` was built without
`evidence_refs`, while success outcomes carried the envelope + validity
refs. The Greenhouse adapter has the same shape; this review does not
touch S2.5 (out of scope) but records it for the S2.7/S2.9 sweep.

**Fix.** `_failure` takes the `ValidatedResult` and attaches
`_evidence_refs(result)`; all call sites updated (list, detail, probe).

**Tests.** `TestFailureTraceability` (5 cases: three list fixtures, three
detail bodies, one health probe). All red against `7c0073f`.

### F4 — Adapter could exceed its own declared `max_requests` (severity: LOW — declared bound honesty)

**Observed.** `STOP_POLICY.max_requests=60`, `max_pages=10`, but the
per-page detail cap was `max_requests − max_pages = 50`, so ten full pages
could spawn 10 + 500 requests against a declaration of 60. The host caps
runs independently (`MAX_DETAIL_REQUESTS_PER_RUN=200`), so this was never
exploitable, but 02 §19 makes the declared stop policy a promise the host
bounds against; an adapter must not be able to falsify it by construction.

**Fix.** `max_requests=200`; `MAX_DETAIL_REQUESTS = (max_requests −
max_pages) // max_pages = 19`; `DEFAULT_MAX_DETAIL_REQUESTS = 19`; a
module-level assertion pins `max_pages · (1 + MAX_DETAIL_REQUESTS) ≤
max_requests`.

**Tests.** `TestManifestAndRegistry::test_the_adapter_cannot_exceed_its_own_declared_request_budget`
(red against `7c0073f`).

## Reviewed and accepted as-is

- Config fails closed (unknown keys, control characters, reserved route
  words, credential-bearing origins, host-cap overrides) — verified.
- Detail URL built from pinned site + validated id only; payload-carried
  `board`/`url`/`api_base_url` ignored by construction — verified.
- Plan-scoped offset cursor; foreign/stale/other-schema cursors restart
  from `skip=0`; `ids_hash` repeated-page trap — verified through the
  driver (`ignoreskip` site).
- `createdAt` recorded as evidence, never `posted_at` — verified in
  `field_evidence`.
- Cross-plan detail closure, id mismatch, 403/429/404/malformed/changed
  template all non-terminal — verified.
- Driver `_PROVIDER_NATIVE_ADAPTERS` widening is host-owned reviewed data,
  hosts still sourced from the endpoint table; a Greenhouse entry host
  under the Lever adapter gets no widening — verified.
- Restart recovery between pages resumes the same generation from the
  durable cursor without re-fetching page 1 — verified (crash injected
  at claim time).

## Sandbox note (not a code finding)

The sandbox was reset between the S2.6 delivery and this review: the
clone came back shallow at `df1879c` (`main`) with an unrelated stale
`AGENTS.md` in the working tree. The branch was re-fetched and reset to
`7c0073f` before review; the stray `AGENTS.md` matches the version at
`ad625b5` and is **not** part of this corrective. It was discarded.

## Migration immutability

`src/jobscraper/db/migrations.py` blob SHA:
`161704673ea3ac50e93a7e68c9f4bc593b096bf4` — unchanged from the accepted
S2.4/S2.5 seal. No new migration.

## Verification (Python 3.11.2 sandbox; CI on 3.12 is authoritative)

| Command | Result |
|---|---|
| `python -m pytest tests/unit/test_lever_adapter.py -q` | 159 passed |
| `python -m pytest tests/integration/test_lever_e2e.py -q` | 24 passed |
| Lever + router/S2.4/provisioning/contract | 305 passed |
| `python -m pytest tests/unit tests/contract tests/integration -q` | **1063 passed, 1 skipped** |
| `python scripts/verify_slice0.py` | `SLICE 0 AUTOMATED GATE: PASS` |
| `python scripts/verify_slice1.py` | `SLICE 1 AUTOMATED GATE: PASS` |
| Pre-fix red check (`lever.py` stashed) | 10 unit failures + 1 e2e failure, exactly the new tests |

## Files changed by this corrective

- `src/jobscraper/adapters/lever.py` — F1–F4 (behaviour), module docstring
- `tests/unit/test_lever_adapter.py` — +13 tests
- `tests/integration/test_lever_e2e.py` — +1 e2e test, +1 route
- `tests/fixtures/lever/postings_list_rejected_member_paged.json` — new
- `tests/fixtures/lever/README.md` — fixture row
- `docs/reviews/slice-2-s2.6-corrective-review-2026-09-09.md` — this record

No change to registry, router, driver, contract tests or migrations.

## Carried forward (not fixed here; out of S2.6 scope)

- **Greenhouse `_failure` evidence refs** (same shape as F3) — S2.5
  adapter, accepted tree; recommend folding into the S2.9 sweep.
- The driver's `CONTINUE` path pre-empts `coverage_degraded` for a
  `PARTIAL` outcome that also proposes a cursor. F1 closes this for Lever
  at the adapter; a host-side belt-and-braces (`PARTIAL ⇒ coverage_degraded`
  regardless of cursor) would make the invariant adapter-independent and
  is recommended for the S2.9 driver hardening pass. Not changed here to
  keep the corrective inside S2.6's surface.
