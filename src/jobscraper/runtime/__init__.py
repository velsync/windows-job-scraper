"""Durable run/request core (S1.3).

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md §16
(durable run/frontier contract), §18 (cancellation semantics), RUN-01
(run aggregate states), RUN-02 (immutable RunSourcePlan), RUN-04 (request
model), RUN-05 (request_unique_key), RUN-06 (attempt history), RUN-07
(atomic claim and lease semantics), RUN-08 (fenced terminal commit),
RUN-09 (single-machine capacity coordinator).

The service process is the authoritative durable claim and capacity
coordinator (RUN-09): every claim/heartbeat/commit in this module is
executed by the service (or its in-process executor threads) against the
single SQLite database. Workers never claim durable work independently.

Timestamps: every durable timestamp is produced by ``clock.db_utc_now``
(the database's own UTC clock, rendered in the repository's canonical
RFC-3339 microsecond format) or injected explicitly by callers/tests;
claim/renew/reclaim comparisons always use that same source (RUN-20).
"""
