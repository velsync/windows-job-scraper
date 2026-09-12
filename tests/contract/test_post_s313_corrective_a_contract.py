"""Post-S3.13 Corrective A / A0 architecture-contract lock.

A0 is deliberately non-behavioral. It freezes the approved ownership,
migration, authority, and future RED-test vocabulary before A1 changes
production code.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "post_s313_corrective_a_contract_v1.json")
    .read_text(encoding="utf-8")
)
DESIGN = ROOT / "docs" / "reviews" / (
    "post-s3.13-corrective-a-enumeration-lifecycle-authority-2026-09-13.md"
)
REVIEW = ROOT / "docs" / "reviews" / (
    "post-s3.13-corrective-a-design-corrective-review-2026-09-13.md"
)
PLAN = ROOT / "docs" / "superpowers" / "plans" / (
    "2026-09-13-post-s3.13-corrective-a-a0-contract-lock.md"
)
AGENTS = ROOT / "AGENTS.md"


def test_a0_records_user_approved_corrective_a_authority():
    assert FIXTURE["status"] == "APPROVED_ARCHITECTURE_LOCK"
    design = DESIGN.read_text(encoding="utf-8")
    review = REVIEW.read_text(encoding="utf-8")
    agents = AGENTS.read_text(encoding="utf-8")
    assert "APPROVED ARCHITECTURE LOCK" in design
    assert "user approved the design on **2026-09-13**" in review
    assert "Post-S3.13 Corrective A" in agents
    assert "A0 only" in agents


def test_a0_locks_state_lifetimes_without_conflating_cross_run_cache():
    owners = FIXTURE["lifetime_owners"]
    assert owners["ordinary_pagination_cursor"] == "EXACT_RUN_SOURCE_PLAN"
    assert owners["pagination_guard"] == "EXACT_RUN_SOURCE_PLAN"
    assert owners["coverage_generation"] == "EXACT_RUN_SOURCE_PLAN"
    assert owners["cache_representation"] == "CROSS_RUN_EXACT_COMPATIBILITY"
    assert owners["future_incremental_checkpoint"] == "SEPARATE_EXPLICIT_ABSTRACTION"


def test_a0_locks_v22_as_additive_and_preserves_v1_through_v21():
    assert FIXTURE["next_schema_migration"] == 22
    assert FIXTURE["migration_immutability"]["frozen_versions"] == "1-21"
    design = DESIGN.read_text(encoding="utf-8")
    assert "Migrations v1-v21 remain byte-for-byte immutable" in design
    assert "unfinished" in design
    assert "| finalized pre-v22 coverage | unchanged historical evidence |" in design
    review = REVIEW.read_text(encoding="utf-8")
    assert "Finalized historical coverage is untouched." in review


def test_a0_locks_conservative_generic_feed_and_preserves_reviewed_provider_authority():
    contract = FIXTURE["enumeration_contract"]
    assert contract["json_api_feed_default"] == {
        "coverage_authority": "NO_ABSENCE_INFERENCE",
        "pagination_stability": "UNKNOWN",
    }
    assert contract["provider_authority_preserved"] == ["greenhouse", "ashby", "lever"]


def test_a0_locks_observation_effect_vocabulary_for_honest_run_accounting():
    assert FIXTURE["run_effect_values"] == [
        "NEW_JOB",
        "UPDATED_JOB",
        "UNCHANGED_JOB",
        "STALE_IGNORED",
    ]


def test_a0_names_future_red_tests_before_any_behavior_change():
    required = FIXTURE["required_future_behavior_tests"]
    assert set(required) == {"A1", "A2", "A3", "A4", "A5"}
    assert "test_same_plan_restart_loads_exact_cursor_and_guard" in required["A1"]
    assert "test_fresh_plan_does_not_inherit_compatible_prior_plan_cursor" in required["A1"]
    assert "test_json_feed_without_stability_declaration_has_no_absence_authority" in required["A2"]
    assert "test_accepted_304_restores_membership_into_new_coverage" in required["A3"]
    assert "test_overlapping_plans_advance_cursors_and_guards_independently" in required["A4"]
    assert "test_unchanged_reobservation_reports_zero_jobs_updated" in required["A5"]


def test_a0_plan_forbids_production_behavior_changes_and_corrective_b_scope():
    plan = PLAN.read_text(encoding="utf-8")
    assert "A0 changes no `src/jobscraper/**` file" in plan
    assert "Corrective B" in plan
    assert "must not" in plan
    assert "S3.14" not in plan
