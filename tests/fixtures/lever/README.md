# Lever fixture corpus (S2.6, 02 §26)

Deterministic, **sanitized and fully synthetic** captures of the public
Lever Postings API shapes (`api.lever.co/v0/postings/{site}`). Every site
token, company name, posting id, person and URL in this corpus is invented for
regression use (`acme`, `removedjob`, `changedtemplate`, `missingfields`,
`malformed`, `idmismatch`, `acme.example`, `jobs.lever.co/acme/...`, and
UUID-shaped ids such as `1a2b3c4d-0000-4000-8000-000000004001`). No real
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

## Provider shape notes

Lever's list endpoint returns a **bare JSON array** (no envelope, no declared
total) paginated by `skip`/`limit`; a posting carries `id`, `text` (title),
`categories{commitment,location,team,department,allLocations}`, `country`,
`createdAt` (epoch milliseconds — a *creation* time, not a stated publication
time), `workplaceType` (`unspecified|on-site|remote|hybrid`), `hostedUrl`,
`applyUrl`, and the split content members `description`/`lists`/`additional`
(plus `*Plain` variants). The list fixtures below deliberately omit the
content members so the typed `DETAIL` child path is exercised;
`postings_list_with_content.json` is the inline-content shape.

## Corpus

| File | Provider shape | Regression purpose |
|---|---|---|
| `postings_list.json` | `/v0/postings/{site}` — 3 published postings, no inline content | valid list; enumerate observations + typed `DETAIL` child tasks (ACQ-04); short page (< `limit`) is terminal |
| `postings_list_with_content.json` | same 3 postings with `description`/`lists`/`additional` inline | valid list with inline content; no child work needed |
| `postings_list_empty.json` | `[]` | recognized empty site → `SUCCESS_EMPTY`, never a failure (ACQ-03) |
| `postings_list_removed_posting.json` | one listed posting whose detail endpoint is gone | list evidence survives; detail closure is typed evidence, not a fabricated observation |
| `postings_list_changed_template.json` | renamed envelope (`data.postings`, `postingId`) | changed template → `PARSE_MARKER_MISSING`, never `SUCCESS_EMPTY` (02 §22) |
| `postings_list_missing_required_fields.json` | items without `id`, with a traversal-shaped `id` and `javascript:` URLs, with a null `text` | required-field discipline: reject with structured review evidence, never invent values; a path-traversal id is never used to build a URL |
| `postings_list_malformed.json` | truncated JSON bytes | classifier gate: `UNEXPECTED_CONTENT` never reaches a normal parser (ACQ-02) |
| `posting_detail.json` | `/v0/postings/{site}/{id}` — full posting | detail parse: title, source-native id, team/commitment/country, `createdAt` as evidence, cleaned content from `description`+`lists`+`additional`, reviewed `hostedUrl` |
| `posting_detail_multi_location.json` | `allLocations` with two cities, `workplaceType: hybrid` | multi-location set (01 §33.2), never one flattened `location_text`; hybrid is evidence, not remote |
| `posting_detail_remote_hostile_links.json` | `workplaceType: remote` posting with `javascript:`/`data:` `hostedUrl`/`applyUrl`, script residue and hostile links in the body | hostile content: active-scheme links are refused as URL evidence and neutralized by the cleaner; remote stays explicit via `job_location_type` |
| `posting_detail_id_mismatch.json` | detail body describing a *different* posting id | a response cannot mint an observation for a posting it does not contain (`INVALID_JOB_RECORD` + closure/missing evidence) |
| `posting_removed.404.json` | `404` for a removed posting | `NOT_FOUND` → typed closure evidence; no absence authority, no fabricated observation |
| `rate_limited.429.json` | `429` rate-limit body | `RATE_LIMITED` classification; host policy, never a parser failure |
| `challenge.403.html` | `403` challenge/interstitial page | `CHALLENGE_PAGE` classification; no normal observation extraction |

Not represented, deliberately: `auth-expired` and `login-required` captures.
The public Lever postings API is unauthenticated (`supported_auth_modes:
["NONE"]`), so an auth-expired shape is not a Lever contract; the
classifier-level regression for those classes lives with the HTTP/HTML path.
Offset-paging continuation (a page that fills `limit`) is exercised by the
adapter unit tests from generated payloads rather than a 100-item capture.
