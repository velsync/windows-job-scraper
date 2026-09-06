# Windows Job Scraper — Slice 0 Worker Implementation Plan (v0.3.1.3)

> **For agentic workers:** execute exactly one S0.x package at a time. Do not redesign architecture. If a requirement is ambiguous or cannot be implemented without changing a normative contract, STOP and report the ambiguity to the architecture reviewer.

**Version:** 1.0  
**Date:** 2026-09-06  
**Status:** AUTHORITATIVE EXECUTION PLAN FOR SLICE 0; subordinate to the v0.3.1.3 normative specification set  
**Repository:** `velsync/windows-job-scraper`  
**Goal:** Deliver Slice 0 — Foundation / Safety Shell as a packaged Windows application that starts safely, stores local state durably, protects localhost/browser authority, reports actionable health, and is ready for Slice 1 without throwaway architecture.

**Architecture:** One Windows launcher owns a single local service instance. The service binds only to loopback, serves a same-origin FastAPI/Jinja/HTMX dashboard, persists through SQLite using verified PRAGMAs and migrations, and starts browser automation only through a separate browser-worker process. Browser authorization is established through a protected per-install secret, an authenticated launcher/service bootstrap channel, a short-lived single-use browser bootstrap ticket, and a service-instance-bound browser session.

**Tech stack:** Windows 11; Python 3.12+; FastAPI; Uvicorn; Jinja2; HTMX served locally; SQLite/FTS5; Playwright/Chromium; PyInstaller `--onedir`; pytest. Exact resolved dependency versions are frozen by S0.0 and become release metadata.

**Normative specs:** `docs/spec/v0.3.1.3/` — especially modules `03`, `04`, `05`, `06`, and `07`.

---

## 1. Authority and worker operating contract

### 1.1 Authority order

1. v0.3.1.3 module owner identified by `00_architecture_overview_and_authority.md`.
2. This Slice 0 plan.
3. The current S0.x worker prompt/package.
4. Existing implementation and tests.

A lower layer never weakens a higher layer.

### 1.2 Roles

**Architecture reviewer / gatekeeper (ChatGPT):** owns task decomposition, architecture interpretation, security review, acceptance criteria, diff review, corrective instructions, and package promotion.

**Implementation workers (GLM 5.3, Muse Spark, or equivalent):** implement only the assigned package, run the named tests/build commands, produce evidence, and stop at the package boundary. They do not expand scope, reinterpret security requirements, or begin the next package without approval.

### 1.3 Mandatory worker rules

Every worker package MUST:

- begin from a clean branch/worktree at the reviewer-approved base commit;
- read `AGENTS.md`, this plan, and the named normative sections before editing;
- change only the package's allowed files plus mechanically required lock/metadata files explicitly named by the package;
- use test-first development for behavior-bearing code;
- run focused tests before the broader package gate;
- record exact files changed and exact commands executed;
- make no source-adapter/crawler/product Slice 1 implementation;
- make no LAN listener, permissive CORS, cloud dependency, telemetry, auto-application, anti-bot bypass, CAPTCHA solving, proxy harvesting, arbitrary remote code loading, or browser-in-service shortcut;
- stop on an architecture ambiguity rather than inventing a workaround;
- stop on an unexpected failing pre-existing test and report it;
- never claim PASS without command output/evidence.

### 1.4 Required worker completion report

Each package report must contain:

```text
PACKAGE: S0.x
BASE_COMMIT: <sha>
END_COMMIT: <sha>
FILES_CHANGED:
- <path>
COMMANDS_RUN:
- <command> -> PASS|FAIL, exit <n>
TESTS: <passed>/<total>, <failed> failed
PACKAGING: NOT_RUN|PASS|FAIL
WINDOWS_NATIVE: NOT_REQUIRED|PASS|FAIL
KNOWN_LIMITATIONS:
- <none or exact item>
ARCHITECTURE_AMBIGUITIES:
- <none or exact item>
STOP_CONDITION_TRIGGERED: NO|YES - <reason>
```

The reviewer compares this report to the actual Git diff; the report is not trusted as proof by itself.

---

## 2. Slice 0 fixed implementation choices

These are Slice 0 implementation decisions made to turn v0.3.1.3 into an executable plan. They do not alter the architecture.

1. **One packaged executable:** `JobScraper.exe` is the launcher entry point and may spawn the same packaged program in internal `--service` and `--browser-worker` modes. Internal modes are not user-facing product features.
2. **Collision-safe port:** the service binds `127.0.0.1` to port `0` and publishes the OS-assigned bound port only after the listener exists. There is no probe-close-bind sequence.
3. **Single instance:** Windows named mutex via a focused stdlib/`ctypes` wrapper. The runtime descriptor is not trusted merely because the mutex exists.
4. **Windows native primitives:** use the pinned `pywin32` runtime dependency for DPAPI, named-mutex, and ACL operations instead of custom security-sensitive `ctypes` reimplementations.
5. **Per-install secret:** 32 random bytes protected with Windows user-scoped DPAPI and stored under an explicitly ACL-hardened app-owned `auth` directory. No plaintext secret is persisted.
6. **Launcher/service bootstrap channel:** a launcher-only loopback endpoint requires a nonce, bounded timestamp, service-instance ID, and HMAC proof using the protected install secret. This channel returns the one-time browser bootstrap ticket. The secret itself is never sent.
7. **Bootstrap ticket:** 256-bit random value; single-use; expires after 60 seconds; only its cryptographic hash is retained by the service while pending.
8. **Browser session:** 256-bit opaque session ID; server-side in-memory registry; bound to the current service-instance epoch; 30-minute idle lifetime and 12-hour absolute lifetime. Service restart or install-secret rotation invalidates all sessions.
9. **CSRF:** separate random CSRF token is bound to the server-side session, mirrored in a host-only `SameSite=Strict` non-HttpOnly cookie named `wjs_csrf`, and required verbatim as `X-CSRF-Token` on mutations. The authentication cookie is `wjs_session`, host-only, `HttpOnly`, `SameSite=Strict`. No permissive CORS.
10. **Unauthenticated HTTP surface:** only minimal liveness plus the non-private bootstrap shell/exchange endpoints required to establish a session. No user data, events, diagnostics, SSE, export, or mutation is public.
11. **SQLite baseline:** `foreign_keys=ON`, WAL, `synchronous=FULL`, explicit `busy_timeout`, verified on every relevant connection; forward-only migrations.
12. **Browser smoke:** Slice 0 browser-worker acceptance uses only local inert content (`about:blank`/`data:` test content). It does not navigate to job sources.
13. **HTMX:** served from a committed/local packaged static asset, never a runtime CDN dependency.
14. **Timezone:** use IANA identifiers through pinned/bundled `tzdata`; Doctor proves at least one named DST-capable zone resolves in the packaged build.

If implementation evidence proves one of these choices is technically blocked on the target Windows environment, STOP and return the blocker to the reviewer. Do not silently substitute a weaker model.

---

## 3. Repository and Slice 0 file map

The following is the target Slice 0 structure. Later slices may add directories already anticipated by module 05, but Slice 0 must not create unused product/crawler code merely to mirror the full future tree.

```text
AGENTS.md
README.md
.gitignore
pyproject.toml
requirements/
  production.in
  production.lock.txt
  dev.in
  dev.lock.txt
build/
  jobscraper.spec
  build_metadata.py
src/jobscraper/
  __init__.py
  __main__.py
  version.py
  paths.py
  config.py
  launcher/
    __init__.py
    main.py
    single_instance.py
    lifecycle.py
    runtime_descriptor.py
    doctor.py
  service/
    __init__.py
    app.py
    lifespan.py
    runner.py
  web/
    __init__.py
    security.py
    sessions.py
    bootstrap.py
    routes.py
    sse.py
    templates/
      bootstrap.html
      dashboard.html
      error.html
    static/
      app.js
      htmx.min.js
  security/
    __init__.py
    dpapi.py
    windows_acl.py
    install_secret.py
    redaction.py
  db/
    __init__.py
    connection.py
    migrations.py
    schema.py
    backup.py
    restore.py
  diagnostics/
    __init__.py
    events.py
    health.py
  browser_worker/
    __init__.py
    main.py
    protocol.py
    playwright_runtime.py
  timeutil/
    __init__.py
    zones.py
tests/
  unit/
  integration/
  contract/
  windows/
  fixtures/
docs/spec/v0.3.1.3/
docs/plans/
artifacts/
  .gitkeep
```

No future Slice 1 domain models, adapters, crawler, queue leases, applications, or scoring code is created in Slice 0.

---

## 4. Package dependency graph

```text
S0.0 Repository/build skeleton
  ↓
S0.1 Data-root + configuration foundation
  ↓
S0.2 SQLite + schema/migration foundation
  ↓
S0.3 Backup-before-migration + restore foundation
  ↓
S0.4 Event log + redaction
  ↓
S0.5 Localhost security primitives
  ↓
S0.6 FastAPI/Jinja/HTMX shell under security boundary
  ↓
S0.7 Install secret + bootstrap/session authentication
  ↓
S0.8 Launcher + single instance + runtime descriptor + lifecycle
  ↓
S0.9 Browser worker + Playwright/Chromium compatibility
  ↓
S0.10 Timezone packaging + Doctor aggregation
  ↓
S0.11 PyInstaller --onedir packaged build
  ↓
S0.12 Full automated Slice 0 acceptance
  ↓
S0.13 Native Windows acceptance + promotion gate
```

No package may be skipped because a later package appears to cover the same behavior.

---

# S0.0 — Repository authority, package skeleton, dependency lock, test/build skeleton

**Purpose:** Make the repository deterministic enough that every later worker starts from the same layout and dependency/build contract.

**Normative basis:** module 05 `WIN-03`, module 07 `ROAD-01`, `ROAD-11`.

**Files:**

- Create `pyproject.toml`.
- Create `requirements/production.in`, `requirements/dev.in` and fully resolved lock files.
- Create package/test directory skeleton listed in section 3, but only `__init__.py`, `__main__.py`, `version.py` contain code.
- Create `build/build_metadata.py` and `artifacts/.gitkeep`.
- Do not add application behavior yet.

**Required interfaces:**

```python
# src/jobscraper/version.py
APP_NAME: str = "Windows Job Scraper"
APP_VERSION: str
SCHEMA_VERSION: int = 0

# build/build_metadata.py
def collect_build_metadata() -> dict[str, str]: ...
```

`collect_build_metadata()` must report at least Python, FastAPI, Playwright, PyInstaller, tzdata, and application version values from the installed environment; browser revision may be `NOT_INSTALLED` until S0.9.

**Test-first steps:**

- [ ] Add `tests/unit/test_version.py` proving `APP_NAME`, parseable semantic `APP_VERSION`, and `SCHEMA_VERSION == 0`.
- [ ] Run `python -m pytest tests/unit/test_version.py -q`; expected initial FAIL because package is absent.
- [ ] Add minimal package/version implementation.
- [ ] Add `tests/unit/test_build_metadata.py` proving the required metadata keys exist and no secret/environment value is dumped wholesale.
- [ ] Implement metadata collection using explicit package/version lookups only.
- [ ] Resolve and commit exact production/dev dependency locks. Production must include FastAPI, Uvicorn, Jinja2, Playwright, tzdata, and pywin32; build tooling includes PyInstaller; dev includes pytest and test HTTP tooling. No runtime CDN requirement is allowed.
- [ ] Run `python -m pytest tests/unit/test_version.py tests/unit/test_build_metadata.py -q`.
- [ ] Run `python -m pip check`.

**Gate S0.0:** PASS only when a clean environment can install from committed locks and import `jobscraper` with focused tests green.

**STOP:** dependency resolver conflict, Python <3.12, or need for an unapproved major framework substitution.

---

# S0.1 — Application data root and deterministic configuration

**Purpose:** Establish safe, testable path ownership before DB/auth/runtime files exist.

**Normative basis:** module 05 `WIN-06` and package/data sections.

**Files:** `src/jobscraper/paths.py`, `src/jobscraper/config.py`, `tests/unit/test_paths.py`, `tests/unit/test_config.py`.

**Required interfaces:**

```python
@dataclass(frozen=True)
class AppPaths:
    root: Path
    db: Path
    backups: Path
    auth: Path
    fixtures: Path
    snapshots: Path
    diagnostics: Path
    logs: Path
    runtime: Path


def default_data_root() -> Path: ...
def build_app_paths(root: Path) -> AppPaths: ...
def ensure_app_directories(paths: AppPaths) -> None: ...

@dataclass(frozen=True)
class AppConfig:
    data_root: Path
    loopback_host: str = "127.0.0.1"
    busy_timeout_ms: int = 5000
```

**Behavior:** default root is `%LOCALAPPDATA%\WindowsJobScraper`; tests may inject a temporary root. Runtime artifacts are structurally distinguishable from durable user data. No directory under the source/package tree is used for mutable production data.

**Test-first steps:**

- [ ] Prove default root resolves under `LOCALAPPDATA` and is not the repository/install directory.
- [ ] Prove all eight required subdirectories are derived deterministically.
- [ ] Prove `ensure_app_directories` is idempotent.
- [ ] Prove an injected temp root prevents tests from touching real user data.
- [ ] Implement minimum path/config code.
- [ ] Run `python -m pytest tests/unit/test_paths.py tests/unit/test_config.py -q`.

**Gate S0.1:** PASS with no writes outside injected test root during tests.

**STOP:** any implementation writes production data beside the executable/source tree.

---

# S0.2 — SQLite connection, PRAGMA verification, baseline schema and migrations

**Purpose:** Establish the durability contract before feature tables proliferate.

**Normative basis:** module 03 section 50; module 05 migration requirements; module 06 storage acceptance.

**Files:** `src/jobscraper/db/connection.py`, `migrations.py`, `schema.py`, `tests/unit/test_db_connection.py`, `tests/integration/test_migrations.py`.

**Required interfaces:**

```python
@dataclass(frozen=True)
class SqliteSettings:
    foreign_keys: int
    journal_mode: str
    synchronous: int
    busy_timeout_ms: int


def connect_db(path: Path, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection: ...
def read_sqlite_settings(conn: sqlite3.Connection) -> SqliteSettings: ...
def verify_sqlite_settings(conn: sqlite3.Connection) -> None: ...
def current_schema_version(conn: sqlite3.Connection) -> int: ...
def migrate_schema(conn: sqlite3.Connection, target_version: int) -> None: ...
def run_database_checks(conn: sqlite3.Connection) -> dict[str, object]: ...
```

**Baseline schema:** metadata/schema-version table plus `events` table may be created here only if S0.4 owns its final columns; preferably version 1 creates metadata only and S0.4 adds events in a later migration. No future product tables.

**Test-first steps:**

- [ ] Prove every connection reports `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`, configured busy timeout.
- [ ] Prove `foreign_key_check` and `integrity_check` are invoked by `run_database_checks` and failures are returned as failures.
- [ ] Prove schema migration is ordered, idempotent at target version, and rejects unsupported downgrade.
- [ ] Prove interrupted/failed migration transaction leaves prior schema usable where SQLite transaction semantics permit.
- [ ] Implement minimal connection/migration code using stdlib `sqlite3`.
- [ ] Run focused DB tests twice against fresh temporary databases.

**Gate S0.2:** PASS only if effective PRAGMAs are observed, not merely configured.

**STOP:** migration requires raw copying of an open WAL database or a schema downgrade path.

---

# S0.3 — Consistent backup-before-migration and stopped/isolated restore foundation

**Purpose:** Make migration safe before any later slice depends on accumulated local state.

**Normative basis:** module 05 section 57; v0.3.1.3 application backup-generation manifest correction.

**Files:** `src/jobscraper/db/backup.py`, `restore.py`, `tests/integration/test_backup_restore.py`, `tests/fixtures/backup/`.

**Required interfaces:**

```python
@dataclass(frozen=True)
class BackupArtifact:
    relative_path: str
    sha256: str
    required: bool
    kind: str

@dataclass(frozen=True)
class BackupManifest:
    manifest_version: int
    app_version: str
    schema_version: int
    created_at_utc: str
    artifacts: tuple[BackupArtifact, ...]
    external_references: tuple[str, ...]


def create_backup_generation(paths: AppPaths, conn: sqlite3.Connection) -> Path: ...
def verify_backup_generation(backup_dir: Path) -> BackupManifest: ...
def stage_restore(backup_dir: Path, target_root: Path) -> Path: ...
def activate_staged_restore(staged_root: Path, target_root: Path) -> None: ...
```

**Rules:** use SQLite Backup API or another spec-approved SQLite-consistent mechanism; capture required app-owned artifacts under a generation directory; exclude runtime locks/markers; hash every required captured artifact; restore only while target service is stopped/isolated; validate before activation; DPAPI-protected auth material may be preserved as protected bytes but must not be decrypted into the backup.

**Test-first steps:**

- [ ] Prove backup of an open WAL database contains a committed row and passes integrity/foreign-key checks after restore.
- [ ] Prove manifest hash mismatch rejects restore.
- [ ] Prove missing required artifact rejects restore.
- [ ] Prove `runtime/` markers are not treated as restorable durable state.
- [ ] Prove staged restore does not modify live target until verification succeeds.
- [ ] Implement minimum backup/restore flow.
- [ ] Run integration tests on temporary roots.

**Gate S0.3:** PASS only when a clean temporary data root can be restored and reopened with required checks green.

**STOP:** any backup method depends on copying only the live `.db` file while WAL is open.

---

# S0.4 — Unified event log and redaction foundation

**Purpose:** Create one safe append-oriented diagnostic/event surface before service/launcher/browser events exist.

**Normative basis:** module 05 section 46; module 04 secret rules.

**Files:** `src/jobscraper/diagnostics/events.py`, `src/jobscraper/security/redaction.py`, DB migration for `events`, `tests/unit/test_redaction.py`, `tests/integration/test_events.py`.

**Required interfaces:**

```python
@dataclass(frozen=True)
class EventRecord:
    at: str
    level: str
    kind: str
    message: str
    data: dict[str, object]
    run_id: str | None = None
    source_id: str | None = None
    binding_id: str | None = None
    request_id: str | None = None


def redact_event_data(value: object) -> object: ...
def append_event(conn: sqlite3.Connection, event: EventRecord) -> int: ...
def list_recent_events(conn: sqlite3.Connection, *, limit: int = 100) -> list[EventRecord]: ...
```

**Tests must prove:** known secret keys/headers/cookie-like values are redacted before persistence; timestamps are UTC RFC3339; insertion is append-oriented; limit is bounded; redaction does not silently serialize arbitrary process environment.

**Gate S0.4:** focused unit/integration tests green and raw test secret absent from the SQLite file bytes where practical to assert.

**STOP:** logging helper requires caller discipline to avoid persistence of raw secret dictionaries; central redaction must remain unavoidable for this event path.

---

# S0.5 — Localhost security primitives and route classification

**Purpose:** Build security enforcement before the dashboard is accepted as a runnable surface.

**Normative basis:** module 04 section 5 and `SEC-01`; module 06 localhost/session tests.

**Files:** `src/jobscraper/web/security.py`, `tests/unit/test_web_security.py`, `tests/contract/test_route_security_contract.py`.

**Required interfaces:**

```python
@dataclass(frozen=True)
class RoutePolicy:
    public: bool
    mutation: bool
    csrf_required: bool


def validate_host(host_header: str, bound_port: int) -> bool: ...
def validate_origin(origin: str | None, *, expected_origin: str, allow_missing: bool) -> bool: ...
def security_headers() -> dict[str, str]: ...
def require_content_type(content_type: str | None, allowed: set[str]) -> None: ...
```

**Policy:** loopback-only expected origin is `http://127.0.0.1:<port>`; no `*` CORS; hostile/mismatched Host fails; browser-originated protected requests require exact Origin; launcher-only authenticated endpoint may have no Origin but must satisfy launcher proof in S0.7; strict CSP must forbid arbitrary remote script execution; request sizes are bounded.

**Test-first steps:**

- [ ] Table-test valid/invalid Host values including `localhost`, alternate port, embedded userinfo, malformed host, and attacker domain.
- [ ] Table-test exact Origin, hostile Origin, missing Origin in browser-protected context.
- [ ] Prove no permissive CORS header is emitted.
- [ ] Prove CSP does not authorize remote scripts.
- [ ] Prove mutation route policy requires session + CSRF once session hooks exist.
- [ ] Implement primitives independent of FastAPI first; add thin middleware hooks only after unit tests pass.

**Gate S0.5:** all security contract tests green.

**STOP:** proposed workaround relies on CORS as the main localhost security boundary or accepts arbitrary `localhost`/port aliases.

---

# S0.6 — FastAPI/Jinja/HTMX shell under the security boundary

**Purpose:** Create the local UI/service shell without exposing user/private state unauthenticated.

**Normative basis:** module 05 topology/module boundaries; module 01 private-route requirement; module 04 localhost policy.

**Files:** `src/jobscraper/service/app.py`, `lifespan.py`, `web/routes.py`, `web/sse.py`, templates/static files, `tests/integration/test_service_shell.py`.

**Required routes at end of package:**

```text
GET /health/live       public; minimal {status, service_instance_id}
GET /                  public bootstrap shell only; no private data
GET /app               protected hook (returns 401 until a valid session is supplied by S0.7)
GET /api/events        protected hook
GET /api/events/stream protected hook/SSE
```

No product job/profile data routes exist.

**Test-first steps:**

- [ ] Prove `/health/live` contains no filesystem path, token, environment, DB content, or event content.
- [ ] Prove `/` contains only local assets and bootstrap logic; no CDN URL.
- [ ] Prove `/app`, event list and SSE fail without auth hook.
- [ ] Prove Host validation applies before route handling.
- [ ] Prove security headers/CSP on HTML responses.
- [ ] Implement minimal Jinja dashboard showing version/health placeholders only.
- [ ] Run integration tests with ASGI test client and a temp data root.

**Gate S0.6:** shell works in test client but is not yet promoted to packaged/native use until S0.7/S0.8.

**STOP:** any private route is made temporarily public to simplify development.

---

# S0.7 — DPAPI install secret, launcher proof, bootstrap ticket, browser session and CSRF

**Purpose:** Complete the concrete launcher→service→dashboard authentication contract introduced by v0.3.1.3.

**Normative basis:** module 04 `SEC-01`; module 06 localhost/session acceptance.

**Files:** `security/dpapi.py`, `security/windows_acl.py`, `security/install_secret.py`, `web/sessions.py`, `web/bootstrap.py`, `web/security.py`, tests under `unit/`, `contract/`, `integration/`.

**Required interfaces:**

```python
# security/dpapi.py
def protect_for_current_user(plaintext: bytes, *, entropy: bytes | None = None) -> bytes: ...
def unprotect_for_current_user(ciphertext: bytes, *, entropy: bytes | None = None) -> bytes: ...

# security/windows_acl.py
def harden_auth_directory(path: Path) -> None: ...
def inspect_auth_directory_acl(path: Path) -> dict[str, object]: ...

# security/install_secret.py
def load_or_create_install_secret(paths: AppPaths) -> bytes: ...
def rotate_install_secret(paths: AppPaths) -> bytes: ...

# web/bootstrap.py
def make_launcher_proof(secret: bytes, instance_id: str, nonce: str, issued_at: int) -> str: ...
def verify_launcher_proof(...) -> bool: ...
def issue_bootstrap_ticket(...) -> str: ...
def consume_bootstrap_ticket(ticket: str, ...) -> bool: ...

# web/sessions.py
@dataclass
class BrowserSession: ...
class SessionRegistry:
    def create(self, instance_id: str) -> tuple[str, str]: ...  # session_id, csrf_token
    def validate(self, session_id: str, instance_id: str) -> BrowserSession | None: ...
    def revoke_all(self) -> None: ...
```

**HTTP contract:**

```text
POST /__launcher/bootstrap-ticket
  launcher proof only; Host strict; no browser session required
  -> one-time ticket

POST /api/bootstrap
  one-time ticket + strict Host/Origin
  -> `wjs_session` HttpOnly SameSite=Strict cookie + session-bound `wjs_csrf` SameSite=Strict cookie/token

POST /api/session/logout
  session + CSRF
  -> revokes current session
```

**Test-first steps:**

- [ ] On Windows, DPAPI round-trip succeeds for current user; plaintext is not equal to persisted bytes.
- [ ] Auth directory ACL is explicitly hardened and inspection proves no broad `Everyone`/ordinary `Users` write authority; current user retains required access.
- [ ] Install secret remains stable across reload and rotates explicitly.
- [ ] Secret/ticket/session values never appear in event records or ordinary request logs used by tests.
- [ ] Launcher HMAC proof rejects modified instance, nonce, timestamp and proof; enforces bounded skew and nonce replay rejection.
- [ ] Bootstrap ticket is single-use and expires after 60 seconds using an injectable clock.
- [ ] Session expires on 30-minute idle and 12-hour absolute lifetime; use injectable monotonic/wall time in tests rather than sleeping.
- [ ] Session bound to wrong/new service-instance ID fails.
- [ ] CSRF cookie/token is session-bound; wrong-session token and header/cookie mismatch fail.
- [ ] `/app`, events, SSE require session; mutation requires session + CSRF.
- [ ] Hostile Origin and absent CSRF fail.
- [ ] Bootstrap fragment is processed client-side; no ticket is emitted in ordinary server request path/query logging.

**Gate S0.7:** complete authenticated test-client flow passes; secret scan across logs/events/test diagnostics finds no test secret.

**STOP:** requirement to place install secret or bootstrap ticket in URL query/path, JavaScript bundle, localStorage, or ordinary logs.

---

# S0.8 — Windows launcher, single-instance ownership, service runtime descriptor and lifecycle

**Purpose:** Turn the service into the canonical double-click local product topology.

**Normative basis:** module 05 section 4, `WIN-04`, module 04 launcher bootstrap.

**Files:** `launcher/main.py`, `single_instance.py`, `lifecycle.py`, `runtime_descriptor.py`, `service/runner.py`, `__main__.py`, tests under `unit/`, `integration/`, `windows/`.

**Required contracts:**

```python
@dataclass(frozen=True)
class RuntimeDescriptor:
    schema_version: int
    service_instance_id: str
    pid: int
    process_start_identity: str
    host: str
    port: int
    service_epoch: str
    created_at_utc: str
    authenticator: str


def acquire_single_instance() -> object: ...
def sign_runtime_descriptor(desc_without_auth: dict, secret: bytes) -> str: ...
def verify_runtime_descriptor(desc: RuntimeDescriptor, secret: bytes) -> bool: ...
def start_service_process(...) -> subprocess.Popen: ...
def wait_for_valid_service(...) -> RuntimeDescriptor: ...
def request_bootstrap_ticket(...) -> str: ...
def open_dashboard(port: int, ticket: str) -> None: ...
```

**Lifecycle sequence:**

```text
launcher acquires named mutex
→ if first: spawn service
→ service binds 127.0.0.1:0
→ service publishes signed descriptor after listener is live
→ launcher validates descriptor + PID/start identity + health instance ID
→ launcher requests one-time bootstrap ticket using launcher proof
→ launcher opens http://127.0.0.1:<port>/#bootstrap=<ticket>
```

Second launch must validate and attach to the existing service, not start another. Stale descriptor, dead PID, PID reuse/start-identity mismatch, wrong HMAC, and old-port impersonation all fail validation and enter bounded recovery.

**Test-first steps:**

- [ ] Unit-test descriptor canonical serialization/sign/verify and tamper rejection.
- [ ] Integration-test service binds before descriptor publication and uses OS-assigned port.
- [ ] Integration-test stale descriptor recovery with a dead PID fixture.
- [ ] Native Windows test named mutex: first acquisition succeeds; second reports existing instance.
- [ ] Native Windows test second launcher opens existing dashboard and does not spawn a second service.
- [ ] Native Windows test old descriptor pointed at another listener cannot pass instance validation.
- [ ] Native Windows test clean shutdown removes/invalidates descriptor; forced kill leaves recoverable stale state.

**Gate S0.8:** process/lifecycle native tests pass on Windows before package promotion.

**STOP:** use of probe-free-port/close/rebind without collision-safe ownership, or trust in PID/port alone.

---

# S0.9 — Browser-worker process skeleton and Playwright/Chromium compatibility

**Purpose:** Prove browser isolation/process supervision mechanics without implementing source browsing.

**Normative basis:** module 05 sections 4.2/4.3, `WIN-02`, `WIN-05`; module 07 Slice 0 browser skeleton.

**Files:** `browser_worker/main.py`, `protocol.py`, `playwright_runtime.py`, `launcher/lifecycle.py` or service lifespan integration, tests under `unit/`, `integration/`, `windows/`.

**Protocol v1 messages:**

```json
{"type":"PING","request_id":"..."}
{"type":"VERSION","request_id":"..."}
{"type":"SMOKE","request_id":"..."}
{"type":"SHUTDOWN","request_id":"..."}
```

Responses include `protocol_version`, matching `request_id`, `ok`, and typed payload/error. `SMOKE` launches the pinned Chromium, creates an isolated context/page, loads inert local content only, returns title/version facts, closes page/context/browser, and proves cleanup.

**Test-first steps:**

- [ ] Unit-test protocol parse rejects unknown/malformed/oversized messages.
- [ ] Prove browser-worker module imports no FastAPI route objects and service process does not call Playwright directly.
- [ ] Integration-test child process PING/VERSION/SHUTDOWN.
- [ ] Windows test Playwright browser revision/path is the pinned expected runtime.
- [ ] Windows `SMOKE` proves Chromium launches outside service PID and exits cleanly.
- [ ] Kill browser worker/Chromium and prove bounded restart/cleanup without service crash; no source network navigation.

**Gate S0.9:** compatibility facts are programmatically available to Doctor; no orphan Chromium remains after clean smoke/shutdown.

**STOP:** Playwright launched inside FastAPI/service process, arbitrary browser executable/CDP endpoint, or any job-source crawling behavior.

---

# S0.10 — Timezone data and Doctor aggregation

**Purpose:** Make packaged health actionable and prove Windows does not depend on an ambient IANA timezone database.

**Normative basis:** module 05 `WIN-03A`, `WIN-09`; module 06 Windows acceptance.

**Files:** `timeutil/zones.py`, `launcher/doctor.py`, `diagnostics/health.py`, tests under `unit/`, `integration/`, `windows/`.

**Required interfaces:**

```python
@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    summary: str
    details: dict[str, object]


def resolve_named_zone(name: str) -> ZoneInfo: ...
def run_doctor(config: AppConfig) -> list[CheckResult]: ...
def doctor_exit_code(results: list[CheckResult]) -> int: ...
```

**Doctor minimum Slice 0 checks:** app build/version; data-root writability; DB open/integrity/foreign keys/migration; effective SQLite PRAGMAs; latest backup/restore-check status; FTS5 capability; loopback/security configuration; auth storage/secret decryptability without revealing secret; stale runtime markers; browser worker launch; Playwright/browser revision/launch smoke; free disk; timezone-data version and named-zone resolution; schema/resource manifest consistency.

**Test-first steps:**

- [ ] Prove missing/broken DB/browser/tzdata produces actionable FAIL without secret leakage.
- [ ] Prove named IANA zone resolves with packaged `tzdata` dependency and records tzdata version.
- [ ] Unit-test deterministic DST policy helpers for one spring gap and autumn fold even though scheduling itself is later; this proves the packaged data can support the declared policy.
- [ ] Prove Doctor is diagnostic and does not migrate, restore, rotate secrets, or delete stale markers silently.
- [ ] Run Doctor against an isolated healthy temp root and assert PASS set.

**Gate S0.10:** all mandatory checks produce stable machine-readable and human-readable results.

**STOP:** Doctor repairs durable data without explicit action or assumes Windows system IANA data exists.

---

# S0.11 — PyInstaller `--onedir` production package

**Purpose:** Produce the first real Windows-runnable Slice 0 candidate.

**Normative basis:** module 05 packaging/build reproducibility; module 07 every-slice runnable-build rule.

**Files:** `build/jobscraper.spec`, build metadata, packaging tests/scripts under `tests/windows/` or `scripts/` if a script is necessary.

**Package must contain:** `JobScraper.exe`, Python/internal runtime, local Jinja templates/static/HTMX, migration resources, pinned tzdata, Playwright support and the exact pinned Chromium runtime strategy selected in S0.0/S0.9. Mutable user data must remain under `%LOCALAPPDATA%\WindowsJobScraper`, not inside the package.

**Build steps:**

- [ ] Start from a clean environment installed solely from committed locks.
- [ ] Run complete automated suite before packaging.
- [ ] Build with committed PyInstaller spec in `--onedir` mode.
- [ ] Record build ID/hash plus Python/FastAPI/Playwright/browser/PyInstaller/tzdata/schema versions.
- [ ] Launch packaged `JobScraper.exe --doctor` against isolated test data root.
- [ ] Launch packaged app, complete authenticated bootstrap, then shut down.
- [ ] Verify no runtime CDN request is required for dashboard shell.
- [ ] Verify packaged resource manifest against actual files.

**Gate S0.11:** packaged candidate launches on target Windows account and Doctor has no unexplained mandatory FAIL.

**STOP:** development Python/PYTHONPATH or globally installed browser is required for the packaged candidate to function.

---

# S0.12 — Full automated Slice 0 acceptance gate

**Purpose:** Establish one deterministic pre-native gate before asking the user to perform Windows-specific checks.

**Normative basis:** module 06 plus all Slice 0 requirements from modules 03–05.

**Files:** `tests/contract/test_slice0_contract.py`, `tests/integration/test_slice0_acceptance.py`, `scripts/verify_slice0.py` only if needed to aggregate existing tests without duplicating logic.

**Automated acceptance must prove at minimum:**

1. application/data paths isolate mutable state;
2. exact dependency/build metadata is present;
3. SQLite PRAGMAs observed and verified;
4. integrity + foreign key + application checks work;
5. forward-only migration behavior;
6. WAL-consistent backup and staged restore;
7. backup manifest hash/reference rejection on corruption;
8. event persistence with redaction;
9. strict Host/Origin/CSP/no-permissive-CORS behavior;
10. public liveness reveals no private state;
11. private reads/SSE/mutations reject absent session;
12. bootstrap ticket one-use/expiry;
13. session idle/absolute expiry and service-instance binding;
14. CSRF rejection;
15. runtime descriptor HMAC/tamper/stale logic;
16. collision-safe service port publication logic;
17. second-instance decision logic;
18. browser protocol rejection of malformed input;
19. browser compatibility metadata;
20. no Playwright in service process path;
21. timezone-data named-zone/DST tests;
22. Doctor healthy and broken-state behavior;
23. secret scan across events/log fixtures/diagnostic outputs;
24. package resource manifest generation/verification.

**Command gate:**

```text
python -m pytest tests/unit tests/contract tests/integration -q
python -m pip check
python -m jobscraper --doctor --data-root <isolated-temp-root>
```

The exact packaged/native commands are recorded by S0.13.

**Gate S0.12:** zero failed tests, zero unexplained skips in mandatory Slice 0 tests, no critical security warning, clean dependency check.

**STOP:** any test is weakened/deleted merely to obtain green status.

---

# S0.13 — Native Windows acceptance and Slice 0 promotion

**Purpose:** Test only behavior that cannot be conclusively proven in ordinary offline/unit integration tests, then freeze Slice 0.

**Normative basis:** module 06 real Windows acceptance; module 07 definition of done.

**Execution authority:** architecture reviewer provides one bounded PowerShell acceptance harness or individually named commands after reviewing the S0.12 candidate. Local worker may run it; user performs only unavoidable desktop/UAC/browser observations.

**Required native evidence:**

- packaged `JobScraper.exe` starts by double click/normal invocation;
- exactly one service instance owns lifecycle;
- second launch attaches/focuses existing dashboard rather than spawning second service;
- service binds loopback only and publishes a live OS-assigned port without probe-close-bind race;
- stale runtime descriptor and old-port impersonator are rejected;
- DPAPI secret survives normal restart for same Windows user and is not plaintext at rest;
- browser bootstrap succeeds; ticket not present in server request logs/query; private routes fail without session;
- SQLite opens with expected PRAGMAs in packaged build;
- backup generation restores into a clean isolated root;
- packaged tzdata resolves named zone;
- browser worker and exact Chromium revision launch;
- Chromium is outside service process and is cleaned up after normal shutdown;
- forced browser-worker/Chromium termination is recoverable;
- forced service termination leaves stale state recoverable on next launch;
- Doctor reports actionable healthy state after recovery;
- idle service and one browser-smoke memory are recorded, not used to invent unsupported hard ceilings;
- no dev Python/PYTHONPATH/global browser is needed.

**Promotion decision:**

```text
SLICE0 = PASS
```

only if S0.12 and S0.13 both pass, all mandatory evidence is attached to the exact build ID/commit, and no unresolved critical security/recovery defect remains.

If any item fails, Slice 0 remains **NOT PROMOTED**. Fix only the owning S0.x package, rerun its focused tests, then rerun S0.12 and the affected native subset/full S0.13 as directed by the reviewer.

**After PASS:** tag/freeze the accepted Slice 0 commit/build evidence, then and only then create the detailed Slice 1 backlog.

---

## 5. Review gates between packages

For each S0.x package the reviewer performs:

1. **scope gate** — diff contains no unrelated/future-slice work;
2. **spec gate** — behavior matches owning v0.3.1.3 module;
3. **test gate** — failure-mode tests exist and actually exercise the contract;
4. **security/recovery gate** — no weaker alternate path was introduced;
5. **worker-evidence gate** — claimed commands match repository state;
6. **promotion gate** — approve next package or issue a narrowly bounded corrective.

Do not batch-review several unapproved packages after a worker has already built on top of them. The review boundary is part of the safety model.

---

## 6. Standard worker prompt template

The architecture reviewer should instantiate this template for one package only:

```text
You are implementing Windows Job Scraper Slice 0 package <S0.x> only.

Authority:
1. docs/spec/v0.3.1.3/00_architecture_overview_and_authority.md
2. the owning v0.3.1.3 modules named in <S0.x>
3. docs/plans/slice-0-worker-implementation-plan-v0313.md, package <S0.x>
4. AGENTS.md

Base commit: <exact SHA>

Read those files before editing. Implement only the files/scope allowed by <S0.x>. Use test-first development. Do not begin any later package. Do not redesign architecture. If an ambiguity requires changing a normative contract, STOP and report it instead of guessing.

Run every focused test and gate command named in <S0.x>. Do not delete/weaken tests to pass. Do not commit unrelated files.

At completion, return the exact completion-report format from section 1.4, including base/end commit, files changed, commands with exit status, test counts, known limitations, architecture ambiguities, and whether a STOP condition triggered.
```

GLM/Muse may be given additional line-by-line coding guidance for a package, but that guidance may not widen this template's authority.

---

## 7. Slice 0 explicit deferrals

Slice 0 does **not** implement:

- Source/AdapterDefinition/Binding product model;
- job acquisition from public websites;
- SSRF-controlled source HTTP fetching beyond future interfaces;
- browser source navigation/authenticated-source runtime;
- durable acquisition queue/leases/fallbacks/coverage generations;
- job observations/canonical jobs/Inbox;
- scoring/eligibility/dedup;
- application workflow;
- Adapter Lab/recorder;
- notifications/reminders/scheduler product behavior;
- exports beyond diagnostic foundations required to prove secret redaction;
- optional JobSpy/Crawlee/Scrapling/LLM integrations.

Creating placeholder production implementations for these is forbidden. Later slices add them against the frozen Slice 0 foundations.

---

## 8. ROAD-01 traceability

| v0.3.1.3 Slice 0 requirement | Owning package(s) |
|---|---|
| repository/package skeleton | S0.0 |
| locked dependencies/build metadata | S0.0, S0.11 |
| PyInstaller `--onedir` | S0.11 |
| launcher + single instance | S0.8 |
| FastAPI/Jinja/HTMX shell | S0.6 |
| SQLite connection/migration foundation | S0.2 |
| backup-before-migration | S0.3 |
| event log | S0.4 |
| localhost Host/Origin/mutation security | S0.5–S0.7 |
| protected per-install token | S0.7 |
| Doctor | S0.10 |
| browser-worker skeleton | S0.9 |
| Playwright/browser compatibility | S0.9, S0.10 |
| launcher→service→dashboard bootstrap/session | S0.7–S0.8 |
| authenticated private read/SSE/download boundary | S0.6–S0.7 |
| SQLite foreign-key/WAL/durability verification | S0.2, S0.10 |
| pinned Windows timezone data/Doctor smoke | S0.10–S0.11 |
| application backup-generation manifest + stopped restore | S0.3 |
| packaged Windows ship condition | S0.11–S0.13 |

## 9. Slice 0 final definition of done

Slice 0 is complete only when:

- all S0.0–S0.13 gates are PASS;
- the exact accepted commit and packaged build ID are recorded;
- production dependency locks/build metadata are committed;
- real Windows behavior is proven only where necessary and automated behavior remains automated;
- no unresolved critical security/recovery defect exists;
- no source/job/product functionality has leaked into the foundation slice;
- v0.3.1.3 remains authoritative with no undocumented architecture deviation;
- the repository is clean at the accepted commit;
- the final package can start, authenticate a local dashboard, persist safe local state, back up/restore it, launch/clean a separate browser worker, and report health on Windows without development-environment dependencies.

Only after this definition is satisfied may Slice 1 be decomposed for implementation.
