# S3.13 Corrective Review / Status Record — 2026-09-12

Status: **PACKAGE GENERATED AND CORRECTIVELY REVIEWED — EXECUTION EVIDENCE PENDING**  
Base: `c70abd5ed4181c18eaa051f109aa80e42cdb5640` on `feat/slice3-s3.5-generic-http-crawler`  
Scope: S3.13 only. No push. W3/native promotion not started.

## Authority decision

S3.13 is the Slice-3 **acceptance/freeze** package, not another production
behavior feature. The authoritative plan requires a coherent runtime
vertical, an automated Slice-3 verifier, actual promoted-v14 -> current
migration proof with immutable v1-v14 bytes, packaged/native preparation,
and a corrective review.

`docs/plans/slice-3-windows-acceptance-strategy-v0313.md` was re-read at the
accepted base. It already defines exact candidate freeze, source-level gates,
exact-SHA Ubuntu/Windows CI, packaged-build verification, W0/W1/W2 regression,
the W3 matrix, evidence layout, rerun policy and promotion closure. It is
intentionally **unchanged**; editing it merely to create an S3.13 diff would
add provenance noise without changing authority.

## Corrective findings and dispositions

### CR-1 — Existing promoted-boundary migration proof stopped at v15
**Finding:** the existing schema suite pins v1-v14 migration bytes, but its
promoted-boundary test exercises v14 -> v15 rather than v14 -> current.

**Disposition: CORRECTED.** S3.13 appends a test that creates a real v14 DB,
seeds FK-linked promoted-domain rows, runs `migrate_database_with_backup()`
through `LATEST_SCHEMA_VERSION`, proves the complete applied-version list,
requires both production pre/post database checks to be green, verifies
integrity/FK/effective PRAGMAs, preserves seeded rows, and re-proves all
v1-v14 digests.

### CR-2 — A final vertical could accidentally bypass real owners
**Finding:** direct SQL state transitions would make an attractive but false
"end-to-end" test.

**Disposition: CORRECTED.** The new acceptance vertical drives the real
`execute_run()` path against a deterministic loopback JSON feed. It crosses
the ordinary adapter, HTTP dispatch, claim/capacity/envelope/fence, cursor,
coverage, presence, obligation, group and run owners.

### CR-3 — Restart success alone would not prove durable continuation
**Finding:** reaching `SUCCEEDED` after restart does not establish that the
page-1 result was not replayed or that continuation identity was durable.

**Disposition: CORRECTED.** The test crashes only after page 1 commits,
requires page 2 to remain durable/unclaimed, proves the persisted cursor is
`{"page": 2}`, runs startup recovery under a fresh service epoch, and then
requires the HTTP sequence to be exactly pages 1,2,3. Unique request keys,
exactly-one request attempts, final cursor state, and a second fresh-epoch
recovery replay are also checked.

### CR-4 — Final verifier must preserve prior verifier semantics
**Finding:** replacing Slice-0/1/2 verifier gates with one hand-picked pytest
list could silently weaken earlier acceptance definitions.

**Disposition: CORRECTED.** `scripts/verify_slice3.py` invokes the committed
Slice-0, Slice-1 and Slice-2 verifier scripts, then the full focused Slice-3
matrix, repository-wide tests and `pip check`.

### CR-5 — Package/build metadata must not be edited speculatively
**Finding:** S3.13 adds tests, a source-level verifier and review record only.
No product import/resource is introduced.

**Disposition: NO CHANGE REQUIRED.** PyInstaller/package-resource metadata and
runtime modules remain untouched. Packaged-build verification is still a
required external gate for the frozen candidate.

### CR-6 — Artificial RED would be misleading
**Finding:** S3.13 freezes behavior already implemented in S3.0-S3.12.
Correct code may make the newly installed acceptance tests pass immediately.

**Disposition: ACCEPTED BY DESIGN.** The package still supports a tests-only
stage, but a green first run is legitimate. Do not manufacture a runtime
defect just to force RED.

### CR-7 — Artifact-generation drafts exposed template quoting/indentation defects
**Finding:** the first in-chat generator draft produced a literal leading
backslash/indentation artifact in generated Python files. A second regeneration
then exposed an escaped-newline templating defect in the applicator itself.

**Disposition: CORRECTED BEFORE FREEZE.** Neither draft was frozen or delivered.
The final applicator was rewritten without nested generator quoting, and all
Python/fragment artifacts are parsed with Python AST/`compile()` before hashes
or the ZIP are frozen.

### CR-8 — Fixture/config insertion and recovery replay could be stricter
**Finding:** an early draft used SQL text interpolation for adapter config and
replayed recovery without opening another service epoch.

**Disposition: CORRECTED.** Final fixture inserts use bound parameters. The
replay proof opens a third service epoch before rerunning startup recovery.

### CR-9 — Package application itself needed tamper/partial-state guards
**Finding:** branch/HEAD guards alone do not prove the extracted package bytes
are the reviewed bytes. Git's default porcelain output can also collapse new
untracked directories, weakening an exact changed-path check.

**Disposition: CORRECTED.** The final applicator verifies
`PACKAGE-FILES-SHA256.txt` before touching the repo, guards the exact accepted
schema-test Git blob, supports clean -> tests-only -> apply continuation
without duplicate append, forces `--untracked-files=all`, preserves Git
porcelain's leading status columns, and verifies the exact changed-path set
after each stage. A synthetic Git-repository state-machine test is required to
pass before the package is frozen.

### CR-10 — Validation import polluted the package with bytecode cache
**Finding:** a final package listing found `__pycache__/apply_s3_13.cpython-313.pyc`,
created by importing the applicator during validation. It was not part of the
reviewed source scope and must not ship.

**Disposition: CORRECTED BEFORE DELIVERY.** The cache was removed, the final
validation executes the applicator source without bytecode-emitting imports,
and package freeze now explicitly rejects any `__pycache__`, `.pyc`, or `.pyo`
entry.

### CR-11 — The first v14→current fixture was too shallow for a final migration gate
**Finding:** the initial S3.13 migration proof genuinely started at promoted
schema v14, but seeded only source/binding/event rows. That would prove the
version chain and database health while leaving the data-transforming parts of
v16, v18, v19, v20 and v21 unexercised on pre-existing promoted data.

**Disposition: CORRECTED.** The v14 fixture now also seeds a legacy crawl
cursor, immutable RunSourcePlan plus completed coverage, a pre-v19 logical plan
outcome, a canonical job, and an ACTIVE `job_sources` row with content revision
7. The post-migration assertions prove:

- v16 preserves cursor payload while conservatively adding unknown provenance;
- v18 derives coverage group/binding-revision identity from the immutable plan;
- v19 materializes deterministic `source_plan_group_state` truth;
- v20 preserves presence while backfilling `LEGACY_ACTIVE` availability order;
- v21 seeds `jobs.evaluation_revision` from the historical max content revision
  (7), rather than resetting it to 1.

The production backup gate, pre/post database checks, effective PRAGMAs,
integrity/FK checks, exact 15→LATEST applied-version list and v1-v14 immutable
digests remain required in the same test.

### CR-12 — The coherent vertical under-proved “current availability” and local drain
**Finding:** the first vertical proved ACTIVE presence, three eligibility rows
and three score rows, but did not directly assert S3.10's durable availability
coordinates or prove that every host-native RECONCILE/ELIGIBILITY/SCORE
obligation had actually reached terminal success before run finalization.

**Disposition: CORRECTED.** The vertical now requires every source presence to
be `ACTIVE` with `ACTIVE_OBSERVATION`, non-empty effective/received timestamps
and a positive availability revision; every canonical job must be `ACTIVE` with
a positive evaluation revision; all nine host-native obligations must be
`SUCCEEDED`; and the run must have zero PENDING/RUNNING/RETRY_WAIT requests.
The cursor helper also fails if compatible run pins ever produce more than one
cursor row.

### CR-13 — Internal package hashing sampled listed files but did not reject extras
**Finding:** hashing every manifest entry detects altered reviewed files, but an
unlisted extra extracted file would not itself fail `verify_package_files()`.
The applicator also accepted a path inside the worktree as `--repo` until later
path guards happened to fail.

**Disposition: CORRECTED.** The hash manifest is now an exact allow-list: any
extra/missing file or any symlink fails before repository mutation. `--repo`
must resolve exactly to `git rev-parse --show-toplevel`, eliminating ambiguous
subdirectory application.

### CR-14 — The focused Slice-3 matrix omitted the accepted clock checkpoint file
**Finding:** the repository-wide suite would still execute
`test_n3a_clock_checkpoint.py`, but the named focused S3 matrix did not include
it even though service-clock/restart behavior is part of the queue/lease
foundation inherited by the final gate.

**Disposition: CORRECTED.** `tests/integration/test_n3a_clock_checkpoint.py` is
now an explicit focused Slice-3 verifier input in addition to the full suite.


### CR-15 — The strengthened migration fixture initially embedded JSON unsafely
**Finding:** while deepening the v14 fixture, the first revision embedded the
legacy cursor JSON literal directly inside a Python string containing SQL.
The second-pass compile gate rejected it immediately with a syntax error before
the package was frozen.

**Disposition: CORRECTED BEFORE FREEZE.** `crawl_cursors.state_json` is now a
bound SQL parameter (`{"page":4}`) rather than interpolated SQL/Python quoting.
The applicator, verifier, coherent acceptance test and migration fragment are
all re-run through Python `compile()` and AST parsing after this correction.

## Repository paths in S3.13

Added:
- `scripts/verify_slice3.py`
- `tests/integration/test_slice3_acceptance.py`
- `docs/reviews/slice-3-s3.13-corrective-review-2026-09-12.md`

Modified append-only:
- `tests/integration/test_schema_slice3.py`

Intentionally unchanged:
- all product/runtime code;
- package/PyInstaller resources;
- the already-finalized Slice-3 Windows acceptance strategy.

## Evidence still required after local application

This chat-side package generation does **not** claim runtime execution
evidence. Before the local S3.13 commit is accepted, the authoritative
Windows worktree must freshly pass:

1. exact branch/base and clean-tree preflight;
2. tests-only focused S3.13 tests;
3. full package apply;
4. `python scripts/verify_slice3.py`;
5. exact four-path diff review and `git diff --check`;
6. one local S3.13 commit, no push.

After candidate freeze, exact-SHA Ubuntu + Windows CI and packaged-build
verification remain prerequisites to W3/native execution and later promotion.

## Review conclusion

The second adversarial corrective pass found and corrected substantive gaps in
migration-fixture depth, current-availability/local-obligation proof and package
application hardening. No unresolved **known package-design/code-review
blocker** remains after those corrections. Runtime test/CI/package/native
evidence is intentionally pending until the package is applied to the
authoritative worktree.
