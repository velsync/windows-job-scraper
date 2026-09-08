# Greenhouse fixture corpus (S2.5, 02 §26)

Deterministic, **sanitized and fully synthetic** captures of the public
Greenhouse Job Board API shapes. Every board token, company name, job id,
requisition id, person and URL in this corpus is invented for regression use
(`acme`, `truncated`, `changedtemplate`, `missingfields`, `malformed`,
`removedjob`, `acme.example`, `boards.greenhouse.io/acme/...`). No real
employer, real posting, real candidate data and no captured authentication
material is stored here, and nothing in this corpus may be used as a live
endpoint.

Live sites are smoke tests, not the regression suite (02 §26): the adapter
parser, the page-validity classifier and the provenance/origin path are
regressed against these files only.

## Naming and HTTP semantics

A file name is `<scenario>[.<status>].<ext>`.

* no status segment → served as `200 OK`;
* `<status>` segment → served with exactly that HTTP status (the status is
  part of the fixture, because the page-validity classifier reads it).

Content type follows the extension: `.json` → `application/json`,
`.html` → `text/html; charset=utf-8`.

## Corpus

| File | Provider shape | Regression purpose |
|---|---|---|
| `board_list.json` | `/v1/boards/{board}/jobs` — 3 open jobs, no inline content | valid list; enumerate observations + typed `DETAIL` child tasks (ACQ-04) |
| `board_list_with_content.json` | `/v1/boards/{board}/jobs?content=true` | valid list with inline content; no child work needed |
| `board_list_empty.json` | `{"jobs": [], "meta": {"total": 0}}` | recognized empty board → `SUCCESS_EMPTY`, never a failure (ACQ-03) |
| `board_list_removed_job.json` | one listed job whose detail endpoint is gone | list evidence survives; detail closure is typed evidence, not a fabricated observation |
| `board_list_truncated.json` | `meta.total` (5) exceeds the returned jobs (2) | bounded `PARTIAL`: no authoritative coverage claim (ACQ-03) |
| `board_list_changed_template.json` | renamed envelope (`data.listings`, `listingId`) | changed template → `PARSE_MARKER_MISSING`, never `SUCCESS_EMPTY` (02 §22) |
| `board_list_missing_required_fields.json` | items without `id`, with a non-numeric `id`, with a null `title`, with a `javascript:` URL | required-field discipline: reject with structured review evidence, never invent values; a path-traversal id is never used to build a URL |
| `board_list_malformed.json` | truncated JSON bytes | classifier gate: `UNEXPECTED_CONTENT` never reaches a normal parser (ACQ-02) |
| `job_detail.json` | `/v1/boards/{board}/jobs/{id}` — full detail | detail parse: title, source-native id, offices/departments, `updated_at`, cleaned content, `absolute_url` |
| `job_detail_multi_location.json` | two admin locations + two `applicant_location_requirements` | multi-location set (01 §33.2), never one flattened `location_text` |
| `job_detail_remote_hostile_links.json` | remote-worldwide posting with `javascript:`/`data:` links and script residue | hostile content: active-scheme links are refused as URL evidence and neutralized by the cleaner; worldwide remote stays explicit |
| `job_detail_id_mismatch.json` | detail body describing a *different* job id | a response cannot mint an observation for a job it does not contain (`INVALID_JOB_RECORD` + closure/missing evidence) |
| `job_removed.404.json` | `404` for a removed job | `NOT_FOUND` → typed closure evidence; no absence authority, no fabricated observation |
| `rate_limited.429.json` | `429` rate-limit body | `RATE_LIMITED` classification; host policy, never a parser failure |
| `challenge.403.html` | `403` challenge/interstitial page | `CHALLENGE_PAGE` classification; no normal observation extraction |

Not represented, deliberately: `auth-expired` and `login-required` captures.
The public Greenhouse board API is unauthenticated (`supported_auth_modes:
["NONE"]`), so an auth-expired shape is not a Greenhouse contract; the
classifier-level regression for those classes lives with the HTTP/HTML path.
