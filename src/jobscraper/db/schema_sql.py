"""Canonical SQLite schema (forward-only migrations).

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md RUN-17
(consolidated corrected logical tables), section 51/52; module 01 schemas;
module 02 binding/recipe tables; module 04 auth-scope tables; module 05
events/backup tables.

Each step is forward-only; a step may never be edited after being committed to
a release (append a new step instead). SCHEMA_VERSION in jobscraper.version
must equal the newest step version.
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


# ---------------------------------------------------------------- v1 foundation
@_step(1, "foundation_meta_events")
def _(sql: str = """
CREATE TABLE app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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
""") -> None:
    return sql


# ------------------------------------------------- v2 sources / adapters / bindings
@_step(2, "sources_adapters_bindings")
def _(sql: str = """
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    source_family TEXT,
    entry_url TEXT NOT NULL,
    canonical_host TEXT NOT NULL,
    source_access_policy TEXT NOT NULL DEFAULT 'PUBLIC',
    desired_state TEXT NOT NULL DEFAULT 'ENABLED'
        CHECK (desired_state IN ('ENABLED','DISABLED')),
    administrative_state TEXT NOT NULL DEFAULT 'NORMAL'
        CHECK (administrative_state IN ('NORMAL','QUARANTINED')),
    quarantined_at TEXT,
    quarantine_reason TEXT,
    robots_mode TEXT NOT NULL DEFAULT 'RESPECT',
    terms_notes TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    cadence_seconds INTEGER,
    current_revision_id TEXT,
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_revisions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, revision)
);

CREATE TABLE adapter_definitions (
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    adapter_api_version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (adapter_id, adapter_version)
);

CREATE TABLE adapter_permission_profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    administrative_state TEXT NOT NULL DEFAULT 'NORMAL'
        CHECK (administrative_state IN ('NORMAL','REVOKED')),
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE adapter_permission_profile_revisions (
    id TEXT PRIMARY KEY,
    permission_profile_id TEXT NOT NULL REFERENCES adapter_permission_profiles(id),
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
        CHECK (desired_state IN ('ENABLED','DISABLED')),
    administrative_state TEXT NOT NULL DEFAULT 'NORMAL'
        CHECK (administrative_state IN ('NORMAL','QUARANTINED')),
    quarantined_at TEXT,
    quarantine_reason TEXT,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_adapter_binding_revisions (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES source_adapter_bindings(id),
    revision INTEGER NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    fallback_group TEXT,
    fallback_rank INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    auth_requirement TEXT NOT NULL DEFAULT 'NONE',
    auth_scope_id TEXT,
    execution_class TEXT NOT NULL DEFAULT 'HTTP',
    permission_profile_id TEXT NOT NULL,
    permission_profile_revision TEXT NOT NULL,
    listing_identity_sufficient INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    superseded_at TEXT
);
CREATE UNIQUE INDEX idx_binding_rev_unique
    ON source_adapter_binding_revisions(binding_id, revision);
CREATE INDEX idx_binding_rev_adapter
    ON source_adapter_binding_revisions(adapter_id, adapter_version);
""") -> None:
    return sql


# ------------------------------------------------------------- v3 profiles/queries
@_step(3, "profiles_queries")
def _(sql: str = """
CREATE TABLE search_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_default INTEGER NOT NULL DEFAULT 0,
    timezone TEXT NOT NULL DEFAULT 'UTC',
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
    profile_id TEXT REFERENCES search_profiles(id),
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE query_revisions (
    id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES queries(id),
    profile_revision_id TEXT,
    revision INTEGER NOT NULL,
    query_kind TEXT NOT NULL DEFAULT 'ALL',
    query_text_or_json TEXT NOT NULL DEFAULT '{}',
    source_scope_json TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (query_id, revision)
);
""") -> None:
    return sql


# ------------------------------------------------------------ v4 runs and queue
@_step(4, "runs_plans_requests_attempts")
def _(sql: str = """
CREATE TABLE scrape_runs (
    id TEXT PRIMARY KEY,
    run_kind TEXT NOT NULL DEFAULT 'COLLECT'
        CHECK (run_kind IN ('COLLECT','DISCOVERY','REVALIDATE','SMOKE','PROCESSING')),
    profile_id TEXT REFERENCES search_profiles(id),
    query_id TEXT REFERENCES queries(id),
    status TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')),
    collection_status TEXT NOT NULL DEFAULT 'PENDING',
    processing_status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    cancel_requested_at TEXT,
    cancel_reason TEXT,
    jobs_discovered INTEGER NOT NULL DEFAULT 0,
    jobs_saved INTEGER NOT NULL DEFAULT 0,
    jobs_updated INTEGER NOT NULL DEFAULT 0,
    requests_total INTEGER NOT NULL DEFAULT 0,
    requests_failed INTEGER NOT NULL DEFAULT 0,
    error_summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_runs_status ON scrape_runs(status, created_at);

CREATE TABLE run_source_plans (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scrape_runs(id),
    source_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    source_config_snapshot_ref TEXT NOT NULL,
    query_id TEXT,
    query_revision_id TEXT,
    source_plan_group_id TEXT NOT NULL,
    fallback_rank INTEGER NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    binding_config_snapshot_json TEXT NOT NULL,
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
    permission_profile_revision TEXT NOT NULL,
    profile_revision TEXT,
    rules_revision TEXT,
    run_config_hash TEXT NOT NULL,
    group_status TEXT NOT NULL DEFAULT 'PENDING',
    group_outcome_detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (run_id, source_plan_group_id, fallback_rank)
);
CREATE INDEX idx_rsp_run ON run_source_plans(run_id, group_status);

CREATE TABLE scrape_requests (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scrape_runs(id),
    run_source_plan_id TEXT NOT NULL REFERENCES run_source_plans(id),
    query_id TEXT,
    source_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    request_type TEXT NOT NULL
        CHECK (request_type IN ('SOURCE_HEALTH_CHECK','SOURCE_DISCOVERY','LIST_FETCH',
                                'DETAIL_FETCH','SOURCE_CRAWL','NORMALIZE','RECONCILE',
                                'ENRICH','ELIGIBILITY','SCORE','ADAPTER_SMOKE','EXPORT')),
    request_unique_key TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    strategy TEXT NOT NULL,
    execution_class TEXT NOT NULL DEFAULT 'HTTP',
    priority INTEGER NOT NULL DEFAULT 100,
    depth INTEGER NOT NULL DEFAULT 0,
    parent_request_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED','CANCELLED')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_retry_at TEXT,
    current_worker_id TEXT,
    current_attempt_id TEXT,
    lease_until TEXT,
    heartbeat_at TEXT,
    cursor_checkpoint_ref TEXT,
    coverage_generation_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    page_class TEXT,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    last_failure_kind TEXT,
    last_failure_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, request_unique_key)
);
CREATE INDEX idx_requests_claim ON scrape_requests(status, lease_until, next_retry_at);
CREATE INDEX idx_requests_plan ON scrape_requests(run_source_plan_id, status);
CREATE INDEX idx_requests_parent ON scrape_requests(parent_request_id);

CREATE TABLE request_attempts (
    attempt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    execution_plan_id TEXT,
    worker_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_heartbeat_at TEXT,
    lease_expires_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    failure_kind TEXT,
    abandoned_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_attempts_request ON request_attempts(request_id, started_at);

CREATE TABLE crawl_cursors (
    source_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    cursor_schema_version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    checkpoint_at TEXT NOT NULL,
    PRIMARY KEY (source_id, binding_id, adapter_id, adapter_version, cursor_schema_version)
);

CREATE TABLE host_policy_state (
    scope_key TEXT PRIMARY KEY,
    circuit_state TEXT NOT NULL DEFAULT 'CLOSED'
        CHECK (circuit_state IN ('CLOSED','OPEN','HALF_OPEN')),
    cooldown_until TEXT,
    recent_failure_count INTEGER NOT NULL DEFAULT 0,
    recent_success_count INTEGER NOT NULL DEFAULT 0,
    last_retry_after TEXT,
    last_rate_event_at TEXT,
    last_success_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE processing_obligations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    run_id TEXT,
    request_id TEXT,
    observation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','RUNNING','SATISFIED','CANCELLED')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    claim_token TEXT,
    created_at TEXT NOT NULL,
    satisfied_at TEXT,
    UNIQUE (kind, observation_id)
);
CREATE INDEX idx_obligations_pending ON processing_obligations(status, created_at);
""") -> None:
    return sql


# ---------------------------------------------------------------- v5 evidence
@_step(5, "evidence_observations")
def _(sql: str = """
CREATE TABLE snapshots (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL
        CHECK (kind IN ('RAW_METADATA','NORMALIZED_TEXT','HTML_FRAGMENT',
                        'STRUCTURED_JSON','SCREENSHOT_DEBUG')),
    content_hash TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    content_type TEXT,
    retention_class TEXT NOT NULL DEFAULT 'STANDARD',
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE UNIQUE INDEX idx_snapshots_hash ON snapshots(content_hash, kind);

CREATE TABLE fetch_attempts (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    attempt_id TEXT NOT NULL,
    execution_plan_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    transport TEXT NOT NULL,
    requested_url TEXT NOT NULL,
    final_url TEXT,
    status_code INTEGER,
    content_type TEXT,
    body_ref TEXT,
    body_hash TEXT,
    normalized_content_hash TEXT,
    redirect_chain_json TEXT NOT NULL DEFAULT '[]',
    headers_redacted_json TEXT NOT NULL DEFAULT '{}',
    validators_sent_json TEXT NOT NULL DEFAULT '{}',
    was_304 INTEGER NOT NULL DEFAULT 0,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    failure_kind TEXT,
    failure_json TEXT,
    robots_decision TEXT,
    resource_blocking_applied TEXT
);
CREATE INDEX idx_fetch_request ON fetch_attempts(request_id, started_at);

CREATE TABLE parse_attempts (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    attempt_id TEXT NOT NULL,
    fetch_attempt_id TEXT REFERENCES fetch_attempts(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    parser_id TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    recipe_version_id TEXT,
    outcome_kind TEXT NOT NULL,
    failure_kind TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_parse_request ON parse_attempts(request_id, started_at);

CREATE TABLE job_observations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scrape_runs(id),
    request_id TEXT NOT NULL REFERENCES scrape_requests(id),
    attempt_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
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
    observed_at TEXT NOT NULL,
    raw_payload_ref TEXT,
    parse_evidence_ref TEXT,
    observation_unique_key TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    is_closure_evidence INTEGER NOT NULL DEFAULT 0,
    closure_kind TEXT,
    UNIQUE (request_id, observation_unique_key)
);
CREATE INDEX idx_obs_source_time ON job_observations(source_id, binding_id, observed_at);
CREATE INDEX idx_obs_source_job ON job_observations(source_id, source_job_id);
CREATE INDEX idx_obs_run ON job_observations(run_id);

CREATE TABLE field_evidence (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES job_observations(id),
    attempt_id TEXT NOT NULL,
    field TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    evidence_text TEXT,
    evidence_start INTEGER,
    evidence_end INTEGER,
    locator_json TEXT,
    confidence REAL NOT NULL DEFAULT 0.5
);
CREATE INDEX idx_field_evidence_obs ON field_evidence(observation_id);
""") -> None:
    return sql


# ----------------------------------------------------- v6 coverage and cache
@_step(6, "coverage_cache")
def _(sql: str = """
CREATE TABLE enumeration_coverage (
    id TEXT PRIMARY KEY,
    run_source_plan_id TEXT NOT NULL REFERENCES run_source_plans(id),
    source_plan_group_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    generation_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    completion_state TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (completion_state IN ('COMPLETE','PARTIAL','CANCELLED','FAILED',
                                    'BUDGET_EXHAUSTED','UNKNOWN')),
    stop_reason TEXT,
    pages_completed INTEGER NOT NULL DEFAULT 0,
    items_observed INTEGER NOT NULL DEFAULT 0,
    cursor_terminal INTEGER NOT NULL DEFAULT 0,
    terminal_enumeration_proven INTEGER NOT NULL DEFAULT 0,
    contributing_request_count INTEGER NOT NULL DEFAULT 0,
    coverage_authority TEXT NOT NULL DEFAULT 'NO_ABSENCE_INFERENCE'
        CHECK (coverage_authority IN ('AUTHORITATIVE_FULL_SOURCE',
                                      'AUTHORITATIVE_DECLARED_SCOPE',
                                      'NON_AUTHORITATIVE_QUERY','DETAIL_ONLY',
                                      'NO_ABSENCE_INFERENCE')),
    absence_inference_allowed INTEGER NOT NULL DEFAULT 0,
    finalized_at TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_source_plan_id, scope_key, generation_key)
);
CREATE INDEX idx_coverage_state ON enumeration_coverage(completion_state, coverage_authority, source_id, binding_id);

CREATE TABLE coverage_contributing_request (
    coverage_id TEXT NOT NULL REFERENCES enumeration_coverage(id),
    request_id TEXT NOT NULL,
    PRIMARY KEY (coverage_id, request_id)
);

CREATE TABLE coverage_seen_identity (
    coverage_id TEXT NOT NULL REFERENCES enumeration_coverage(id),
    stable_source_identity TEXT NOT NULL,
    source_identity_generation INTEGER NOT NULL DEFAULT 1,
    observation_or_listing_evidence_ref TEXT,
    PRIMARY KEY (coverage_id, stable_source_identity, source_identity_generation)
);

CREATE TABLE cache_representations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    binding_revision_id TEXT NOT NULL,
    auth_scope_generation TEXT NOT NULL DEFAULT 'NONE',
    request_variant_key TEXT NOT NULL,
    validated_page_class TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parser_recipe_compatibility_key TEXT NOT NULL,
    membership_ref TEXT,
    stored_at TEXT NOT NULL,
    expires_at TEXT,
    retention_policy TEXT NOT NULL DEFAULT 'STANDARD',
    UNIQUE (source_id, binding_revision_id, auth_scope_generation, request_variant_key)
);
""") -> None:
    return sql


# ------------------------------------------------------- v7 canonical domain
@_step(7, "canonical_domain")
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
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_companies_identity ON companies(normalized_name, ifnull(domain,''));

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
    salary_ref_currency TEXT,
    salary_confidence REAL,
    posted_at TEXT,
    discovered_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_changed_at TEXT,
    content_revision INTEGER NOT NULL DEFAULT 1,
    listing_status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (listing_status IN ('ACTIVE','UNCERTAIN','EXPIRED','CLOSED','WITHDRAWN')),
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
CREATE INDEX idx_jobs_listing ON jobs(listing_status);
CREATE INDEX idx_jobs_presence ON jobs(last_seen_at, last_verified_at);
CREATE INDEX idx_jobs_origin ON jobs(origin_provider, origin_board, origin_job_id);
CREATE INDEX idx_jobs_fingerprint ON jobs(fingerprint);

CREATE TABLE job_locations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    raw_text TEXT,
    country TEXT,
    region TEXT,
    city TEXT,
    remote INTEGER NOT NULL DEFAULT 0,
    timezone_min TEXT,
    timezone_max TEXT,
    source_location_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.5
);
CREATE INDEX idx_job_locations_job ON job_locations(job_id);

CREATE TABLE job_history (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    at TEXT NOT NULL,
    change_class TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    evidence_observation_id TEXT,
    source_id TEXT
);
CREATE INDEX idx_job_history_job ON job_history(job_id, at);

CREATE TABLE job_facts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    fact_type TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_text TEXT,
    evidence_start INTEGER,
    evidence_end INTEGER,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX idx_job_facts_job ON job_facts(job_id, fact_type);

CREATE TABLE job_eligibility (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    profile_revision_id TEXT,
    rules_revision_id TEXT,
    job_content_revision INTEGER NOT NULL,
    normalization_version TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('ELIGIBLE','LIKELY','UNCLEAR','UNLIKELY','INELIGIBLE')),
    confidence REAL NOT NULL DEFAULT 0.5,
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    rule_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_id)
);

CREATE TABLE job_scores (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    profile_revision_id TEXT,
    rules_revision_id TEXT,
    job_content_revision INTEGER NOT NULL,
    normalization_version TEXT NOT NULL,
    scorer_version TEXT NOT NULL,
    score REAL NOT NULL,
    breakdown_json TEXT NOT NULL DEFAULT '[]',
    rule_version TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_id)
);
CREATE INDEX idx_job_scores_profile ON job_scores(profile_id, score);

CREATE TABLE job_merges (
    id TEXT PRIMARY KEY,
    kept_job_id TEXT NOT NULL,
    absorbed_job_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    match_score REAL,
    reason_code TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    merged_at TEXT NOT NULL,
    undone_at TEXT,
    undone_by TEXT
);

CREATE TABLE job_aliases (
    job_id TEXT NOT NULL,
    alias_of_job_id TEXT NOT NULL,
    merge_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, alias_of_job_id)
);

CREATE TABLE entity_resolution_events (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES job_observations(id),
    job_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('CREATE','SELECT','SPLIT','REVIEW_REQUIRED')),
    stage TEXT NOT NULL,
    match_evidence_json TEXT NOT NULL DEFAULT '{}',
    normalization_version TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX idx_eres_obs ON entity_resolution_events(observation_id);

CREATE TABLE job_sources (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT,
    source_job_id TEXT,
    discovery_url TEXT,
    raw_source_url TEXT,
    canonical_job_url TEXT,
    application_url TEXT,
    origin_url TEXT,
    origin_provider TEXT,
    origin_board TEXT,
    origin_job_id TEXT,
    origin_resolution_confidence REAL,
    origin_resolution_evidence TEXT,
    origin_resolved_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_changed_at TEXT,
    presence_state TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (presence_state IN ('ACTIVE','UNCERTAIN','EXPIRED','CLOSED','WITHDRAWN','UNKNOWN')),
    content_revision INTEGER NOT NULL DEFAULT 1,
    evidence_order TEXT NOT NULL,
    last_authoritative_scope_key TEXT,
    last_absence_coverage_id TEXT,
    last_observation_id TEXT,
    source_rank INTEGER NOT NULL DEFAULT 100,
    source_identity_generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_job_sources_identity
    ON job_sources(job_id, source_id, ifnull(source_job_id,''), source_identity_generation);
CREATE INDEX idx_job_sources_lookup ON job_sources(source_id, source_job_id);
CREATE INDEX idx_job_sources_job ON job_sources(job_id);

CREATE TABLE job_relations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('REPOST_CANDIDATE','REOPEN','DUPLICATE_CLUSTER')),
    from_job_id TEXT NOT NULL,
    to_job_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
""") -> None:
    return sql


# ---------------------------------------------------------------- v8 workflow
@_step(8, "workflow_user_state")
def _(sql: str = """
CREATE TABLE job_profile_state (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT NOT NULL REFERENCES search_profiles(id),
    disposition TEXT NOT NULL DEFAULT 'NONE'
        CHECK (disposition IN ('NONE','SHORTLISTED','DISMISSED','SNOOZED','ARCHIVED')),
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
        CHECK (event_kind IN ('NEW_ELIGIBLE_APPEARANCE','MEANINGFUL_CHANGE','REOPENED',
                              'SNOOZE_EXPIRED','PROFILE_REVISION_ELIGIBLE')),
    trigger_history_id TEXT,
    trigger_content_revision INTEGER,
    trigger_repost_relation_id TEXT,
    trigger_listing_state TEXT,
    created_at TEXT NOT NULL,
    surfaced_at TEXT,
    acknowledged_at TEXT,
    dedupe_key TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX idx_inbox_events_dedupe
    ON job_profile_inbox_events(job_id, profile_id, dedupe_key);

CREATE TABLE applications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    profile_id TEXT REFERENCES search_profiles(id),
    status TEXT NOT NULL DEFAULT 'PREPARING'
        CHECK (status IN ('PREPARING','APPLIED','SCREENING','INTERVIEWING','OFFER',
                          'ACCEPTED','REJECTED','WITHDRAWN','GHOSTED','CLOSED')),
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
    row_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_applications_job ON applications(job_id);
CREATE INDEX idx_applications_next_action ON applications(next_action_at);

CREATE TABLE application_events (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_application_events ON application_events(application_id, at);

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    external INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE contacts (
    id TEXT PRIMARY KEY,
    company_id TEXT REFERENCES companies(id),
    job_id TEXT REFERENCES jobs(id),
    name TEXT NOT NULL,
    role TEXT,
    email TEXT,
    phone TEXT,
    profile_url TEXT,
    company_contact_url TEXT,
    contact_type TEXT,
    source_url TEXT,
    source_name TEXT,
    found_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    verified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    profile_id TEXT,
    job_id TEXT,
    application_id TEXT REFERENCES applications(id),
    kind TEXT NOT NULL,
    due_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    local_due_at TEXT,
    occurrence_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','DELIVERED','CANCELLED')),
    detail TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (occurrence_key)
);
CREATE INDEX idx_reminders_due ON reminders(status, due_at_utc);

CREATE TABLE notification_state (
    id TEXT PRIMARY KEY,
    reminder_id TEXT NOT NULL REFERENCES reminders(id),
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    at TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE user_feedback (
    id TEXT PRIMARY KEY,
    profile_id TEXT,
    job_id TEXT,
    action TEXT NOT NULL,
    reason TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE fx_rates (
    currency TEXT PRIMARY KEY,
    usd_per_unit REAL NOT NULL,
    as_of TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'builtin',
    version TEXT NOT NULL DEFAULT '1'
);
""") -> None:
    return sql


# --------------------------------------------- v9 recipes / lab / health / auth
@_step(9, "recipes_lab_health_auth")
def _(sql: str = """
CREATE TABLE recipes (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    display_name TEXT NOT NULL,
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE recipe_versions (
    id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL REFERENCES recipes(id),
    version INTEGER NOT NULL,
    recipe_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CANDIDATE'
        CHECK (status IN ('CANDIDATE','ACTIVE','RETIRED','REJECTED')),
    validation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    retired_at TEXT,
    UNIQUE (recipe_id, version)
);

CREATE TABLE navigation_plans (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    display_name TEXT NOT NULL,
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE navigation_plan_versions (
    id TEXT PRIMARY KEY,
    navigation_plan_id TEXT NOT NULL REFERENCES navigation_plans(id),
    version INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CANDIDATE'
        CHECK (status IN ('CANDIDATE','ACTIVE','RETIRED','REJECTED')),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (navigation_plan_id, version)
);

CREATE TABLE source_fixtures (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    binding_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    redacted INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (source_id, kind, name)
);

CREATE TABLE locator_health (
    id TEXT PRIMARY KEY,
    recipe_version_id TEXT NOT NULL REFERENCES recipe_versions(id),
    locator_key TEXT NOT NULL,
    locator_kind TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    last_success_at TEXT,
    UNIQUE (recipe_version_id, locator_key)
);

CREATE TABLE source_binding_health (
    binding_id TEXT NOT NULL,
    binding_revision_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    operational_state TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (operational_state IN ('UNKNOWN','HEALTHY','DEGRADED','RATE_LIMITED',
                                     'CHALLENGED','NEEDS_LOGIN','BROKEN')),
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    last_healthy_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (binding_id, binding_revision_id, strategy)
);

CREATE TABLE source_health_events (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision_id TEXT,
    at TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_source_health_events ON source_health_events(source_id, at);

CREATE TABLE auth_scopes (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    label TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'STORAGE_STATE'
        CHECK (mode IN ('STORAGE_STATE','PERSISTENT_PROFILE')),
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE','EXPIRED','REVOKED')),
    state_ref TEXT,
    golden_state_hash TEXT,
    candidate_state_hash TEXT,
    candidate_status TEXT,
    generation INTEGER NOT NULL DEFAULT 1,
    last_validated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, label)
);
""") -> None:
    return sql


# ------------------------------------------------------ v10 lab / imports / egress
@_step(10, "lab_imports_egress")
def _(sql: str = """
CREATE TABLE adapter_lab_sessions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    mode TEXT NOT NULL DEFAULT 'PUBLIC'
        CHECK (mode IN ('PUBLIC','AUTHENTICATED')),
    status TEXT NOT NULL DEFAULT 'OPEN',
    state_json TEXT NOT NULL DEFAULT '{}',
    candidate_recipe_version_id TEXT,
    candidate_navigation_plan_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE import_records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('RECIPE','SOURCE_CONFIG','NAVIGATION_PLAN')),
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    validation_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','REJECTED','IMPORTED','ACTIVATED')),
    imported_at TEXT,
    activated_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE egress_profiles (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'DIRECT' CHECK (type IN ('DIRECT','PROXY')),
    endpoint_secret_ref TEXT,
    region TEXT,
    desired_state TEXT NOT NULL DEFAULT 'DISABLED',
    operational_health TEXT NOT NULL DEFAULT 'UNKNOWN',
    last_tested_at TEXT,
    latency_ms INTEGER,
    recent_success_rate REAL,
    source_constraints_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE backup_records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('AUTO','PRE_MIGRATION','USER','UPGRADE')),
    generation_dir TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (status IN ('CREATED','VERIFIED','RESTORED','FAILED')),
    created_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
""") -> None:
    return sql


# ----------------------------------------------------------------- v11 FTS
@_step(11, "fts5_search")
def _(sql: str = """
CREATE VIRTUAL TABLE jobs_fts USING fts5(
    job_id UNINDEXED,
    title,
    company,
    description,
    locations,
    facts,
    tokenize='porter unicode61'
);
""") -> None:
    return sql


# --------------------------------------------------- v12 run/audit finalization
@_step(12, "audit_indexes_policy")
def _(sql: str = """
CREATE TABLE policy_snapshots (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, content_hash)
);

CREATE TABLE resource_measurements (
    id TEXT PRIMARY KEY,
    scenario TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_requests_source ON scrape_requests(source_id, status);
CREATE INDEX idx_observation_scope ON job_observations(source_id, enumeration_scope_key);
CREATE INDEX idx_job_profile_state_profile ON job_profile_state(profile_id, disposition);
CREATE INDEX idx_inbox_events_created ON job_profile_inbox_events(profile_id, created_at);
CREATE INDEX idx_jobs_posted ON jobs(posted_at);
""") -> None:
    return sql


# --------------------------------------------- v13 canonical projection evidence
@_step(13, "jobs_projection_evidence_at")
def _(sql: str = """
ALTER TABLE jobs ADD COLUMN projection_evidence_at TEXT;
""") -> None:
    return sql


def _freeze() -> None:
    for version in sorted(_STEP):
        name, sql = _STEP[version]
        MIGRATION_STEPS.append((version, name, sql))


_freeze()

LATEST_SCHEMA_VERSION = MIGRATION_STEPS[-1][0]