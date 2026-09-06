# Windows Job Scraper v0.3.1.3 — Correction Ledger

**Date:** 2026-09-06  
**Status:** Non-normative audit trail  
**Normative specification:** the eight numbered Markdown modules in this bundle.

This ledger records the targeted reconciliation applied to v0.3.1.2 after the complete specification review dated 6 September 2026. The architecture was **not redesigned**; the existing eight-module authority split remains authoritative.

## Corrections applied from the v0.3.1.2 review

1. Corrected the two remaining overview ordering summaries to `JobObservation → normalization/entity resolution → canonical Job identity → job_sources`.
2. Moved minimum validity, SSRF, safe-rendering, fencing, downstream-processing and stale-projection protections into the first slice that exposes source content; browser teaching is gated on browser safety.
3. Added coverage-generation membership, multi-page union, host-owned finalization barrier, terminal-enumeration proof and exactly-once absence application.
4. Added deterministic fallback activation, source-plan-group outcomes and a run aggregation truth table including valid empty success and accepted partial/budget-limited outcomes.
5. Defined cancellation-safe handling of already accepted observations via durable host-native processing obligations and separate collection/processing visibility.
6. Added temporal/evidence ordering and conditional current-projection updates so older independently valid work cannot overwrite newer state.
7. Made mandatory HTTP/browser network isolation fail closed when a backend cannot enforce the required destination boundary, including alternate browser network channels.
8. Added host-approved read-acquisition capabilities and resulting-request validation for both HTTP and browser side-effect prevention.
9. Defined HTTP 304 reuse of retained validated representations/membership and prohibited treating a bare 304 as authoritative empty coverage.
10. Added durable provisional Source/generic discovery binding-plan identity before unknown-source fingerprinting I/O.
11. Expanded reproducibility to immutable/resolvable source, query, profile/rule, policy, normalization/evaluator and build inputs and versioned derived outputs.
12. Added reversible merge/undo ownership rules for dispositions, applications/events, notes/documents/reminders and Inbox events; extended reuse guards to origin IDs.
13. Replaced ambiguous Inbox prose with explicit predicate/trigger/disposition transition semantics, stable snooze occurrence identity and stale-tab mutation protection.
14. Defined launcher→service→dashboard bootstrap, short-lived one-time bootstrap ticket, authenticated browser session, private read/SSE/export protection and rotation/revocation behavior.
15. Expanded backup/restore from SQLite consistency to an application-generation manifest with artifact hashes, pruning coordination, stopped/isolated restore and external-reference handling.
16. Set the v0.3.1.3 SQLite durability baseline (`foreign_keys`, WAL, `synchronous=FULL`), added `foreign_key_check`/application consistency, and defined clock-anomaly lease invalidation behavior.
17. Selected IANA timezone identifiers with pinned/bundled timezone data for Windows and explicit spring-gap/autumn-fold/reminder occurrence behavior.
18. Added versioned `PlanningContext`/`ValidatedResultEnvelope`/`ParseContext`/`ParseOutcome` contract requirements, typed closure/missing outcomes, bounded safe imports and explicit host-native non-network dispatch.
19. Removed/rewrote historical or duplicate wording: canonical topology no longer says “Option C”; ScrapeBox historical wording is non-normative; early request ownership names match the corrected request model; duplicate Slice 3 revalidation entry removed; nine-slice count corrected.
20. Added focused acceptance gates for all of the above plus a repeatable packaged-release resource acceptance record.

## Relationship to v0.3.1.2

All v0.3.1.2 architectural foundations remain: Source/Adapter/Binding separation, host-owned I/O, immutable observations, durable ownership/fencing, provenance-first canonicalization, authoritative absence evidence, reversible deduplication, browser isolation, fixture-first maintenance and Windows-first packaging.

This release is a corrective implementation-contract freeze candidate, not a replacement architecture.
