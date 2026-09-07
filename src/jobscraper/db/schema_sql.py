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


def _finalize() -> None:
    global MIGRATION_STEPS
    MIGRATION_STEPS = sorted((version, *_STEP[version]) for version in _STEP)


_finalize()

LATEST_SCHEMA_VERSION = MIGRATION_STEPS[-1][0] if MIGRATION_STEPS else 0

assert LATEST_SCHEMA_VERSION == 9, "Slice 1 S1.1 schema is versions 1-9"
