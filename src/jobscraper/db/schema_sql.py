"""Canonical SQLite schema (forward-only migrations) — Slice 0 baseline.

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md section 50;
docs/plans/slice-0-worker-implementation-plan-v0313.md S0.2/S0.4;
docs/plans/slice-1-worker-implementation-plan-v0313.md S1.1.

Slice 0 created the foundation tables: schema bookkeeping/application
metadata (v1) and the unified event log (v2).

Slice 1 appends the domain model (v3–v9) per RUN-17/RUN-18:
  v3 source/adapter/binding model (02 §8/§9)
  v4 profiles and queries with immutable revisions (RUN-03, 01 §35)
  v5 durable run/request model (RUN-01/02/04/05/06)
  v6 evidence/observation model (§29/§30)
  v7 canonical model (§52, RUN-11/12/13)
  v8 profile-relative state: disposition, inbox events, eligibility, scores
     (PROD-01/02, 01 §36/§41)
  v9 applications and documents (01 §43)

  v11 S2.1 richer acquisition evidence + origin resolution (02 §11.3/§32,
     ACQ-09, 03 §30 — append-only, no released step touched)

  v12 S2.2 companies, location lookup and provenance quality (01 §33/§38/§39)

  v13 S2.3 content-cleaning version + search documents/state/capability
     (01 §34/§45; plain bookkeeping only — the FTS5 virtual table and its
     triggers are provisioned by capability-gated idempotent code in
     jobscraper.search, never by an unconditional migration step)

  v10 S1.1 corrective (architectural review 2026-09-08):
     - companies: normalized_name is a resolution signal, not identity
       (01 §33.1 "weak evidence must not aggressively merge companies") —
       the v7 UNIQUE(normalized_name) is replaced by a non-unique lookup
       index;
     - job_sources: native identity is strong per RUN-15 — a partial unique
       index binds one (source_id, source_job_id, source_identity_generation)
       to one canonical job for non-null native ids; genuine reuse keeps
       incrementing the generation;
     - permission pins resolve: source_adapter_binding_revisions and
       run_source_plans gain composite FKs to
       adapter_permission_profile_revisions(permission_profile_id, revision)
       (02 §9.1, RUN-02 rule 8);
     - field_evidence.observation_id gains an FK to job_observations (03 §30);
     - job_sources.last_absence_coverage_id gains an FK to
       enumeration_coverage (03 RUN-13).

Each step is forward-only; a step may never be edited after being committed to
a release (append a new step instead). The pinned-hash test in
tests/integration/test_schema_slice1.py enforces this for released steps.
"""

from __future__ import annotations

# (version, name, sql)
MIGRATION_STEPS: list[tuple[int, str, str]] = []

_STEP: dict[int, tuple[str, str]] = {}


def _step(version: int, name: str):
    def deco(fn):
        sql = fn()
        if not isinstance(sql, str) or not sql.strip():
            raise AssertionError(f"migration step {version} produced no SQL")
        _STEP[version] = (name, sql.strip())
        return fn

    return deco


# ------------------------------------------------- v1 schema bookkeeping / meta
@_step(1, "foundation_meta")
def _(sql: str = """
CREATE TABLE app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
) -> None:
    return sql


# --------------------------------------------------------- v2 unified event log
@_step(2, "events")
def _(sql: str = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    source_id TEXT,
    binding_id TEXT,
    request_id TEXT
);
CREATE INDEX idx_events_at ON events(at);
CREATE INDEX idx_events_kind ON events(kind, at);
CREATE INDEX idx_events_run ON events(run_id);
"""
) -> None:
    return sql


# --------------------------------------- v3 source / adapter / binding model
@_step(3, "source_adapter_binding_model")
def _(sql: str = """
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    source_family TEXT NOT NULL,
    entry_url TEXT NOT NULL,
    canonical_host TEXT,
    source_access_policy_json TEXT NOT NULL DEFAULT '{}',
    desired_state TEXT NOT NULL DEFAULT 'ENABLED'
        CHECK (desired_state IN ('ENABLED', 'DISABLED')),
    administrative_state TEXT NOT NULL DEFAULT 'NORMAL'
        CHECK (administrative_state IN ('NORMAL', 'QUARANTINED')),
    robots_mode TEXT NOT NULL DEFAULT 'RESPECT',
    terms_notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_checked_at TEXT
);

CREATE TABLE adapter_definitions (
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    adapter_api_version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    is_builtin INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (adapter_id, adapter_version)
);

CREATE TABLE adapter_permission_profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    administrative_state TEXT NOT NULL DEFAULT 'NORMAL'
        CHECK (administrative_state IN ('NORMAL', 'QUARANTINED')),
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE adapter_permission_profile_revisions (
    id TEXT PRIMARY KEY,
    permission_profile_id TEXT NOT NULL
        REFERENCES adapter_permission_profiles(id),
    revision INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE (permission_profile_id, revision)
);

CREATE TABLE source_adapter_bindings (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    display_name TEXT NOT NULL,
    desired_state TEXT NOT NULL DEFAULT 'ENABLED'
        CHECK (desired_state IN ('ENABLED', 'DISABLED')),
    administrative_state TEXT NOT NULL DEFAULT 'NORMAL'
        CHECK (administrative_state IN ('NORMAL', 'QUARANTINED')),
    quarantined_at TEXT,
    quarantine_reason TEXT,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE source_adapter_binding_revisions (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    revision INTEGER NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    strategy TEXT NOT NULL
        CHECK (strategy IN (
            'PROVIDER_NATIVE', 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT',
            'STRUCTURED_PAGE', 'HTTP_HTML', 'PLAYWRIGHT_PUBLIC',
            'PLAYWRIGHT_AUTHENTICATED', 'MANUAL_UNSUPPORTED',
            'GENERIC_DISCOVERY')),
    priority INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    auth_requirement TEXT NOT NULL DEFAULT 'NONE',
    auth_scope_id TEXT,
    execution_class TEXT NOT NULL
        CHECK (execution_class IN ('HTTP', 'BROWSER', 'BROWSER_INTERACTIVE')),
    permission_profile_id TEXT NOT NULL
        REFERENCES adapter_permission_profiles(id),
    permission_profile_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE (binding_id, revision),
    FOREIGN KEY (adapter_id, adapter_version)
        REFERENCES adapter_definitions(adapter_id, adapter_version)
);
CREATE INDEX idx_binding_revisions_adapter
    ON source_adapter_binding_revisions(adapter_id, adapter_version);
"""
) -> None:
    return sql


# ------------------------------------- v4 search profiles / queries (RUN-03)
@_step(4, "profiles_queries")
def _(sql: str = """
CREATE TABLE search_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE profile_revisions (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    revision INTEGER NOT NULL,
    profile_snapshot_json TEXT NOT NULL,
    rules_revision_id TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (profile_id, revision)
);

CREATE TABLE queries (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE query_revisions (
    id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES queries(id),
    profile_revision_id TEXT REFERENCES profile_revisions(id),
    revision INTEGER NOT NULL,
    query_kind TEXT NOT NULL DEFAULT 'KEYWORD',
    query_text_or_json TEXT NOT NULL,
    source_scope_json TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (query_id, revision)
);
"""
) -> None:
    return sql


# --------------------------- v5 durable run/request model (RUN-01/02/04/05/06)
@_step(5, "run_request_model")
def _(sql: str = """
CREATE TABLE scrape_runs (
    id TEXT PRIMARY KEY,
    profile_id TEXT REFERENCES search_profiles(id),
    status TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL',
                          'FAILED', 'CANCELLED')),
    collection_status TEXT,
    processing_status TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    cancel_requested_at TEXT,
    jobs_discovered INTEGER NOT NULL DEFAULT 0,
    jobs_saved INTEGER NOT NULL DEFAULT 0,
    jobs_updated INTEGER NOT NULL DEFAULT 0,
    requests_total INTEGER NOT NULL DEFAULT 0,
    requests_failed INTEGER NOT NULL DEFAULT 0,
    error_summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_runs_status ON scrape_runs(status, created_at);
CREATE INDEX idx_runs_profile ON scrape_runs(profile_id, created_at);

CREATE TABLE run_source_plans (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scrape_runs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_config_snapshot_ref TEXT,
    query_id TEXT REFERENCES queries(id),
    query_revision_id TEXT REFERENCES query_revisions(id),
    source_plan_group_id TEXT NOT NULL,
    fallback_rank INTEGER NOT NULL DEFAULT 0,
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    binding_revision_id TEXT NOT NULL
        REFERENCES source_adapter_binding_revisions(id),
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    adapter_api_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    execution_class TEXT NOT NULL,
    cursor_schema_version INTEGER NOT NULL DEFAULT 1,
    recipe_version_id TEXT,
    navigation_plan_version_id TEXT,
    crawl_policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
    rate_policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
    auth_scope_id TEXT,
    permission_profile_id TEXT NOT NULL,
    permission_profile_revision INTEGER NOT NULL,
    profile_revision TEXT,
    rules_revision TEXT,
    run_config_hash TEXT,
    group_outcome TEXT
        CHECK (group_outcome IS NULL OR group_outcome IN (
            'SATISFIED', 'SATISFIED_PARTIAL', 'FAILED', 'CANCELLED',
            'POLICY_DENIED', 'SKIPPED_NOT_NEEDED')),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, source_plan_group_id, fallback_rank)
);
CREATE INDEX idx_run_source_plans_run ON run_source_plans(run_id);
CREATE INDEX idx_run_source_plans_binding ON run_source_plans(binding_id);

CREATE TABLE scrape_requests (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scrape_runs(id),
    run_source_plan_id TEXT REFERENCES run_source_plans(id),
    query_id TEXT REFERENCES queries(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    request_type TEXT NOT NULL
        CHECK (request_type IN (
            'SOURCE_HEALTH_CHECK', 'SOURCE_DISCOVERY', 'LIST_FETCH',
            'DETAIL_FETCH', 'SOURCE_CRAWL', 'NORMALIZE', 'RECONCILE',
            'ENRICH', 'ELIGIBILITY', 'SCORE', 'ADAPTER_SMOKE', 'EXPORT')),
    request_unique_key TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    strategy TEXT,
    execution_class TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    depth INTEGER NOT NULL DEFAULT 0,
    parent_request_id TEXT REFERENCES scrape_requests(id),
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED',
                          'FAILED', 'CANCELLED')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_retry_at TEXT,
    current_worker_id TEXT,
    current_attempt_id TEXT,
    lease_until TEXT,
    heartbeat_at TEXT,
    cursor_checkpoint_ref TEXT,
    started_at TEXT,
    finished_at TEXT,
    page_class TEXT,
    bytes_downloaded INTEGER,
    duration_ms INTEGER,
    last_failure_kind TEXT,
    last_failure_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, request_unique_key)
);
CREATE INDEX idx_requests_claim ON scrape_requests(status, lease_until);
CREATE INDEX idx_requests_plan ON scrape_requests(run_source_plan_id, status);
CREATE INDEX idx_requests_parent ON scrape_requests(parent_request_id);

CREATE TABLE request_attempts (
    attempt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    execution_plan_id TEXT,
    worker_id TEXT,
    started_at TEXT NOT NULL,
    last_heartbeat_at TEXT,
    lease_expires_at TEXT,
    finished_at TEXT,
    outcome TEXT
        CHECK (outcome IS NULL OR outcome IN (
            'SUCCEEDED', 'FAILED', 'RETRY_WAIT', 'ABANDONED', 'CANCELLED')),
    failure_kind TEXT,
    abandoned_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_request_attempts_request
    ON request_attempts(request_id, started_at);

CREATE TABLE crawl_cursors (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    cursor_schema_version INTEGER NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_at TEXT NOT NULL,
    UNIQUE (binding_id, adapter_id, adapter_version, cursor_schema_version)
);
"""
) -> None:
    return sql


# ---------------------------- v6 evidence / observation model (§29/§30, §40)
@_step(6, "evidence_observation_model")
def _(sql: str = """
CREATE TABLE fetch_attempts (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES request_attempts(attempt_id),
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    requested_url TEXT NOT NULL,
    final_url TEXT,
    status_code INTEGER,
    content_type TEXT,
    body_hash TEXT,
    normalized_content_hash TEXT,
    body_ref TEXT,
    bytes_downloaded INTEGER,
    duration_ms INTEGER,
    redirect_chain_json TEXT,
    was_304 INTEGER NOT NULL DEFAULT 0,
    failure_kind TEXT,
    failure_json TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX idx_fetch_attempts_request
    ON fetch_attempts(request_id, fetched_at);

CREATE TABLE parse_attempts (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES request_attempts(attempt_id),
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    parser_kind TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    outcome_kind TEXT NOT NULL
        CHECK (outcome_kind IN ('SUCCESS_WITH_JOBS', 'SUCCESS_EMPTY',
                                'PARTIAL', 'FAILURE')),
    observation_count INTEGER NOT NULL DEFAULT 0,
    child_task_count INTEGER NOT NULL DEFAULT 0,
    failure_kind TEXT,
    failure_json TEXT,
    parsed_at TEXT NOT NULL
);
CREATE INDEX idx_parse_attempts_request
    ON parse_attempts(request_id, parsed_at);

CREATE TABLE job_observations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scrape_runs(id),
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    attempt_id TEXT REFERENCES request_attempts(attempt_id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    execution_class TEXT NOT NULL,
    query_id TEXT,
    source_job_id TEXT,
    raw_url TEXT,
    canonical_url_candidate TEXT,
    application_url_candidate TEXT,
    page_cursor_json TEXT,
    enumeration_scope_key TEXT,
    source_rank_or_order INTEGER,
    raw_payload_ref TEXT,
    parse_evidence_ref TEXT,
    observed_at TEXT NOT NULL,
    observation_unique_key TEXT NOT NULL,
    UNIQUE (request_id, observation_unique_key)
);
CREATE INDEX idx_observations_source
    ON job_observations(source_id, binding_id, observed_at);
CREATE INDEX idx_observations_source_job_id
    ON job_observations(source_id, source_job_id);
CREATE INDEX idx_observations_run ON job_observations(run_id);

CREATE TABLE field_evidence (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    locator_kind TEXT,
    locator_value TEXT,
    evidence_start INTEGER,
    evidence_end INTEGER,
    value_hash TEXT,
    excerpt TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_field_evidence_observation
    ON field_evidence(observation_id, field_name);

CREATE TABLE enumeration_coverage (
    id TEXT PRIMARY KEY,
    run_source_plan_id TEXT NOT NULL REFERENCES run_source_plans(id),
    source_plan_group_id TEXT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    binding_revision_id TEXT,
    scope_key TEXT NOT NULL,
    generation_key TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    completion_state TEXT
        CHECK (completion_state IS NULL OR completion_state IN (
            'COMPLETE', 'PARTIAL', 'CANCELLED', 'FAILED',
            'BUDGET_EXHAUSTED', 'UNKNOWN')),
    coverage_authority TEXT
        CHECK (coverage_authority IS NULL OR coverage_authority IN (
            'AUTHORITATIVE_FULL_SOURCE', 'AUTHORITATIVE_DECLARED_SCOPE',
            'NON_AUTHORITATIVE_QUERY', 'DETAIL_ONLY',
            'NO_ABSENCE_INFERENCE')),
    stop_reason TEXT,
    pages_completed INTEGER NOT NULL DEFAULT 0,
    items_observed INTEGER NOT NULL DEFAULT 0,
    cursor_terminal INTEGER NOT NULL DEFAULT 0,
    terminal_enumeration_proven INTEGER NOT NULL DEFAULT 0,
    contributing_request_count INTEGER NOT NULL DEFAULT 0,
    absence_inference_allowed INTEGER NOT NULL DEFAULT 0,
    finalized_at TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_source_plan_id, scope_key, generation_key)
);
CREATE INDEX idx_coverage_lookup
    ON enumeration_coverage(completion_state, coverage_authority,
                            source_id, binding_id);

CREATE TABLE coverage_contributing_request (
    coverage_id TEXT NOT NULL REFERENCES enumeration_coverage(id),
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    PRIMARY KEY (coverage_id, request_id)
);

CREATE TABLE coverage_seen_identity (
    coverage_id TEXT NOT NULL REFERENCES enumeration_coverage(id),
    stable_source_identity TEXT NOT NULL,
    source_identity_generation INTEGER NOT NULL DEFAULT 1,
    observation_or_listing_evidence_ref TEXT,
    PRIMARY KEY (coverage_id, stable_source_identity,
                 source_identity_generation)
);

CREATE TABLE entity_resolution_events (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES job_observations(id),
    job_id TEXT,
    stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    match_score REAL,
    reason_code TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    decided_at TEXT NOT NULL
);
CREATE INDEX idx_entity_events_job ON entity_resolution_events(job_id, decided_at);
CREATE INDEX idx_entity_events_observation
    ON entity_resolution_events(observation_id);
"""
) -> None:
    return sql


# ------------------------------------ v7 canonical model (§52, RUN-11/12/13)
@_step(7, "canonical_model")
def _(sql: str = """
CREATE TABLE companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    domain TEXT,
    careers_url TEXT,
    ats_provider TEXT,
    ats_board TEXT,
    country TEXT,
    notes_md TEXT,
    watch INTEGER NOT NULL DEFAULT 0,
    blocklist INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_posting_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (normalized_name)
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    company_id TEXT REFERENCES companies(id),
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    description_md TEXT,
    description_text TEXT,
    description_lang TEXT,
    description_hash TEXT,
    employment_type TEXT,
    experience_level TEXT,
    remote_mode TEXT,
    remote_worldwide INTEGER NOT NULL DEFAULT 0,
    salary_original_text TEXT,
    salary_min REAL,
    salary_max REAL,
    salary_currency TEXT,
    salary_period TEXT,
    salary_annual_min_ref REAL,
    salary_annual_max_ref REAL,
    posted_at TEXT,
    discovered_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_changed_at TEXT,
    listing_status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (listing_status IN ('ACTIVE', 'UNCERTAIN', 'EXPIRED',
                                  'CLOSED', 'WITHDRAWN')),
    fingerprint TEXT,
    canonical_provenance_id TEXT,
    origin_provider TEXT,
    origin_board TEXT,
    origin_job_id TEXT,
    notes_md TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_jobs_company ON jobs(company_id);
CREATE INDEX idx_jobs_listing ON jobs(listing_status, last_seen_at);
CREATE INDEX idx_jobs_origin ON jobs(origin_provider, origin_board, origin_job_id);

CREATE TABLE job_locations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    raw_text TEXT,
    country TEXT,
    region TEXT,
    city TEXT,
    remote INTEGER NOT NULL DEFAULT 0,
    timezone_min TEXT,
    timezone_max TEXT,
    source_location_id TEXT,
    confidence REAL
);
CREATE INDEX idx_job_locations_job ON job_locations(job_id);

CREATE TABLE job_sources (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    source_job_id TEXT,
    source_identity_generation INTEGER NOT NULL DEFAULT 1,
    discovery_url TEXT,
    raw_source_url TEXT,
    canonical_job_url TEXT,
    application_url TEXT,
    origin_url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_changed_at TEXT,
    presence_state TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (presence_state IN ('ACTIVE', 'UNCERTAIN', 'EXPIRED',
                                  'CLOSED', 'WITHDRAWN', 'UNKNOWN')),
    content_revision INTEGER NOT NULL DEFAULT 1,
    last_authoritative_scope_key TEXT,
    last_absence_coverage_id TEXT,
    last_observation_id TEXT REFERENCES job_observations(id),
    source_rank INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, source_id, source_job_id, source_identity_generation)
);
CREATE INDEX idx_job_sources_lookup
    ON job_sources(job_id, source_id, source_job_id);
CREATE INDEX idx_job_sources_source ON job_sources(source_id, source_job_id);
CREATE INDEX idx_job_sources_presence
    ON job_sources(presence_state, last_seen_at, last_verified_at);

CREATE TABLE job_history (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    at TEXT NOT NULL,
    change_class TEXT NOT NULL
        CHECK (change_class IN (
            'UNCHANGED', 'CONTENT_CHANGED', 'SALARY_CHANGED',
            'LOCATION_CHANGED', 'TITLE_CHANGED', 'APPLY_URL_CHANGED',
            'JOB_CLOSED', 'JOB_REOPENED', 'REPOST_DETECTED',
            'PAGE_TEMPLATE_CHANGED', 'EXTRACTION_DRIFT', 'UNKNOWN_CHANGE')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    evidence_ref TEXT
);
CREATE INDEX idx_job_history_job ON job_history(job_id, at);
"""
) -> None:
    return sql


# --------------------- v8 profile-relative state (PROD-01/02, 01 §36/§41)
@_step(8, "profile_relative_state")
def _(sql: str = """
CREATE TABLE job_profile_state (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    disposition TEXT NOT NULL DEFAULT 'NONE'
        CHECK (disposition IN ('NONE', 'SHORTLISTED', 'DISMISSED',
                               'SNOOZED', 'ARCHIVED')),
    dismissed_reason TEXT,
    snoozed_until TEXT,
    first_inbox_at TEXT,
    last_inbox_at TEXT,
    triaged_at TEXT,
    archived_at TEXT,
    row_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_id)
);

CREATE TABLE job_profile_inbox_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    event_kind TEXT NOT NULL
        CHECK (event_kind IN (
            'NEW_ELIGIBLE_APPEARANCE', 'MEANINGFUL_CHANGE', 'REOPENED',
            'SNOOZE_EXPIRED', 'PROFILE_REVISION_ELIGIBLE')),
    trigger_history_id TEXT,
    trigger_content_revision INTEGER,
    trigger_repost_relation_id TEXT,
    trigger_listing_state TEXT,
    created_at TEXT NOT NULL,
    surfaced_at TEXT,
    acknowledged_at TEXT,
    dedupe_key TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (job_id, profile_id, dedupe_key)
);
CREATE INDEX idx_inbox_events_profile
    ON job_profile_inbox_events(profile_id, created_at);
CREATE INDEX idx_inbox_events_surface
    ON job_profile_inbox_events(profile_id, surfaced_at);

CREATE TABLE job_eligibility (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    profile_revision_id TEXT,
    rules_revision_id TEXT,
    job_content_revision INTEGER,
    normalization_version TEXT,
    evaluator_version TEXT,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('ELIGIBLE', 'LIKELY', 'UNCLEAR',
                           'UNLIKELY', 'INELIGIBLE')),
    confidence REAL,
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    rule_version TEXT,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_id)
);

CREATE TABLE job_scores (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    profile_revision_id TEXT,
    rules_revision_id TEXT,
    job_content_revision INTEGER,
    normalization_version TEXT,
    scorer_version TEXT,
    score REAL NOT NULL,
    breakdown_json TEXT NOT NULL DEFAULT '[]',
    rule_version TEXT,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_id)
);
CREATE INDEX idx_job_scores_profile ON job_scores(profile_id, score);
"""
) -> None:
    return sql


# --------------------------------------- v9 applications and documents (§43)
@_step(9, "applications_documents")
def _(sql: str = """
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE applications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    status TEXT NOT NULL DEFAULT 'PREPARING'
        CHECK (status IN ('PREPARING', 'APPLIED', 'SCREENING',
                          'INTERVIEWING', 'OFFER', 'ACCEPTED', 'REJECTED',
                          'WITHDRAWN', 'GHOSTED', 'CLOSED')),
    applied_at TEXT,
    applied_via_url TEXT,
    resume_doc_id TEXT REFERENCES documents(id),
    cover_letter_doc_id TEXT REFERENCES documents(id),
    contact_id TEXT,
    salary_asked TEXT,
    notes_md TEXT,
    next_action_at TEXT,
    next_action_text TEXT,
    closed_at TEXT,
    outcome TEXT,
    outcome_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_applications_job ON applications(job_id);
CREATE INDEX idx_applications_profile ON applications(profile_id, status);
CREATE INDEX idx_applications_next_action
    ON applications(next_action_at);

CREATE TABLE application_events (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_application_events
    ON application_events(application_id, at);
"""
) -> None:
    return sql



# --------------------- v10 S1.1 corrective: identity + referential integrity
@_step(10, "s1_1_corrective_identity_and_referential_integrity")
def _(sql: str = """
-- Forward-only corrective for the S1.1 architectural review (2026-09-08).
-- Released steps v3-v9 are byte-stable; corrections rebuild tables here.
--
-- This step is declared in REBUILD_STEPS, so the migration machinery runs it
-- under SQLite's documented table-rebuild procedure (PRAGMA foreign_keys=OFF
-- for the duration of the step) and refuses to COMMIT unless
-- PRAGMA foreign_key_check is clean for the entire database inside the step
-- transaction. Immediate-FK enforcement cannot host a DROP TABLE of a
-- referenced parent (the implicit DELETE records a deferred violation that
-- re-insertion into the rebuilt table does not clear), and defer_foreign_keys
-- therefore cannot substitute for the documented procedure here.
--
-- The partial unique index on job_sources is created after the copy:
-- pre-existing duplicates of one (source_id, source_job_id, generation)
-- abort this migration (fail-closed) rather than silently picking a winner.

-- companies: drop UNIQUE(normalized_name) (01 §33.1), keep lookup index.
CREATE TABLE companies_v10 (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    domain TEXT,
    careers_url TEXT,
    ats_provider TEXT,
    ats_board TEXT,
    country TEXT,
    notes_md TEXT,
    watch INTEGER NOT NULL DEFAULT 0,
    blocklist INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_posting_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO companies_v10 (id, name, normalized_name, domain, careers_url,
    ats_provider, ats_board, country, notes_md, watch, blocklist,
    first_seen_at, last_posting_at, created_at, updated_at)
    SELECT id, name, normalized_name, domain, careers_url, ats_provider,
        ats_board, country, notes_md, watch, blocklist, first_seen_at,
        last_posting_at, created_at, updated_at
    FROM companies;
DROP TABLE companies;
ALTER TABLE companies_v10 RENAME TO companies;
CREATE INDEX idx_companies_normalized_name ON companies(normalized_name);

-- source_adapter_binding_revisions: permission pin must resolve (02 §9.1).
CREATE TABLE source_adapter_binding_revisions_v10 (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    revision INTEGER NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    strategy TEXT NOT NULL
        CHECK (strategy IN (
            'PROVIDER_NATIVE', 'FEED_OR_PUBLIC_STRUCTURED_ENDPOINT',
            'STRUCTURED_PAGE', 'HTTP_HTML', 'PLAYWRIGHT_PUBLIC',
            'PLAYWRIGHT_AUTHENTICATED', 'MANUAL_UNSUPPORTED',
            'GENERIC_DISCOVERY')),
    priority INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    auth_requirement TEXT NOT NULL DEFAULT 'NONE',
    auth_scope_id TEXT,
    execution_class TEXT NOT NULL
        CHECK (execution_class IN ('HTTP', 'BROWSER', 'BROWSER_INTERACTIVE')),
    permission_profile_id TEXT NOT NULL
        REFERENCES adapter_permission_profiles(id),
    permission_profile_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE (binding_id, revision),
    FOREIGN KEY (adapter_id, adapter_version)
        REFERENCES adapter_definitions(adapter_id, adapter_version),
    FOREIGN KEY (permission_profile_id, permission_profile_revision)
        REFERENCES adapter_permission_profile_revisions(
            permission_profile_id, revision)
);
INSERT INTO source_adapter_binding_revisions_v10 (id, binding_id, revision,
    adapter_id, adapter_version, strategy, priority, config_json,
    auth_requirement, auth_scope_id, execution_class, permission_profile_id,
    permission_profile_revision, created_at, superseded_at)
    SELECT id, binding_id, revision, adapter_id, adapter_version, strategy,
        priority, config_json, auth_requirement, auth_scope_id,
        execution_class, permission_profile_id, permission_profile_revision,
        created_at, superseded_at
    FROM source_adapter_binding_revisions;
DROP TABLE source_adapter_binding_revisions;
ALTER TABLE source_adapter_binding_revisions_v10
    RENAME TO source_adapter_binding_revisions;
CREATE INDEX idx_binding_revisions_adapter
    ON source_adapter_binding_revisions(adapter_id, adapter_version);

-- run_source_plans: the plan's permission pin must resolve (RUN-02 rule 8).
CREATE TABLE run_source_plans_v10 (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scrape_runs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_config_snapshot_ref TEXT,
    query_id TEXT REFERENCES queries(id),
    query_revision_id TEXT REFERENCES query_revisions(id),
    source_plan_group_id TEXT NOT NULL,
    fallback_rank INTEGER NOT NULL DEFAULT 0,
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    binding_revision_id TEXT NOT NULL
        REFERENCES source_adapter_binding_revisions(id),
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    adapter_api_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    execution_class TEXT NOT NULL,
    cursor_schema_version INTEGER NOT NULL DEFAULT 1,
    recipe_version_id TEXT,
    navigation_plan_version_id TEXT,
    crawl_policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
    rate_policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
    auth_scope_id TEXT,
    permission_profile_id TEXT NOT NULL,
    permission_profile_revision INTEGER NOT NULL,
    profile_revision TEXT,
    rules_revision TEXT,
    run_config_hash TEXT,
    group_outcome TEXT
        CHECK (group_outcome IS NULL OR group_outcome IN (
            'SATISFIED', 'SATISFIED_PARTIAL', 'FAILED', 'CANCELLED',
            'POLICY_DENIED', 'SKIPPED_NOT_NEEDED')),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, source_plan_group_id, fallback_rank),
    FOREIGN KEY (permission_profile_id, permission_profile_revision)
        REFERENCES adapter_permission_profile_revisions(
            permission_profile_id, revision)
);
INSERT INTO run_source_plans_v10 (id, run_id, source_id,
    source_config_snapshot_ref, query_id, query_revision_id,
    source_plan_group_id, fallback_rank, binding_id, binding_revision_id,
    adapter_id, adapter_version, adapter_api_version, strategy,
    execution_class, cursor_schema_version, recipe_version_id,
    navigation_plan_version_id, crawl_policy_snapshot_json,
    rate_policy_snapshot_json, auth_scope_id, permission_profile_id,
    permission_profile_revision, profile_revision, rules_revision,
    run_config_hash, group_outcome, created_at)
    SELECT id, run_id, source_id, source_config_snapshot_ref, query_id,
        query_revision_id, source_plan_group_id, fallback_rank, binding_id,
        binding_revision_id, adapter_id, adapter_version, adapter_api_version,
        strategy, execution_class, cursor_schema_version, recipe_version_id,
        navigation_plan_version_id, crawl_policy_snapshot_json,
        rate_policy_snapshot_json, auth_scope_id, permission_profile_id,
        permission_profile_revision, profile_revision, rules_revision,
        run_config_hash, group_outcome, created_at
    FROM run_source_plans;
DROP TABLE run_source_plans;
ALTER TABLE run_source_plans_v10 RENAME TO run_source_plans;
CREATE INDEX idx_run_source_plans_run ON run_source_plans(run_id);
CREATE INDEX idx_run_source_plans_binding ON run_source_plans(binding_id);

-- field_evidence: bind evidence to its observation (03 §30).
CREATE TABLE field_evidence_v10 (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES job_observations(id),
    field_name TEXT NOT NULL,
    locator_kind TEXT,
    locator_value TEXT,
    evidence_start INTEGER,
    evidence_end INTEGER,
    value_hash TEXT,
    excerpt TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO field_evidence_v10 (id, observation_id, field_name, locator_kind,
    locator_value, evidence_start, evidence_end, value_hash, excerpt,
    created_at)
    SELECT id, observation_id, field_name, locator_kind, locator_value,
        evidence_start, evidence_end, value_hash, excerpt, created_at
    FROM field_evidence;
DROP TABLE field_evidence;
ALTER TABLE field_evidence_v10 RENAME TO field_evidence;
CREATE INDEX idx_field_evidence_observation
    ON field_evidence(observation_id, field_name);

-- job_sources: absence attribution must resolve (RUN-13) and native
-- identity is strong within one generation (RUN-15).
CREATE TABLE job_sources_v10 (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    source_job_id TEXT,
    source_identity_generation INTEGER NOT NULL DEFAULT 1,
    discovery_url TEXT,
    raw_source_url TEXT,
    canonical_job_url TEXT,
    application_url TEXT,
    origin_url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_changed_at TEXT,
    presence_state TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (presence_state IN ('ACTIVE', 'UNCERTAIN', 'EXPIRED',
                                  'CLOSED', 'WITHDRAWN', 'UNKNOWN')),
    content_revision INTEGER NOT NULL DEFAULT 1,
    last_authoritative_scope_key TEXT,
    last_absence_coverage_id TEXT REFERENCES enumeration_coverage(id),
    last_observation_id TEXT REFERENCES job_observations(id),
    source_rank INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, source_id, source_job_id, source_identity_generation)
);
INSERT INTO job_sources_v10 (id, job_id, source_id, binding_id,
    source_job_id, source_identity_generation, discovery_url, raw_source_url,
    canonical_job_url, application_url, origin_url, first_seen_at,
    last_seen_at, last_verified_at, last_changed_at, presence_state,
    content_revision, last_authoritative_scope_key, last_absence_coverage_id,
    last_observation_id, source_rank, created_at, updated_at)
    SELECT id, job_id, source_id, binding_id, source_job_id,
        source_identity_generation, discovery_url, raw_source_url,
        canonical_job_url, application_url, origin_url, first_seen_at,
        last_seen_at, last_verified_at, last_changed_at, presence_state,
        content_revision, last_authoritative_scope_key, last_absence_coverage_id,
        last_observation_id, source_rank, created_at, updated_at
    FROM job_sources;
DROP TABLE job_sources;
ALTER TABLE job_sources_v10 RENAME TO job_sources;
CREATE INDEX idx_job_sources_lookup
    ON job_sources(job_id, source_id, source_job_id);
CREATE INDEX idx_job_sources_source ON job_sources(source_id, source_job_id);
CREATE INDEX idx_job_sources_presence
    ON job_sources(presence_state, last_seen_at, last_verified_at);
CREATE UNIQUE INDEX idx_job_sources_native_identity
    ON job_sources(source_id, source_job_id, source_identity_generation)
    WHERE source_job_id IS NOT NULL;
"""
) -> None:
    return sql


# ---------------------- v11 richer acquisition evidence + origin resolution
# Slice 2 S2.1 (02 §11.3 completeness, ACQ-09 versioned contracts, 02 §32
# origin resolver, 03 §30 evidence chain).  Append-only: additive columns on
# the existing evidence/observation/presence tables plus the new immutable
# acquisition_evidence spine.  No released step is edited.
@_step(11, "s2_1_richer_envelope_evidence_and_origin")
def _(sql: str = """
ALTER TABLE fetch_attempts ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE fetch_attempts ADD COLUMN execution_plan_id TEXT;
ALTER TABLE fetch_attempts ADD COLUMN headers_redacted_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE fetch_attempts ADD COLUMN validators_sent_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE fetch_attempts ADD COLUMN robots_decision TEXT;
ALTER TABLE fetch_attempts ADD COLUMN transport TEXT;
ALTER TABLE fetch_attempts ADD COLUMN browser_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fetch_attempts ADD COLUMN resource_blocking_applied INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fetch_attempts ADD COLUMN security_policy_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE fetch_attempts ADD COLUMN structured_payload_ref TEXT;

ALTER TABLE parse_attempts ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE parse_attempts ADD COLUMN validated_page_class TEXT;
ALTER TABLE parse_attempts ADD COLUMN validation_evidence_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE parse_attempts ADD COLUMN result_envelope_ref TEXT;
ALTER TABLE parse_attempts ADD COLUMN cursor_proposal_json TEXT;
ALTER TABLE parse_attempts ADD COLUMN coverage_proposal_json TEXT;
ALTER TABLE parse_attempts ADD COLUMN continuation_required INTEGER NOT NULL DEFAULT 0;
ALTER TABLE parse_attempts ADD COLUMN closure_evidence_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE parse_attempts ADD COLUMN evidence_refs_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE parse_attempts ADD COLUMN review_evidence_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE job_observations ADD COLUMN fetch_attempt_id TEXT
    REFERENCES fetch_attempts(id);
ALTER TABLE job_observations ADD COLUMN parse_attempt_id TEXT
    REFERENCES parse_attempts(id);
ALTER TABLE job_observations ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE field_evidence ADD COLUMN source_url TEXT;
ALTER TABLE field_evidence ADD COLUMN excerpt_hash TEXT;

-- Origin resolution (02 §32) is durable *per source presence*: which
-- employer/board/job id this observation's evidence points at, with the
-- confidence and evidence that made it so.  Resolution never destroys the
-- original source links above it (discovery_url / raw_source_url).
ALTER TABLE job_sources ADD COLUMN origin_provider TEXT;
ALTER TABLE job_sources ADD COLUMN origin_board TEXT;
ALTER TABLE job_sources ADD COLUMN origin_job_id TEXT;
ALTER TABLE job_sources ADD COLUMN origin_resolution_confidence REAL;
ALTER TABLE job_sources ADD COLUMN origin_resolution_evidence_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE job_sources ADD COLUMN origin_resolved_at TEXT;
CREATE INDEX idx_job_sources_origin
    ON job_sources(origin_provider, origin_board, origin_job_id);

CREATE TABLE acquisition_evidence (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    attempt_id TEXT,
    fetch_attempt_id TEXT REFERENCES fetch_attempts(id),
    parse_attempt_id TEXT REFERENCES parse_attempts(id),
    observation_id TEXT REFERENCES job_observations(id),
    kind TEXT NOT NULL
        CHECK (kind IN ('RESULT_ENVELOPE', 'PAGE_VALIDITY', 'SECURITY_POLICY',
                        'ORIGIN_RESOLUTION', 'CONTENT_CLEANING', 'FAILURE',
                        'REVIEW')),
    ref TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_acquisition_evidence_request
    ON acquisition_evidence(request_id, kind);
CREATE INDEX idx_acquisition_evidence_observation
    ON acquisition_evidence(observation_id, kind);
"""
) -> None:
    return sql



# ---------------------- v12 companies, locations index, provenance quality
# Slice 2 S2.2 (01 §33.1 companies, §33.2 locations, §38 stage 2, §39
# canonical provenance selection).  Append-only.
@_step(12, "s2_2_companies_locations_and_provenance_quality")
def _(sql: str = """
-- §39 source-quality class is durable per presence, together with the two
-- inputs the classification consumed, so the decision stays inspectable and
-- replayable after a rule change (never re-derived from mutable config).
ALTER TABLE job_sources ADD COLUMN source_quality_class TEXT;
ALTER TABLE job_sources ADD COLUMN content_kind TEXT;
ALTER TABLE job_sources ADD COLUMN same_host_as_source INTEGER;

-- Strong-signal company identity index (01 §33.1).  Only identifiers that can
-- carry a merge are registered: a normalized name alone is never a key here.
CREATE TABLE company_identifiers (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    kind TEXT NOT NULL
        CHECK (kind IN ('ATS_BOARD', 'APP_HOST', 'ORG_DOMAIN', 'CAREERS_HOST')),
    value TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, value)
);
CREATE INDEX idx_company_identifiers_company ON company_identifiers(company_id);

-- Every company-resolution decision is evidence, including the ones that
-- refused to merge (RUN-15 direction: splits are recoverable, silent merges
-- are not).
CREATE TABLE company_resolution_events (
    id TEXT PRIMARY KEY,
    observation_id TEXT REFERENCES job_observations(id),
    company_id TEXT REFERENCES companies(id),
    decision TEXT NOT NULL
        CHECK (decision IN ('CREATED', 'ATTACHED', 'NAME_ONLY_NEW_COMPANY',
                            'NO_SIGNAL')),
    matched_on TEXT,
    name_conflict INTEGER NOT NULL DEFAULT 0,
    reason_code TEXT,
    signals_json TEXT NOT NULL DEFAULT '{}',
    resolution_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_company_resolution_events_company
    ON company_resolution_events(company_id, created_at);
CREATE INDEX idx_company_resolution_events_observation
    ON company_resolution_events(observation_id);

-- §33.2 locations are read by country/city for filtering on a set of
-- locations (no single location_text exists to index).
CREATE INDEX idx_job_locations_lookup ON job_locations(country, city);

-- Canonical projection records which location rule set produced it.
ALTER TABLE jobs ADD COLUMN location_rules_version TEXT;
ALTER TABLE jobs ADD COLUMN company_resolution_version TEXT;
ALTER TABLE jobs ADD COLUMN provenance_selector_version TEXT;
"""
) -> None:
    return sql



# ---------------------- v13 content-cleaning version + search bookkeeping
# Slice 2 S2.3 (01 §34 deterministic cleaning versioning, §45 FTS5 search).
# Plain bookkeeping only: the FTS5 virtual table + triggers are provisioned
# by capability-gated idempotent code in ``jobscraper.search`` (never by an
# unconditional migration step), so a host without FTS5 migrates cleanly and
# records an honest SUBSTRING_FALLBACK capability.
# ---------------------- v13 content-cleaning version + search bookkeeping
# Slice 2 S2.3 (01 §34 deterministic cleaning versioning, §45 FTS5 search).
# Plain bookkeeping only: the FTS5 virtual table + triggers are provisioned
# by capability-gated idempotent code in ``jobscraper.search`` (never by an
# unconditional migration step), so a host without FTS5 migrates cleanly and
# records an honest SUBSTRING_FALLBACK capability.
@_step(13, "s2_3_content_cleaning_and_search_docs")
def _(sql: str = """
-- The canonical row records which cleaner revision produced its description
-- projection, so a later cleaner revision can never silently rewrite history
-- (01 §34, RUN-21).  NULL until the row is (re-)projected under S2.3.
ALTER TABLE jobs ADD COLUMN content_cleaning_version TEXT;

-- Denormalized search document: a deterministic snapshot of the canonical
-- fields that FTS indexes (title, company, description text, locations,
-- available fact text).  The FTS5 virtual table (provisioned separately) is
-- kept in sync by triggers owned by jobscraper.search; job_search_docs is
-- the durable content source for the SUBSTRING_FALLBACK mode and for
-- revision checks.  doc_id is the stable rowid the FTS rows mirror.
-- Rows are never deleted in this slice (canonical jobs are never deleted).
CREATE TABLE job_search_docs (
    doc_id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    description_text TEXT NOT NULL DEFAULT '',
    locations_text TEXT NOT NULL DEFAULT '',
    fact_text TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Per-job index revision state: re-indexing is a no-op while the indexed
-- content hash matches the current canonical content, so maintenance never
-- duplicates or loses rows.  Rows are never deleted in this slice (canonical
-- jobs are never deleted); plain FK references keep the integrity boundary
-- tight and no trigger is bypassed by a cascading delete (SQLite fires no
-- triggers for FK actions).
CREATE TABLE job_search_state (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    indexed_content_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Capability honesty (01 §45): exactly one row records which search engine is
-- active and why.  FTS5_ACTIVE is recorded only when the FTS5 objects were
-- really provisioned; SUBSTRING_FALLBACK always carries the warning.
CREATE TABLE search_capability (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL
        CHECK (mode IN ('FTS5_ACTIVE', 'SUBSTRING_FALLBACK')),
    fts5_detected INTEGER NOT NULL,
    warning TEXT,
    provisioned_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
) -> None:
    return sql



# --------------- v14 ATS fingerprint + route decision evidence (S2.4, 02 §12)
# Slice 2 S2.4 (02 §12.1 fingerprinting, §12.2 strategy router).
# Append-only: two new evidence tables for the fingerprint classifier and
# strategy router.  Evidence rows are immutable once written.  No released
# step (v1–v13) is touched.
@_step(14, "s2_4_ats_fingerprint_and_route_decision")
def _(sql: str = """
-- Fingerprint evidence (02 §12.1): one row per classify_content() call.
-- Append-only: never deleted or updated.  The source_id is nullable at the
-- table level because a provisional discovery source may not yet exist when
-- the first probe is fingerprinted, but provisioning links it once the source
-- row is created.
CREATE TABLE ats_fingerprints (
    id TEXT PRIMARY KEY,
    source_id TEXT,
    url TEXT NOT NULL,
    family TEXT,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    recommended_adapter_id TEXT,
    fingerprint_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_ats_fingerprints_source
    ON ats_fingerprints(source_id, created_at);
CREATE INDEX idx_ats_fingerprints_family
    ON ats_fingerprints(family, confidence);

-- Route decision evidence (02 §12.2): one row per plan_routes() call.
-- Append-only: never deleted or updated.  Records the complete routing
-- decision including any candidates the host filtered out as unsupported
-- execution class.
CREATE TABLE source_route_decisions (
    id TEXT PRIMARY KEY,
    source_id TEXT,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('SPECIALIZED', 'GENERIC_DISCOVERY_FALLBACK')),
    fingerprint_family TEXT,
    fingerprint_confidence REAL NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    unsupported_candidates_json TEXT NOT NULL DEFAULT '[]',
    fallback_reason TEXT,
    router_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_route_decisions_source
    ON source_route_decisions(source_id, created_at);
CREATE INDEX idx_route_decisions_outcome
    ON source_route_decisions(outcome, created_at);
"""
) -> None:
    return sql


# ----------------------------- v15 S3.0 durable runtime foundation (ROAD-04)
# R2-F1: only storage required by S3.1-S3.4. Fallback/group storage
# belongs to S3.9 if proven necessary, in its next unused migration.
@_step(15, "s3_0_runtime_foundation")
def _(sql: str = """
CREATE TABLE service_clock_epochs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_service_clock_epochs_started
    ON service_clock_epochs(started_at);

ALTER TABLE request_attempts ADD COLUMN service_epoch_id TEXT
    REFERENCES service_clock_epochs(id);
CREATE INDEX idx_request_attempts_service_epoch
    ON request_attempts(service_epoch_id, started_at);

CREATE TABLE binding_host_rate_state (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    host TEXT NOT NULL,
    egress_identity TEXT,
    circuit_state TEXT NOT NULL DEFAULT 'CLOSED',
    cooldown_until TEXT,
    recent_failure_count INTEGER NOT NULL DEFAULT 0
        CHECK (recent_failure_count >= 0),
    recent_success_count INTEGER NOT NULL DEFAULT 0
        CHECK (recent_success_count >= 0),
    last_retry_after TEXT,
    last_rate_event_at TEXT,
    last_success_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_binding_host_rate_state_identity
    ON binding_host_rate_state(binding_id, host, COALESCE(egress_identity, ''));
CREATE INDEX idx_binding_host_rate_state_cooldown
    ON binding_host_rate_state(cooldown_until);

"""
) -> None:
    return sql


# ------------------- v16 S3.5 binding-revision crawler cursor (ROAD-04)
# Rebuild the pre-S3.5 cursor table in place. Cursor lookup identity is the
# compatible binding revision + adapter ID/version + cursor schema: any run
# under unchanged pins resumes the same row, and each adapter decides from
# the stored state whether an earlier run's cursor is reusable for a new run
# (feed-style offsets resume; plan-scoped states such as Lever's restart).
# The RunSourcePlan that wrote/checkpointed the cursor is preserved as
# provenance only (checkpoint_run_source_plan_id) and is never the lookup
# key. Pagination/trap guard state resumes only for the SAME RunSourcePlan;
# a new run always begins with fresh guard state. Existing rows are preserved
# as deliberately unbound legacy rows: migration cannot honestly guess which
# historical binding revision or RunSourcePlan owned a binding-wide cursor.
@_step(16, "s3_5_binding_revision_crawl_cursor")
def _(sql: str = """
CREATE TABLE crawl_cursors_v16 (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    binding_revision_id TEXT REFERENCES source_adapter_binding_revisions(id),
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    cursor_schema_version INTEGER NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    guard_state_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_run_source_plan_id TEXT REFERENCES run_source_plans(id),
    checkpoint_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_crawl_cursors_compatible_identity
    ON crawl_cursors_v16(binding_revision_id, adapter_id, adapter_version, cursor_schema_version)
    WHERE binding_revision_id IS NOT NULL;
CREATE INDEX idx_crawl_cursors_binding
    ON crawl_cursors_v16(binding_id, adapter_id, adapter_version);
CREATE INDEX idx_crawl_cursors_binding_revision
    ON crawl_cursors_v16(binding_revision_id);
CREATE INDEX idx_crawl_cursors_checkpoint_plan
    ON crawl_cursors_v16(checkpoint_run_source_plan_id);

INSERT INTO crawl_cursors_v16 (
    id, source_id, binding_id, binding_revision_id,
    adapter_id, adapter_version, cursor_schema_version, state_json,
    guard_state_json, checkpoint_run_source_plan_id, checkpoint_at)
SELECT id, source_id, binding_id, NULL,
       adapter_id, adapter_version, cursor_schema_version, state_json,
       '{}', NULL, checkpoint_at
  FROM crawl_cursors;

DROP TABLE crawl_cursors;
ALTER TABLE crawl_cursors_v16 RENAME TO crawl_cursors;
""") -> None:
    return sql


def _finalize() -> None:
    global MIGRATION_STEPS
    MIGRATION_STEPS = sorted((version, *_STEP[version]) for version in _STEP)


_finalize()

# Steps whose SQL rebuilds existing tables (SQLite 12-step ALTER procedure).
# The machinery runs these with foreign keys disabled for the duration of the
# step and gates COMMIT on a clean PRAGMA foreign_key_check (03 §50).
REBUILD_STEPS: frozenset[int] = frozenset({10, 16})

LATEST_SCHEMA_VERSION = MIGRATION_STEPS[-1][0] if MIGRATION_STEPS else 0

assert LATEST_SCHEMA_VERSION == 16, (
    "Slice 1 ships versions 1-10; Slice 2 appends v11-v14; S3.0 appends v15; S3.5 appends v16"
)
