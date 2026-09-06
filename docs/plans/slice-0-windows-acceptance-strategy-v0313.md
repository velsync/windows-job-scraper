# Windows Job Scraper — Slice 0 Windows Acceptance Strategy (v0.3.1.3)

**Version:** 1.0  
**Date:** 2026-09-06  
**Status:** AUTHORITATIVE ACCEPTANCE COMPANION FOR SLICE 0  
**Repository:** `velsync/windows-job-scraper`  
**Companion plan:** `docs/plans/slice-0-worker-implementation-plan-v0313.md`

## 1. Objective

Prove Slice 0 on the correct layer without turning development into an endless manual Windows-test loop. Logic that can be proven deterministically stays in pytest/offline integration tests. Native Windows acceptance is reserved for real OS/process/packaging behavior.

The acceptance strategy follows the v0.3.1.3 rule:

> state the invariant → implement the smallest enforcing mechanism → test the failure mode → verify on the correct layer → move forward.

## 2. Test layers

### Layer A — unit tests on every package

Use for pure/path/config/security/token/state-machine/redaction/protocol logic. These tests must not touch the real `%LOCALAPPDATA%\WindowsJobScraper` root.

Examples: Host/Origin tables, HMAC descriptor tamper tests, ticket expiry with fake clock, session idle/absolute expiry, redaction, path derivation, protocol parsing, timezone policy helpers.

### Layer B — isolated integration tests

Use temporary data roots, temporary SQLite databases, ASGI test client, and subprocesses where the behavior is platform-neutral enough to automate.

Examples: migrations, effective PRAGMA checks, WAL-consistent backup/restore, event persistence, authenticated bootstrap flow, protected routes/SSE, service descriptor publication, browser-worker JSON protocol.

### Layer C — packaged Windows automation

Run the real `PyInstaller --onedir` build on Windows. Automate invocation and assertions in PowerShell/Python where possible.

Examples: `JobScraper.exe --doctor`, listener address/port, second launch, process count, browser-worker launch, Chromium revision/path, backup/restore in an isolated production-shape data root.

### Layer D — minimal human/native observation

Use only when OS/UI behavior cannot be proved cleanly by automated assertions.

Examples: dashboard actually opens in the default browser; an unavoidable UAC/Windows security prompt if one is introduced (none is expected for normal Slice 0); visual confirmation that second launch focuses/opens existing dashboard when automation cannot reliably assert focus.

No parser/crawler/product testing belongs in Slice 0 native acceptance.

## 3. Acceptance evidence directory

For the candidate build, create an evidence root outside the production data directory:

```text
artifacts/slice0/<build-id>/
  build-metadata.json
  automated-tests.txt
  pip-check.txt
  package-manifest.json
  doctor.json
  native-acceptance.json
  process-inventory-before.txt
  process-inventory-during.txt
  process-inventory-after.txt
  resource-measurements.json
  backup-restore-evidence.json
  security-negative-evidence.json
```

Do not collect cookies, install secrets, bootstrap tickets, session IDs, CSRF tokens, storage state, passwords, API keys, proxy credentials, or unrelated browser data.

## 4. Pre-native gate

Native acceptance MUST NOT begin until:

- S0.12 automated suite is fully green;
- `python -m pip check` passes;
- a clean `--onedir` package exists;
- build metadata/resource manifest identifies the exact candidate;
- no critical security test is skipped;
- a secret scan of test/log/diagnostic artifacts is clean.

A failing pre-native gate is corrected before any manual Windows action.

## 5. Native acceptance matrix

| ID | Invariant | Native action/probe | PASS oracle |
|---|---|---|---|
| W0-01 | Packaged launch works | Run `JobScraper.exe` from package | Service becomes healthy and dashboard URL is produced/opened |
| W0-02 | Loopback only | Inspect listener | Bound address is `127.0.0.1`; no LAN listener |
| W0-03 | Port allocation is collision-safe | Launch against occupied unrelated ports and inspect actual assigned port | Service obtains live OS-assigned port; no probe-close-bind dependency |
| W0-04 | Single instance | Launch executable twice | One service instance; second launcher attaches/opens existing dashboard |
| W0-05 | Stale descriptor recovery | Kill service; retain stale descriptor; relaunch | Stale descriptor rejected and new valid instance established |
| W0-06 | Old-port impersonation fails | Bind unrelated listener to prior port; use stale descriptor | Launcher refuses impersonator because instance/PID/start/HMAC/health do not match |
| W0-07 | Protected install secret | Restart same Windows user; inspect protected file properties/content and auth-directory ACL | Same secret decrypts for app; persisted bytes are not plaintext; ACL has no broad write authority; no secret in logs |
| W0-08 | Browser bootstrap/session | Normal launcher opens dashboard; call protected route without session separately | Normal flow succeeds; unauthenticated private route returns denial |
| W0-09 | Ticket not leaked via request URL/logs | Inspect sanitized server request/event evidence | No bootstrap ticket appears in path/query/log/event evidence |
| W0-10 | SQLite packaged settings | Run Doctor/read explicit diagnostic result | foreign keys ON, WAL, synchronous FULL, busy timeout present |
| W0-11 | Backup/restore | Create state, backup, stop service, restore to clean isolated root | Restored DB/artifacts verify and service opens restored generation |
| W0-12 | Timezone packaged | Run Doctor in packaged environment | pinned tzdata version present; named IANA zone resolves |
| W0-13 | Browser isolation | Run browser smoke and inspect process tree | Browser worker/Chromium are outside service process; exact runtime identified |
| W0-14 | Browser cleanup | Normal shutdown after smoke | No orphan candidate Chromium/browser-worker process remains |
| W0-15 | Browser crash recovery | Terminate worker/Chromium during controlled smoke | Service survives; bounded restart/recovery succeeds; Doctor reports correct final state |
| W0-16 | Service forced-kill recovery | Force terminate service, relaunch | stale runtime state safely recovered; no second concurrent service |
| W0-17 | Package independence | Run from clean shell without dev venv/PYTHONPATH | App/Doctor/browser smoke work using packaged resources |
| W0-18 | Resource observation | Record idle service and one smoke browser tree | Measurements captured and tied to build; no guessed correctness ceiling invented |

## 6. Security negative tests

Automate these against the packaged service wherever possible:

- Host header for attacker domain → denied.
- correct host but hostile Origin → denied.
- mutation without session → denied.
- mutation with session but missing/wrong CSRF → denied.
- private read/SSE without session → denied.
- expired/revoked session → denied.
- reused bootstrap ticket → denied.
- expired bootstrap ticket → denied.
- wrong service-instance session → denied.
- tampered runtime descriptor → denied.
- liveness endpoint → contains only declared non-sensitive fields.
- package/server response → no permissive `Access-Control-Allow-Origin: *`.
- CSP → no runtime CDN/remote script requirement.

A test that accidentally logs a credential/token is itself a FAIL even if the request is denied.

## 7. Backup/restore proof

The native acceptance backup case must use an open WAL-mode database through the application's supported backup mechanism, not a raw `.db` copy. Evidence must include:

- source build/schema version;
- backup generation/manifest ID;
- DB integrity and foreign-key result;
- required artifact hashes;
- excluded runtime markers;
- staged restore verification;
- activation into clean isolated target;
- post-restore Doctor result.

Original production/user data is never used as disposable acceptance input.

## 8. Browser proof scope

Slice 0 browser proof is deliberately narrow:

```text
spawn browser worker
→ PING/VERSION
→ launch exact pinned Chromium
→ create isolated context/page
→ load inert local content
→ return smoke result
→ close context/browser
→ worker shutdown/recovery proof
```

It does not visit public job sites, authenticate to job sources, test crawler behavior, or weaken network containment for convenience. Those belong to later slices.

## 9. Failure/retest policy

When a native test fails:

1. preserve the exact failed build/evidence;
2. identify the owning S0.x package;
3. make one bounded corrective on a new branch/worktree;
4. run the owning package's focused automated tests;
5. rerun S0.12 full automated gate;
6. rebuild with a new build ID;
7. rerun the affected native tests plus any dependency tests the reviewer names;
8. rerun full S0.13 before final Slice 0 promotion.

Do not repeatedly patch ad hoc PowerShell until something passes. The acceptance harness is treated as code: syntax-check/test it before asking the user to run it, keep it single-purpose, and prefer machine-verifiable outputs.

## 10. Final acceptance record

`native-acceptance.json` should contain:

```json
{
  "schema_version": 1,
  "slice": "0",
  "spec_version": "0.3.1.3",
  "commit": "<accepted commit>",
  "build_id": "<exact build id>",
  "automated_gate": "PASS",
  "native_gate": "PASS",
  "tests": {
    "W0-01": "PASS",
    "W0-02": "PASS",
    "W0-03": "PASS",
    "W0-04": "PASS",
    "W0-05": "PASS",
    "W0-06": "PASS",
    "W0-07": "PASS",
    "W0-08": "PASS",
    "W0-09": "PASS",
    "W0-10": "PASS",
    "W0-11": "PASS",
    "W0-12": "PASS",
    "W0-13": "PASS",
    "W0-14": "PASS",
    "W0-15": "PASS",
    "W0-16": "PASS",
    "W0-17": "PASS",
    "W0-18": "PASS"
  },
  "unresolved_critical_findings": 0,
  "promotion": "PASS"
}
```

The actual accepted artifact must replace placeholders with measured values. An acceptance record with placeholders is invalid.

## 11. Promotion rule

Slice 0 promotion requires all of the following simultaneously:

- exact source commit identified;
- exact package build identified;
- S0.12 automated gate PASS;
- W0-01 through W0-18 PASS;
- no unresolved critical security/recovery defect;
- evidence contains no secret material;
- repository clean at accepted commit;
- no Slice 1 implementation mixed into the candidate.

Until then, status is **SLICE 0 — NOT PROMOTED**.
