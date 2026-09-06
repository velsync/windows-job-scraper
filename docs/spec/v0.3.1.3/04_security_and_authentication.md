# Windows Job Scraper v0.3.1.3 — Security & Authentication Specification

**Version:** Windows Job Scraper v0.3.1.3 — Final Corrected Modular Specification Set  
**Date:** 2026-09-05  
**Status:** **NORMATIVE — implementation specification**  
**Target:** Windows 11 · local-first · single-user  
**Core stack:** Python 3.12+ · FastAPI · SQLite + FTS5 · Playwright/Chromium

> This file is one member of the canonical modular specification set. Normative ownership is defined by `00_architecture_overview_and_authority.md`. If duplicated explanatory text conflicts with the normative owner, the normative owner wins.


## SEC-00. Normative ownership

This file owns localhost protections, outbound/browser network boundaries, untrusted-content rendering, secret/auth state, source/recipe import restrictions, authenticated-source isolation and acquisition side-effect limits.

# 2. Non-goals and safety boundary

The application MUST NOT implement or expose as a product feature:

- automatic job-application submission in v0.3.1.3;
- mass outreach;
- generated or guessed recruiter email addresses;
- CAPTCHA solving or defeating;
- paywall bypass;
- login/access-control bypass;
- anti-bot circumvention;
- stolen-cookie acquisition or replay;
- credential extraction from unrelated browser profiles;
- public proxy harvesting as a reliability strategy;
- proxy rotation intended to evade source restrictions;
- TLS/browser-identity rotation intended to defeat `403`, `429`, challenge, or block responses;
- arbitrary remote code loading;
- an untrusted third-party plugin marketplace;
- browser automation for every source;
- unlimited crawling;
- cloud synchronization as a core dependency;
- multi-user hosting;
- distributed crawling;
- mandatory AI extraction or ranking;
- destructive automatic deduplication.

Authenticated acquisition means:

> The user establishes a legitimate session normally in a dedicated source context, and the application automates only content that the user's session is authorized to view.

When an access restriction or challenge is encountered, the permitted behavior is:

```text
classify
→ honor Retry-After / back off
→ lower load
→ try a cleaner already-supported strategy if one exists
→ request explicit user re-authentication/interaction where appropriate
→ otherwise mark the binding challenged/unavailable
```

It is not permitted to turn an access denial into an identity-evasion loop.

---


# 5. Localhost security

Loopback binding is necessary but insufficient.

Required protections:

- loopback-only bind by default;
- random per-install application secret/token;
- mutation protection on every state-changing route;
- Host validation;
- Origin validation when present;
- no permissive CORS;
- same-origin dashboard behavior;
- strict Content Security Policy;
- secure cookie flags where cookies are used;
- request-size limits;
- safe content-type handling;
- snapshot-teaching iframe sandboxing;
- no-network teaching snapshot;
- CSRF or equivalent mutation-token protection;
- no unauthenticated administrative mutation endpoint.

A hostile public website loaded in the user's normal browser MUST NOT be able to change Job Scraper state merely because FastAPI is reachable on loopback.

## 5.1 Outbound SSRF controls

For arbitrary/user-added URLs:

- allow only supported schemes (`http`, `https`);
- reject credentials embedded in URLs;
- resolve hostname before connection;
- reject loopback/private/link-local/special ranges unless a narrowly defined internal feature explicitly permits them;
- validate every redirect;
- cap redirects;
- bind destination validation to the actual connection, including DNS changes, IPv4/IPv6 and configured proxy resolution, and fail closed if the selected backend cannot prove an allowed peer/destination;
- cap body size;
- cap request duration;
- apply source allow-host/path policy;
- never permit `file://` through a source/recipe import;
- never accept arbitrary local browser executable/CDP endpoints from imported configuration.

---


# 6. Secrets and authenticated state

Secrets include:

- API keys;
- cookies;
- authentication headers;
- exported browser storage state;
- proxy credentials;
- user tokens.

Rules:

- never log secrets;
- never export secrets in recipe/source bundles;
- never include secrets in diagnostics;
- never store plaintext passwords;
- use Windows user-scoped OS protection for app-owned secret material;
- apply restrictive ACLs to auth-state files/directories;
- treat Chromium profile internals as opaque browser-managed data;
- adapter configuration stores secret references/capabilities, not secret values.

Preferred authenticated runtime:

```text
user performs normal login in dedicated source setup
→ validate golden session
→ export Playwright storage state where sufficient
→ encrypt/protect app-owned state
→ create isolated runtime browser context
```

Persistent/full-profile mode is a fallback for sources that genuinely require state not represented by the supported storage-state mechanism.

Normal runs MUST NOT silently overwrite the golden authentication state. Persistent/full-profile fallback uses an exclusive, app-owned working profile with no concurrent browser ownership. Golden backup/promotion occurs only from a quiesced/closed profile state; the application does not indiscriminately copy a live Chromium profile. Runtime changes become candidates and follow the validation/promotion flow below.

If refreshed runtime state should be promoted:

```text
runtime candidate
→ validation
→ explicit approval
→ atomic promotion
```

Expired sessions produce `NEEDS_LOGIN`/auth-health evidence rather than parser repair.


Current authorization state overrides pinned historical run configuration. If an auth scope is revoked/invalidated, a permission profile is revoked, or a source/binding is quarantined, the service MUST prevent new execution under that authority and stop/deny commit at the next bounded ownership/policy checkpoint. Historical `RunSourcePlan` data remains for reproducibility only; it is not a continuing authorization grant.

---


# 48. Optional egress profiles

Direct connection is the default.

Optional object:

```text
egress_profiles
---------------
id
type            # DIRECT | PROXY
endpoint_secret_ref
region
desired_state
operational_health
last_tested_at
latency_ms
recent_success_rate
source_constraints_json
```

The host chooses egress according to explicit user configuration and source policy.

No public proxy scraping is required.

No source adapter decides "rotate proxy to evade block."

Adapter manifests may declare egress compatibility, but selection remains host policy.

---


## SEC-01. Per-install mutation/application token

The random per-install mutation/application token is protected application secret material.

It MUST:

- live in an ACL-restricted app-owned location or equivalent protected store;
- never appear in URLs/query strings;
- never be written to ordinary logs/events;
- never be included in diagnostics;
- be rotatable/regeneratable through a safe recovery mechanism;
- be checked together with Host/Origin/same-origin/CSRF-equivalent policy for mutations.

Possession of a loopback address alone is not authorization.

### Launcher → service → dashboard bootstrap

The per-install secret is **not** the browser session credential. The normal launch uses this concrete local strategy:

1. the launcher starts or attaches to the single service instance and validates a protected runtime descriptor containing the loopback endpoint, service-instance ID, PID/start identity and an integrity authenticator derived from protected install state;
2. the service issues the launcher a short-lived, single-use bootstrap ticket through the trusted local launcher/service channel;
3. the launcher opens the dashboard with the ticket in the URL **fragment**, never query/path, so it is not sent in the HTTP request or ordinary server logs;
4. same-origin bootstrap code exchanges the ticket once via a dedicated bootstrap POST that requires the one-time ticket plus strict Host/Origin checks (it does not require a pre-existing browser session); on success the service issues a host-only `HttpOnly`, `SameSite=Strict` browser session cookie plus a separate anti-CSRF token/header mechanism;
5. the bootstrap ticket is invalidated immediately after successful exchange or timeout.

The supported default origin is `http://127.0.0.1:<allocated-port>` with strict Host validation and no permissive CORS. The browser cookie is host-only, `HttpOnly`, `SameSite=Strict`; `Secure` is required if a future HTTPS loopback origin is adopted. Port changes are **not** treated as cookie isolation.

Session requirements:

- private reads, SSE/event streams, exports/downloads and all mutations require a valid browser session; only a minimal non-sensitive liveness endpoint may be unauthenticated;
- sessions are bound to the current service-instance epoch, have explicit idle/absolute lifetime, and can be revoked on service restart/token rotation/security recovery;
- install-secret rotation invalidates existing bootstrap/session authority as defined by recovery policy;
- stale advertised ports/service impersonation cannot pass launcher validation merely by binding the old port;
- hostile Origin/Host or absent/invalid session fails closed.

## SEC-02. Browser-side SSRF and local-network controls

Playwright/Chromium is also an outbound network client and MUST enforce network policy.

Public/authenticated source pages MUST NOT silently use the browser to access:

- `127.0.0.0/8`;
- `::1`;
- RFC1918/private ranges;
- link-local ranges;
- metadata/special-use ranges;
- local services;
- `file:`, `chrome:`, `javascript:` or comparable active/local schemes.

Required browser controls:

- validate top-level navigation;
- validate redirects;
- intercept/validate subrequests where required by policy;
- block private/loopback/link-local destinations by default;
- enforce allowed host scope;
- explicitly approve extra authenticated API hosts;
- apply the same mandatory destination policy to fetch/XHR, subframes, popups, WebSockets and other enabled browser network paths;
- disable service workers and alternate network capabilities by default unless the pinned browser backend has a tested containment mechanism for the required boundary;
- validate DNS/IPv4/IPv6/redirect/proxy-resolved destinations at the connection boundary rather than trusting the URL string alone;
- install containment before pages/connections are created;
- when required enforcement is unavailable, return a denied/unsupported typed result rather than silently bypassing the boundary;
- record policy rejections as typed events.

A narrowly defined internal feature may authorize a local destination only through explicit host policy unavailable to imported source/recipe configuration.


Browser capability posture is default-deny for source-controlled pages unless a tested binding explicitly requires a capability. By default block or tightly control:

- downloads/file writes;
- clipboard read/write;
- geolocation;
- camera/microphone;
- notifications;
- external-protocol launches;
- uncontrolled popups/new windows;
- permission prompts not explicitly anticipated by the binding.

Any allowed download is host-mediated, bounded, content-typed, stored outside executable/application directories, and never auto-opened.

## SEC-03. Untrusted source content / dashboard XSS

Everything fetched from a job source is untrusted.

Rules:

- never render raw source HTML directly into the dashboard;
- HTML escape by default;
- sanitize Markdown/HTML output through an allowlist if rich rendering is enabled;
- never preserve `<script>`, event handlers or executable URL schemes;
- escaped text in diagnostics/events;
- safe external links only;
- external links SHOULD use `rel="noopener noreferrer"`;
- source-controlled SVG/HTML must not execute in application origin;
- snapshot teaching remains sandboxed and network-inert.

A malicious job description MUST NOT be able to read the local token or invoke a mutation route.

## SEC-04. Acquisition side-effect guard

Browser acquisition exists to **read authorized content**, not to transact on the user's behalf.

An acquisition plan MUST NOT:

- submit a job application;
- send a recruiter message;
- place an order/purchase;
- modify account/security settings;
- delete or publish content;
- create comparable business-side effects.

Allowed form submission is limited to explicitly supported navigation/filter/login/consent behavior necessary to reach authorized readable content.

The host MUST enforce this policy for both HTTP and browser acquisition. Binding-specific capabilities declare expected destination, method, operation class and state guards; unknown write-like operations are denied. A read-only search/filter POST can be explicitly authorized. Browser actions are monitored against resulting requests where the backend can enforce them; if semantic safety for a required action cannot be validated, acquisition leaves that action to the user outside automation rather than assuming a selector label is safe.

## SEC-05. Auth storage-state contract

Preferred flow:

```text
dedicated source login context
→ user authenticates normally
→ validate golden session
→ export supported Playwright storage state
→ include IndexedDB where Playwright supports it and the source requires it
→ protect app-owned state
→ instantiate isolated runtime context
```

Persistent profile fallback is used only when a tested source requires state not safely represented by supported storage-state capture.

Rules:

- one source/auth scope cannot read another source's session;
- runtime state never silently overwrites golden state;
- candidate refresh → validate → explicit approval → atomic promotion;
- expiry → `NEEDS_LOGIN`, not recipe repair;
- cookies/tokens never enter recipe exports.

## SEC-06. Source/recipe import contract

Imported source/recipe/navigation configuration MUST:

- validate against a versioned schema;
- declare compatible adapter/API versions;
- keep endpoints within approved host scopes;
- reject `file://`, active local schemes and embedded credentials;
- reject arbitrary browser executable paths;
- reject arbitrary CDP/debug endpoints;
- reject shell/subprocess configuration;
- reject embedded secrets/auth headers/cookies;
- reject executable code;
- reject proxy/egress behavior outside a separately approved trusted configuration flow;
- present a human-readable activation summary/diff;
- require confirmation before activation;
- never auto-promote executable or unknown capabilities.

Imported data cannot expand its own permission profile.

Imported source/recipe documents are parsed with safe non-executing parsers and explicit limits for bytes, nesting/depth, collections/items and aggregate extraction cost. XML external entities, YAML/object constructors or equivalent executable/object-instantiating features are disabled. Archive/import expansion has file-count, per-file and total expanded-size limits and rejects path traversal.

## SEC-07. Authenticated XHR/API discovery

The Adapter Lab may inspect same-session browser network metadata only during an explicit teaching session.

Default trust boundary:

```text
same origin / same site
```

Additional hosts require explicit approval and must pass the browser network policy.

Tokens/cookies are referenced through the auth scope; they are not serialized into the recipe.

## SEC-08. Diagnostics redaction

Redaction occurs **before persistence where practical**, not only at export time.

Sensitive classes include:

- Authorization;
- Cookie / Set-Cookie;
- API keys/tokens;
- storage state;
- proxy credentials;
- mutation/application token;
- browser-profile secrets;
- unrelated local paths/content where unnecessary.

A redaction test corpus must include nested JSON, headers, exception text and URL query fragments.

## SEC-09. Egress boundary

Direct egress is default.

A user-configured proxy may be supported as ordinary networking configuration.

The following are prohibited:

- public proxy harvesting;
- rotating proxy/TLS/browser identity because a source blocked/rate-limited/challenged the request;
- adapter-controlled evasion loops.

`Retry-After`, lower load, cooldown, legitimate supported strategy fallback and user interaction are the permitted responses.

## SEC-10. Security acceptance principle

A local-only app is still a networked browser/server system.

Threat modeling must include:

```text
hostile public webpage
malicious scraped HTML/text
malicious source/recipe import
cross-source auth leakage
browser SSRF
localhost CSRF
unsafe external links
diagnostic secret leakage
stale worker privilege
```
