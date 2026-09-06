# Windows Job Scraper v0.3.1.3 — Windows Runtime, Packaging & Operations Specification

**Version:** Windows Job Scraper v0.3.1.3 — Final Corrected Modular Specification Set  
**Date:** 2026-09-05  
**Status:** **NORMATIVE — implementation specification**  
**Target:** Windows 11 · local-first · single-user  
**Core stack:** Python 3.12+ · FastAPI · SQLite + FTS5 · Playwright/Chromium

> This file is one member of the canonical modular specification set. Normative ownership is defined by `00_architecture_overview_and_authority.md`. If duplicated explanatory text conflicts with the normative owner, the normative owner wins.


## WIN-00. Normative ownership

This file owns launcher/process lifecycle, Windows packaging, browser distribution/runtime, dependency/build reproducibility, Doctor/diagnostics operations, backup/upgrade and optional integration promotion.

# 4. Windows launcher and process topology

## 4.1 Canonical local product topology

The production UX is:

```text
double-click JobScraper.exe
→ single-instance check
→ start/attach to local service
→ health handshake
→ open local browser dashboard
```

The launcher owns:

- service lifecycle;
- single-instance enforcement;
- safe local port allocation;
- browser-dashboard launch;
- startup diagnostics;
- clean shutdown;
- upgrade handoff;
- tray lifecycle if tray mode is enabled.

The service MUST bind to loopback only by default.

No LAN exposure is enabled by default.


Local port allocation must avoid a free-port time-of-check/time-of-use race. Prefer an atomic bind/ownership handoff in which the launcher/service binds loopback first (including an OS-assigned ephemeral port when appropriate) and only then publishes the selected port to the dashboard launcher. A probe-then-close-then-bind design is insufficient without collision-safe retry.

## 4.2 Process tree

```text
JobScraper.exe
  └── service process
      ├── FastAPI
      ├── scheduler
      ├── SQLite write coordinator
      ├── HTTP/API executor workers
      ├── adapter workers when isolation is required
      └── browser worker process
          └── Chromium process tree
```

Chromium MUST NOT execute in the FastAPI/service process.

Default target for an 8 GB system is one active browser worker. Additional browser capacity is a measured, user-configurable optimization.

## 4.3 Browser supervision

The supervisor owns:

- Chromium/Playwright lifecycle;
- context/page creation;
- process-tree cleanup;
- worker heartbeat;
- restart/backoff;
- restart-rate ceiling;
- cancellation;
- auth-context attachment;
- resource accounting;
- session separation;
- bounded debug screenshot capture where permitted.

Windows Job Objects or an equivalent Windows-native process-tree containment mechanism SHOULD be used after native acceptance testing proves the behavior reliable.

---


# 46. Unified event log and diagnostics

Event schema:

```text
events
------
id
at
run_id
source_id
binding_id
request_id
level
kind
message
data_json
```

Properties:

- append-oriented;
- retention-pruned;
- UTC/RFC 3339 timestamps;
- consumed by Runs UI;
- feeds health derivation;
- feeds Adapter Lab timeline;
- feeds diagnostics export.

Sensitive values are redacted before persistence wherever practical.

## Diagnostics bundle

A diagnostics export may include:

- recent events;
- source config without secrets;
- binding/adapter identity;
- recipe versions;
- classifier decisions;
- health state;
- migration version;
- resource measurements;
- fixture/test failure summaries.

It MUST exclude:

- cookies;
- auth headers;
- storage state;
- passwords;
- API keys;
- proxy credentials;
- unrelated browser-profile data.

The bundle includes a manifest and redaction summary.

---


# 53. Application/package module boundaries

Recommended project structure:

```text
src/jobscraper/
  launcher/
    main.py
    single_instance.py
    lifecycle.py
    doctor.py

  service/
    app.py
    lifespan.py
    scheduler.py

  web/
    routes/
    templates/
    static/
    sse.py
    security.py

  db/
    connection.py
    models.py
    repositories/
    migrations/
    backup.py
    maintenance.py

  domain/
    jobs.py
    sources.py
    profiles.py
    applications.py
    evidence.py
    failures.py

  queue/
    claims.py
    leases.py
    recovery.py
    retry.py
    cancellation.py

  acquisition/
    registry.py
    fingerprint.py
    router.py
    plans.py
    result_envelope.py
    classifier.py
    origin.py
    rate_policy.py

    executors/
      http.py
      browser_client.py

    adapters/
      greenhouse/
      lever/
      ashby/
      feeds/
      generic/

    crawler/
      canonicalize.py
      frontier.py
      cursor.py
      pagination.py
      scope.py
      sitemap.py
      revalidation.py

    recipes/
      models.py
      extractor.py
      navigation.py
      fixtures.py
      repair.py
      telemetry.py
      lab.py

  browser_worker/
    main.py
    supervisor_protocol.py
    playwright_runtime.py
    auth_state.py

  normalize/
    content.py
    company.py
    locations.py
    salary.py
    facts.py
    eligibility.py
    dedup.py
    scoring.py

  workflow/
    inbox.py
    applications.py
    reminders.py
    notifications.py

  diagnostics/
    events.py
    health.py
    export.py
    redaction.py

tests/
  unit/
  contract/
  fixtures/
  integration/
  windows/
```

This is a recommended dependency direction, not permission for circular imports.

Adapters should depend on typed acquisition/domain contracts, not on FastAPI routes or UI code.

---


# 56. Packaging and application data

Production baseline:

```text
PyInstaller --onedir
```

Suggested install/package shape:

```text
JobScraper/
  JobScraper.exe
  _internal/
  templates/
  static/
  adapter_resources/
  playwright_support/
```

Suggested application data root:

```text
%LOCALAPPDATA%\WindowsJobScraper\
```

Subdirectories may include:

```text
db\
backups\
auth\
fixtures\
snapshots\
diagnostics\
logs\
runtime\
```

Packaging starts with the first implementation slice, not at the end.

Every milestone produces a Windows-runnable build.

---


# 57. Migration, backup, upgrade, rollback, uninstall

## Migration

Before schema mutation:

```text
verify DB
→ create backup
→ verify backup
→ run ordered migration
→ integrity check + foreign_key_check + application consistency checks
→ verify required SQLite PRAGMAs/effective durability settings
→ record schema version
```

Migration failure must leave a usable prior DB or a documented automatic restore path.

Migration tests use the previous released milestone's actual schema.

## Backups

Support:

- scheduled local backups;
- rotation;
- integrity/restore checks;
- visible last-success status;
- user-initiated backup;
- backup before upgrade/migration.


SQLite/WAL backup invariant:

- never assume a raw copy of an open `.db` file is complete;
- use a SQLite-consistent mechanism such as the SQLite Backup API, `VACUUM INTO`, or a tested coordinated checkpoint/copy procedure;
- verify the produced backup with SQLite integrity/open checks;
- record schema/application version metadata with the backup.

Application backup generation additionally writes a manifest of required app-owned artifacts and hashes, coordinates with retention/pruning during capture, and records whether external user-document references are copied or reference-only. A backup is successful only when its DB and required manifest artifacts are mutually consistent.

Restore is performed only with the target service stopped/isolated. It restores into a clean/staged data root, excludes ephemeral locks/process markers, validates DB/foreign keys/application consistency plus artifact hashes/references, then atomically activates the restored generation. Missing external documents are surfaced explicitly. Authentication restore follows same-user/machine protection semantics; non-portable protected state requires normal re-login rather than pretending recovery succeeded.

## Upgrade

Upgrade flow must preserve:

- database;
- profiles;
- applications;
- recipes;
- fixtures;
- auth state;
- merge ledger;
- source bindings.

Adapter/recipe promotion is logically separate from application upgrade.

## Rollback

Application rollback must respect schema compatibility.

An older binary MUST NOT be launched against a newer incompatible migrated schema merely because its executable files were restored.

Rollback requires one of:

1. explicitly declared and tested backward schema compatibility; or
2. restoration of the verified pre-upgrade backup together with the compatible application version.

Upgrade metadata records the pre-upgrade application/schema versions and backup reference needed for recovery.

## Uninstall

Uninstall behavior must clearly distinguish:

- application binaries;
- browser runtime;
- user data.

User data is not silently deleted without explicit choice.

---


# 58. Optional integrations

Optional and feature-flagged:

- JobSpy;
- Scrapling;
- Crawlee;
- alternate browser backend experiments;
- external contact enrichment;
- local embeddings;
- LLM assistance.

Rules:

- base application works without them;
- failures are isolated;
- optional library types do not leak into core contracts;
- anti-detection/bypass functionality is not adopted;
- Windows packaging/resource acceptance is required before promotion to supported status.

---


## WIN-01. Browser resource interception

The browser worker SHOULD reduce unnecessary network/memory cost where safe.

Policy may block:

- images;
- fonts;
- media;
- known analytics/advertising resources;

only when doing so does not invalidate the source.

Rules:

- per-source/binding opt-out;
- validity classifier remains authoritative;
- a blocked-resource policy that creates an invalid/JS-shell result must be relaxed or disabled for that source;
- HTML/JSON/XHR required for job data cannot be blocked merely for performance.

## WIN-02. Browser distribution and compatibility

Production releases MUST use a known compatible Playwright/browser combination.

Supported production approaches:

1. package a tested compatible Chromium runtime; or
2. have setup/first-run install the exact pinned compatible runtime.

Whichever approach is chosen for a release must be deterministic.

`Doctor` reports:

```text
Playwright package version
expected browser revision
installed browser revision/path
launchability
basic page/context smoke result
```

Imported source configuration cannot choose an arbitrary browser executable/CDP endpoint.

## WIN-03. Dependency and build reproducibility

Each release must have:

- a fully resolved production dependency lock;
- recorded Python runtime version;
- recorded Playwright version;
- recorded browser revision;
- PyInstaller version;
- adapter resource manifest;
- schema migration version;
- source/recipe built-in resource hashes where appropriate;
- build identifier/hash;
- test/acceptance evidence tied to the build identifier.

A release should be reproducible from versioned source plus the lock/build metadata.

## WIN-03A. Timezone data and DST packaging

User schedules store IANA timezone identifiers. Windows releases bundle/pin the timezone data required by the chosen runtime (for the Python baseline, a pinned `tzdata` resource/dependency rather than assuming a system IANA database). Build/Doctor records the timezone-data version.

Scheduler policy is explicit and tested:

- nonexistent local time in the spring gap → advance to the first valid local instant after the gap unless the user edits the schedule;
- ambiguous local time in the autumn fold → use the first occurrence by default and record the resolved offset/fold;
- each scheduled/reminder occurrence has a durable occurrence ID, so restart/catch-up cannot deliver it twice;
- missed occurrences use the configured bounded catch-up policy rather than replaying an unbounded backlog.

A clean packaged Windows machine without development packages must resolve the configured timezone and reproduce these rules.

## WIN-04. Single-instance and lifecycle ownership

The launcher owns the product lifecycle.

The service MUST NOT rely on multiple independently started scheduler/service processes coordinating accidentally through SQLite.

Single-instance design must handle:

- normal second launch → focus/open existing dashboard;
- stale instance marker;
- crashed prior service;
- port conflict;
- upgrade handoff;
- clean shutdown;
- forced shutdown recovery.

## WIN-05. Browser process containment

Chromium lives outside FastAPI/service process.

Windows Job Objects or an equivalent proven Windows mechanism SHOULD contain the browser process tree after native acceptance proves reliable behavior.

Containment must not make normal browser upgrade/restart recovery impossible.

## WIN-06. Application-data ownership

Suggested root:

```text
%LOCALAPPDATA%\WindowsJobScraper\
```

Separate:

```text
db
backups
auth
fixtures
snapshots
diagnostics
logs
runtime
```

Rules:

- auth receives stricter ACL handling;
- temporary runtime artifacts are distinguishable from user data;
- uninstall does not silently delete user data;
- backup/restore excludes ephemeral locks/process markers.

## WIN-07. Upgrade compatibility

Application upgrade and adapter/recipe promotion are distinct mechanisms.

Upgrade must preserve:

- DB/user workflow;
- source/binding history;
- pinned old run identities required for resume/history;
- profiles;
- applications;
- recipes/fixtures;
- auth state;
- merge ledger;
- job-source provenance.

If an old pinned adapter implementation cannot be retained, the migration/restart behavior must be explicit rather than silently substituting a new parser.

## WIN-08. Optional dependency promotion

JobSpy, Crawlee, Scrapling, alternate browser backends, embeddings or LLM integrations remain optional.

Promotion to supported status requires:

- licensing/dependency review;
- safety-boundary review;
- Windows packaging proof;
- resource measurement;
- failure isolation;
- typed interface boundary;
- no bypass/anti-detection feature adoption.

## WIN-09. Doctor minimum checks

Doctor should report actionable status for:

- app build/version;
- data root writability;
- DB open/integrity/migration version;
- last backup and restore-check status;
- FTS5;
- browser compatibility;
- browser worker launch;
- loopback bind/security configuration;
- auth storage directories;
- free disk;
- stale runtime markers;
- schema/resource manifest consistency;
- effective SQLite foreign-key/WAL/synchronous settings;
- bundled timezone-data availability/version and a named-zone resolution smoke test;
- last backup manifest/reference validation result.

Doctor is diagnostic; it must not silently mutate user data except for explicitly safe ephemeral repair actions.
