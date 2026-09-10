# Slice 2 S2.6 Health-Probe Corrective Review — 2026-09-10

## Verdict

**PASS — the S2.6 Lever HEALTH/SMOKE false-positive recognition defect is corrected and covered by red-then-green regression tests.**

This corrective is intentionally limited to Lever probe-recognition semantics. S2.7, S2.8 and S2.9 behavior is unchanged.

## Authority and scope

- Repository: `velsync/windows-job-scraper`
- Authoritative specification: **v0.3.1.3**
- Slice-2 base reviewed: `e04fd3ab5532361e15110235fe482b809b66bef3`
- Corrective branch: `repair/s2.6-health-probe-2026-09-10`
- RED test commit: `6176f71152387b02b722895df5316c10a89ac870`
- Production correction commit: `1175b2857fc4f4a50a92f0fb86c350d19b90d5a9`
- Cleanup/code-head commit: `533445b21afdd374042f8db6e78184bd0963f8ab`

The historical S2.6 corrective review dated 2026-09-09 is preserved unchanged. This record covers the newly identified health-probe defect only.

## Finding

`LeverAdapter._parse_probe()` previously treated any syntactically valid top-level JSON array as a recognized Lever postings response and returned `SUCCESS_EMPTY` with `HEALTH_PROBE_RECOGNIZED`.

Consequently, payloads containing no usable Lever posting members, for example an array of arbitrary objects or strings, could be reported as operationally recognized even though normal `ENUMERATE` would reject those members and could return `PARSE_MARKER_MISSING`.

The defect affected parser/binding health truthfulness. HEALTH/SMOKE did not emit observations or grant enumeration absence authority, so this was not classified as a coverage-integrity breach.

## Root cause

The probe checked only the outer container (`list`) and did not check the minimum member contract used by normal enumeration.

## RED evidence

A focused regression file was added first:

- `tests/unit/test_lever_health_probe_corrective.py`

It covers HEALTH and SMOKE for:

1. a non-empty array with no minimally valid Lever posting;
2. a mixed valid/invalid array;
3. a fully valid postings array;
4. a valid empty postings array.

Against the pre-fix production code, GitHub Actions run `34440045305` on Ubuntu/Python 3.12 produced exactly the expected failures:

- **8 failed, 1298 passed, 5 skipped**

The eight failures were the new HEALTH/SMOKE regression cases and demonstrated the permissive probe behavior rather than setup or infrastructure errors.

## Correction

`LeverAdapter._parse_probe()` remains lightweight and recognition-only. It does not invoke full enumeration parsing and does not emit observations or discovered tasks.

For non-empty arrays it now reuses the same fundamental required-member validators used by enumeration:

- `_posting_id(item["id"])`
- `_text(item["text"])`

The resulting behavior is:

| Probe document | Outcome |
|---|---|
| valid empty array | `SUCCESS_EMPTY` / recognized |
| all members minimally recognized | `SUCCESS_EMPTY` / recognized |
| mixture of recognized and rejected members | `PARTIAL` |
| non-empty array with zero recognized members | `FAILURE / PARSE_MARKER_MISSING` |
| malformed JSON or non-array | existing typed failure behavior |

Probe review evidence contains bounded counts only: `listed_postings`, `recognized_postings`, and `rejected_postings`.

Every HEALTH/SMOKE path continues to emit zero observations and zero child tasks.

## Scope discipline

No changes were made to:

- host driver behavior;
- registry or router behavior;
- destination policy;
- acquisition endpoint authority;
- migrations;
- Lever enumeration or detail semantics;
- S2.7/S2.8/S2.9 production behavior.

A connector whole-file replacement briefly changed an unrelated `LeverConfig.from_mapping` docstring. Commit `533445b21afdd374042f8db6e78184bd0963f8ab` restored the original wording before sealing the code diff.

## GREEN verification

Final code-head CI run: `34440554372` at `533445b21afdd374042f8db6e78184bd0963f8ab`.

### Ubuntu / Python 3.12

- **1306 passed, 5 skipped**
- `pip check`: **No broken requirements found**

### Windows / Python 3.12

- **1311 passed**
- `pip check`: **No broken requirements found**
- committed `pywin32` primitives: **importable**
- Playwright Chromium installation: **PASS**
- inert browser/worker smoke: **PASS** (`Chromium 143.0.7499.4 / chromium-1200 / WJS-Inert-Smoke`)

## Migration immutability

`src/jobscraper/db/migrations.py` blob SHA remains:

`161704673ea3ac50e93a7e68c9f4bc593b096bf4`

No migration was added or modified.

## Corrective files

Behavior/test delta from the Slice-2 base consists of:

- `src/jobscraper/adapters/lever.py`
- `tests/unit/test_lever_health_probe_corrective.py`

This review record is the only documentation addition for the corrective.

## Carried forward

S2.7 and later-slice findings from the broader S2.6–S2.9 corrective review remain open and intentionally untouched by this package.
