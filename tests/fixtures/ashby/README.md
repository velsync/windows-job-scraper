# Ashby fixture corpus (S2.7, 02 §26)

Deterministic, **sanitized and fully synthetic** captures of the public
Ashby Job Posting API shape (`api.ashbyhq.com/posting-api/job-board/{org}`).
Every board name, company name, posting id, person and URL in this corpus is
invented for regression use (`acme`, `removedjob`, `changedtemplate`,
`missingfields`, `malformed`, `rejectedmember`, `apiv2`, `unlisted`,
`onlyunlisted`, `acme.example`, `jobs.ashbyhq.com/acme/...`, and UUID-shaped
ids such as `5a1b2c3d-0000-4000-8000-000000005001`). No real employer, real
posting, real candidate data and no captured authentication material is
stored here, and nothing in this corpus may be used as a live endpoint.

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

## Provider shape notes (verified against the live API and docs, 2026-09-09)

The public posting API is exactly one endpoint,
`GET /posting-api/job-board/{org}?includeCompensation=true`, returning a
**single JSON document** `{"jobs": [...], "apiVersion": "1"}` with **no
pagination** and **no declared total** — one recognized response is the whole
board membership. There is **no public per-job detail endpoint**
(`/posting-api/job-board/{org}/job/{id}` answers `401`); job content
(`descriptionHtml`/`descriptionPlain`), compensation, locations and links all
arrive inline in the board document, so this corpus has no detail fixtures
and the adapter never plans `DETAIL` work.

Each job carries `id` (UUID), `title`, `department`, `team`,
`employmentType` (`FullTime|PartTime|Intern|Contract|Temporary`), `location`
(composed text), `secondaryLocations[].location` (+ structured
`address.postalAddress` constituents), `publishedAt` (ISO DateTime, the last
*publication* time), `isListed`, `isRemote`, `workplaceType`
(`OnSite|Remote|Hybrid`), `address.postalAddress.*`, `jobUrl`,
`applyUrl` (`{jobUrl}/application`), and — with
`includeCompensation=true` — `compensation{compensationTierSummary,
scrapeableCompensationSalarySummary, compensationTiers[], summaryComponents[]}`.
`isListed: false` marks a posting that must not appear on the public board
(direct link only); it is evidence, never a board member.

## Corpus

| File | Provider shape | Regression purpose |
|---|---|---|
| `board_jobs.json` | `/posting-api/job-board/{org}` — 3 listed postings with full inline content: FullTime OnSite with compensation, PartTime Hybrid multi-location (`secondaryLocations`), Remote with an honest `jobUrl`, a hostile `applyUrl`, and script/`javascript:`/`data:` residue in `descriptionHtml` | valid document; enumerate observations with no child work; field mapping; reviewed link gate; hostile content never becomes a link candidate |

The FullTime posting's `scrapeableCompensationSalarySummary` keeps the shape
real boards emit (`"$150K - $210K"` — K-suffix on **both** numbers, observed
live, e.g. `"$130K - $224K"`).  The accepted Slice-1 `parse_salary` range
pattern only understands the K-suffix on the *second* number, so its
normalized max collapses to the min (`150000 -> 150000`).  That limitation is
recorded as deferred finding **DF-1** in the S2.7 review; the adapter passes
the provider string through verbatim — it never rewrites evidence to please
the host parser.
| `board_jobs_removed.json` | the same board without the third posting | RUN-13/§40: a posting missing from a later **COMPLETE** generation transitions to `UNCERTAIN` — closure is absence evidence, nameable to the coverage generation that proved it |
| `board_jobs_empty.json` | `{"jobs": [], "apiVersion": "1"}` | recognized empty board → `SUCCESS_EMPTY`, never a failure (ACQ-03) |
| `board_jobs_unlisted.json` | one listed posting plus one `isListed: false` | only the listed member is observed; the unlisted one is excluded with typed review evidence, and coverage stays COMPLETE for the *listed* board |
| `board_jobs_only_unlisted.json` | a single `isListed: false` posting | the listed set is genuinely empty → `SUCCESS_EMPTY` with review evidence |
| `board_jobs_changed_template.json` | renamed envelope (`postings[]`, `postingId`) | changed template → `PARSE_MARKER_MISSING`, never `SUCCESS_EMPTY` (02 §22) |
| `board_jobs_malformed.json` | truncated JSON bytes | classifier gate: `UNEXPECTED_CONTENT` never reaches a normal parser (ACQ-02) |
| `board_jobs_missing_required_fields.json` | items without `id`, with a traversal-shaped `id` and `javascript:` URL, and with a null `title` | required-field discipline: reject with structured review evidence, never invent values; a path-traversal id is never used to build a URL |
| `board_jobs_rejected_member.json` | 3 listed members: valid, **no `id`**, valid | a rejected listed member breaks the membership proof → `PARTIAL`; good observations persist; the host durably degrades the generation (`absence_inference_allowed = 0`) |
| `board_jobs_api_v2.json` | a parseable document stamped `apiVersion: "2"` | an unrecognized contract version is `PARTIAL`, not authoritative COMPLETE: content parses, coverage does not claim terminal enumeration |
| `rate_limited.429.json` | `429` rate-limit body | `RATE_LIMITED` classification; host policy, never a parser failure |
| `challenge.403.html` | `403` challenge/interstitial page | `CHALLENGE_PAGE` classification; no normal observation extraction |

Not represented, deliberately: a per-job detail capture and a per-job
`404`/`410` (the public posting API has no per-job endpoint — verified
2026-09-09), plus `auth-expired` and `login-required` captures.
