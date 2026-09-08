"""Profile-relative Inbox (S1.8).

Authority: 01 PROD-01 (per-profile job state), PROD-02 (durable inbox
events with deterministic dedupe), §42 (inbox predicate and trigger
grouping).

Durable resurfacing identity: every event carries a ``dedupe_key`` that
deterministically identifies the profile-relative trigger occurrence, so
refresh, restart, rescoring or duplicate delivery of the same trigger
creates zero additional inbox events.
"""
