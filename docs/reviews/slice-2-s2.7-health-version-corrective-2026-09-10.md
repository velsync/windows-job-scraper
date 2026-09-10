# Slice 2 S2.7 Corrective Review — Ashby health/smoke apiVersion parity — 2026-09-10

## Verdict

**S2.7-F1 corrected.** Ashby `HEALTH` and `SMOKE` probes now apply the same
reviewed `apiVersion` gate already used by `ENUMERATE`. A structurally
recognized board document stamped with an unreviewed provider contract version
is `PARTIAL` with `UNRECOGNIZED_API_VERSION` evidence; it is no longer reported
as a healthy recognized success.

This is a deliberately narrow corrective change. No S2.8/S2.9 implementation,
host policy, routing, persistence, migration, normalization, provider endpoint,
or enumeration behavior was changed.

## Authority and baseline

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3**
- Corrective baseline: `8d116eba8d911b7864b1750928831f3233c25cbc`
  (sealed S2.6 corrective baseline)
- Repair branch: `repair/s2.7-health-version-parity-2026-09-10`
- RED test commit: `eea4e6b55293c2bb00e51af044107ca2c74fc119`
- GREEN implementation commit: `67b20d1622a13bb7852688bbfa81c20d2277b70b`
- GREEN CI run: `34441995542`

## Finding corrected

Before this correction, `AshbyAdapter._parse_probe()` accepted any valid JSON
object carrying a `jobs` array as `SUCCESS_EMPTY` with
`HEALTH_PROBE_RECOGNIZED`. It did not inspect the provider `apiVersion` stamp.

That disagreed with the adapter's existing enumeration semantics. The Ashby
implementation declares `RECOGNIZED_API_VERSION = "1"`, and `_parse_board()`
already treats any other, missing, or unusable contract stamp as unreviewed:
good observations may persist, but the outcome is `PARTIAL` and absence
authority is withheld.

Consequently the same deterministic `apiVersion: "2"` document could be
reported as contract-degraded by `ENUMERATE` but healthy/recognized by
`HEALTH` and `SMOKE`. The health result could therefore conceal provider
contract drift.

## Correction

`_parse_probe()` still first requires the normal structural markers: valid JSON
object plus a `jobs` array. Structural breakage remains a typed
`PARSE_MARKER_MISSING` failure.

After structural recognition, the probe now reuses the adapter's existing
`_is_recognized_version()` and `_api_version()` helpers. If the contract stamp
is not the reviewed version, it returns:

- `ParseOutcomeKind.PARTIAL`;
- no failure record;
- no observations;
- no discovered child tasks;
- `UNRECOGNIZED_API_VERSION` review evidence containing the observed stamp;
- the existing ACQ-09 evidence references.

For reviewed `apiVersion: "1"`, existing probe behavior is unchanged:
`SUCCESS_EMPTY` plus `HEALTH_PROBE_RECOGNIZED` and exact membership counts.

No second schema or independent version vocabulary was introduced.

## TDD proof

A focused regression suite was added at
`tests/unit/test_ashby_health_version_corrective.py`, using the existing
deterministic `tests/fixtures/ashby/board_jobs_api_v2.json` fixture and the real
Ashby parser for both `HEALTH` and `SMOKE`.

### RED

At commit `eea4e6b55293c2bb00e51af044107ca2c74fc119`, before the production
change, Ubuntu/Python 3.12 produced exactly:

`2 failed, 1306 passed, 5 skipped`

The two failures were the new HEALTH and SMOKE cases. Both showed the same
root symptom: actual `SUCCESS_EMPTY` versus required `PARTIAL`.

### GREEN

At implementation commit `67b20d1622a13bb7852688bbfa81c20d2277b70b`, CI
run `34441995542` completed successfully on both platforms:

- Ubuntu / Python 3.12: `1308 passed, 5 skipped`; `pip check` clean.
- Windows / Python 3.12: `1313 passed`; `pip check` clean.
- Windows committed `pywin32` primitives import check: PASS.
- Windows pinned Chromium install: PASS.
- Windows inert browser smoke through the worker protocol: PASS.

Thus the two RED cases turned GREEN with no regression in the existing suite.

## Diff and invariant check

Relative to the sealed S2.6 baseline, the GREEN code commit changes exactly:

1. `src/jobscraper/adapters/ashby.py` — 14 additions / 1 deletion;
2. `tests/unit/test_ashby_health_version_corrective.py` — new focused tests.

The source edit is confined to `_parse_probe()` and its docstring. Enumeration,
member parsing, URL authority, host execution, persistence, routing, and
provider provisioning are untouched.

Migration authority is unchanged:

`src/jobscraper/db/migrations.py` blob SHA =
`161704673ea3ac50e93a7e68c9f4bc593b096bf4`.

## Scope boundary

This closes only the previously confirmed **S2.7-F1** health/smoke version
parity defect. It does not claim to resolve or modify any S2.8 or S2.9 finding,
and it does not alter the outstanding broader Slice-2 acceptance/native work.
