"""Trust-boundary and fail-closed proof tests for the Slice-3 native harness."""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "native_acceptance_slice3.py"
FROZEN_CANDIDATE = "c1cc7b07af9ce74f5629aa2de6332709e1739c1a"
FROZEN_BUILD_ID = "onewise-5068fbcdb158c649"
FROZEN_PACKAGE_SHA256 = "5068fbcdb158c6495d629cbdc228bf3908a3fd1d131f502d3289d6ca487b461c"


def _load_slice3_module():
    spec = importlib.util.spec_from_file_location("wjs_native_acceptance_slice3", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _install_fake_checks(harness, module, *, missing: str | None = None) -> None:
    harness.list_processes = lambda: []
    for check_id, method_name in module.CHECK_METHODS.items():
        if check_id == missing:
            setattr(harness, method_name, lambda: None)
        else:
            setattr(
                harness,
                method_name,
                lambda check_id=check_id: harness.record(check_id, module.PASS, "test"),
            )


def _healthy_w3_10_inputs():
    module = _load_slice3_module()
    doctor = {
        "exit_code": 0,
        "checks": [
            {
                "name": "database",
                "status": "PASS",
                "details": {
                    "pragmas": {
                        "journal_mode": "WAL",
                        "foreign_keys": 1,
                        "synchronous": 2,
                        "busy_timeout_ms": 5000,
                    }
                },
            }
        ],
    }
    databases = [
        {
            "name": "W3-05:1",
            "integrity_check": ["ok"],
            "foreign_key_check": [],
            "application_consistency_ok": True,
            "fetch_attempt_count": 1,
            "fetch_bytes_total": 1,
        }
    ]
    processes = {"residual_target_pids": [], "browser_cleanup_ok": True}
    resources = {
        "idle_service_memory_bytes": 1,
        "browser_process_tree_peak_memory_bytes": 1,
        "p95_local_search_latency_ms": 1.0,
        "peak_temporary_disk_bytes": 1,
        "wal_growth_bytes": 0,
        "cancellation_responsiveness_ms": 1.0,
        "total_http_bytes": 1,
        "http_bytes_source": "fixture_http_messages",
        "browser_cleanup_ok": True,
    }
    approval = {
        "approved": True,
        "ceilings": {name: 100 for name in module.RESOURCE_MEASUREMENTS},
    }
    return doctor, databases, processes, resources, approval


def test_harness_has_exact_independent_w3_mapping() -> None:
    module = _load_slice3_module()
    assert module.W3_IDS == [f"W3-{i:02d}" for i in range(1, 11)]
    assert list(module.CHECK_METHODS) == module.W3_IDS
    assert len(set(module.CHECK_METHODS.values())) == 10
    for method_name in module.CHECK_METHODS.values():
        assert callable(getattr(module.Slice3Harness, method_name))


def test_harness_source_cannot_import_checkout_production_behavior() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("jobscraper"):
            forbidden.append((node.lineno, node.module))
        if isinstance(node, ast.Import):
            forbidden.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name.startswith("jobscraper")
            )
    assert forbidden == []


def test_harness_uses_current_slice3_schema_names() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "next_attempt_at" not in source
    assert "revalidation_representations" not in source
    assert "INSERT INTO adapter_permission_profiles VALUES" not in source


def test_exe_guard_detects_new_checkout_module_import(tmp_path: Path) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness(
        "exe",
        Path("unused"),
        tmp_path,
        candidate_commit=FROZEN_CANDIDATE,
        build_id=FROZEN_BUILD_ID,
        package_sha256=FROZEN_PACKAGE_SHA256,
    )
    injected_name = "jobscraper.w3_forbidden_probe"
    injected = types.ModuleType(injected_name)
    injected.__file__ = str(REPO_ROOT / "src" / "jobscraper" / "w3_forbidden_probe.py")
    sys.modules[injected_name] = injected
    try:
        with pytest.raises(RuntimeError, match="checkout production module"):
            harness._assert_exe_no_checkout_behavior()
    finally:
        sys.modules.pop(injected_name, None)


def test_exe_guard_rejects_checkout_module_loaded_before_harness_init(tmp_path: Path) -> None:
    module = _load_slice3_module()
    injected_name = "jobscraper.w3_preloaded_forbidden_probe"
    injected = types.ModuleType(injected_name)
    injected.__file__ = str(REPO_ROOT / "src" / "jobscraper" / "w3_probe.py")
    sys.modules[injected_name] = injected
    try:
        harness = module.Slice3Harness(
            "exe",
            Path("unused"),
            tmp_path,
            candidate_commit=FROZEN_CANDIDATE,
            build_id=FROZEN_BUILD_ID,
            package_sha256=FROZEN_PACKAGE_SHA256,
        )
        with pytest.raises(RuntimeError, match="checkout production module"):
            harness._assert_exe_no_checkout_behavior()
    finally:
        sys.modules.pop(injected_name, None)


def test_exe_binding_requires_exact_frozen_identity(tmp_path: Path) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness(
        "exe",
        Path("unused"),
        tmp_path,
        candidate_commit=FROZEN_CANDIDATE,
        build_id="wrong-build",
        package_sha256=FROZEN_PACKAGE_SHA256,
    )
    with pytest.raises(ValueError, match="frozen package identity"):
        harness._require_exe_binding()


def test_exe_binding_verifies_package_record_and_tree_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_slice3_module()
    package_root = tmp_path / "package"
    package_dir = package_root / "dist" / "JobScraper"
    package_dir.mkdir(parents=True)
    (package_dir / "JobScraper.exe").write_bytes(b"fixture")
    (package_dir / "JobScraper").write_bytes(b"fixture")
    verification = {
        "result": "PASS",
        "build_id": FROZEN_BUILD_ID,
        "build_sha256": FROZEN_PACKAGE_SHA256,
        "file_count": 1016,
    }
    record_path = package_root / "package-verification.json"
    record_path.write_text(json.dumps(verification), encoding="utf-8")
    monkeypatch.setattr(
        module,
        "compute_package_tree_hash",
        lambda _path: (FROZEN_PACKAGE_SHA256, 1016),
    )
    harness = module.Slice3Harness(
        "exe",
        package_dir,
        tmp_path / "evidence",
        candidate_commit=FROZEN_CANDIDATE,
        build_id=FROZEN_BUILD_ID,
        package_sha256=FROZEN_PACKAGE_SHA256,
    )
    assert harness._require_exe_binding()["build_id"] == FROZEN_BUILD_ID
    verification["build_id"] = "wrong-build"
    record_path.write_text(json.dumps(verification), encoding="utf-8")
    with pytest.raises(ValueError, match="verification record"):
        harness._require_exe_binding()


def test_doctor_capture_preserves_real_process_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness("dev", None, tmp_path)
    harness.app_command = lambda *_args: ["doctor"]
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=7,
            stdout=json.dumps({"checks": [{"name": "database", "status": "FAIL"}]}),
            stderr="doctor stderr",
        ),
    )
    doctor = harness._doctor_health(tmp_path)
    assert doctor["exit_code"] == 7
    assert doctor["checks"][0]["status"] == "FAIL"


def test_missing_check_is_not_run_and_forces_nonzero_exit(tmp_path: Path) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness("dev", None, tmp_path)
    _install_fake_checks(harness, module, missing="W3-06")
    assert harness.run_slice3() == 1
    record = json.loads((tmp_path / "native-acceptance.json").read_text(encoding="utf-8"))
    assert record["tests"]["W3-06"] == module.NOT_RUN
    assert set(record["tests"]) == set(module.W3_IDS)


def test_only_ten_pass_results_produce_zero_exit(tmp_path: Path) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness("dev", None, tmp_path)
    _install_fake_checks(harness, module)
    assert harness.run_slice3() == 0


def test_w3_01_requires_independent_stale_owner_rejections() -> None:
    module = _load_slice3_module()
    evidence = {
        "prior_attempt_outcome": "ABANDONED",
        "fresh_epoch": True,
        "retry_under_fresh_epoch": True,
        "stale_heartbeat_rejected": True,
        "stale_commit_rejected": True,
    }
    assert module.evaluate_w3_01(evidence) == []
    for field in (
        "fresh_epoch",
        "retry_under_fresh_epoch",
        "stale_heartbeat_rejected",
        "stale_commit_rejected",
    ):
        bad = dict(evidence)
        bad[field] = False
        assert module.evaluate_w3_01(bad)


def test_w3_02_requires_same_durable_retry_after_cooldown() -> None:
    module = _load_slice3_module()
    evidence = {
        "cooldown_persisted": True,
        "premature_redispatches": 0,
        "same_request_retried": True,
        "later_attempt_succeeded": True,
        "attempt_count": 2,
    }
    assert module.evaluate_w3_02(evidence) == []
    bad = dict(evidence)
    bad["same_request_retried"] = False
    assert module.evaluate_w3_02(bad)


def test_w3_03_requires_same_cursor_resume_no_refetch_and_exact_union() -> None:
    module = _load_slice3_module()
    evidence = {
        "checkpoint_persisted": True,
        "same_plan_identity_survived": True,
        "same_plan_resumed": True,
        "already_succeeded_refetches": 0,
        "union_expected": ["a", "b", "c"],
        "union_observed": ["a", "b", "c"],
        "duplicates": 0,
        "terminal_reached": True,
    }
    assert module.evaluate_w3_03(evidence) == []
    bad = dict(evidence)
    bad["already_succeeded_refetches"] = 1
    assert module.evaluate_w3_03(bad)


def test_w3_04_requires_negative_ssrf_scope_dedup_and_provenance() -> None:
    module = _load_slice3_module()
    evidence = {
        "robots_seen": True,
        "sitemap_seen": True,
        "expected_observations": 2,
        "observations": 2,
        "duplicate_fetches": 0,
        "out_of_scope_fetches": 0,
        "negative_ssrf_denied": True,
        "ordinary_provenance": True,
        "budget_within_limit": True,
    }
    assert module.evaluate_w3_04(evidence) == []
    for field in ("negative_ssrf_denied", "ordinary_provenance", "budget_within_limit"):
        bad = dict(evidence)
        bad[field] = False
        assert module.evaluate_w3_04(bad)


def test_w3_05_requires_durable_membership_and_no_revision_fabrication() -> None:
    module = _load_slice3_module()
    evidence = {
        "conditional_304": True,
        "expected_members": ["a", "b"],
        "before_members": ["a", "b"],
        "after_members": ["a", "b"],
        "membership_reused": ["a", "b"],
        "content_revisions_unchanged": True,
        "absence_transitions": 0,
        "compatible_representation": True,
        "members_active": True,
        "reused_representation_id": "rep-1",
        "membership_representation_ids": ["rep-1"],
    }
    assert module.evaluate_w3_05(evidence) == []
    bad = dict(evidence)
    bad["membership_reused"] = []
    assert module.evaluate_w3_05(bad)


def test_w3_06_requires_same_generation_and_exactly_once_same_scope_absence() -> None:
    module = _load_slice3_module()
    evidence = {
        "same_generation_resumed": True,
        "premature_absence": 0,
        "later_complete": True,
        "absence_application_counts": [1],
        "same_scope_only": True,
        "replay_duplicate_transitions": 0,
    }
    assert module.evaluate_w3_06(evidence) == []
    bad = dict(evidence)
    bad["absence_application_counts"] = [2]
    assert module.evaluate_w3_06(bad)


def test_w3_07_requires_real_pinned_fallback_sequence() -> None:
    module = _load_slice3_module()
    evidence = {
        "pinned_ranks": [0, 1, 2],
        "rank_outcomes": ["FAILED", "SATISFIED", "SKIPPED_NOT_NEEDED"],
        "activated_ranks": [0, 1],
        "final_group_outcome": "SATISFIED",
        "final_run_terminal": True,
        "final_run_status": "SUCCEEDED",
    }
    assert module.evaluate_w3_07(evidence) == []
    bad = dict(evidence)
    bad["activated_ranks"] = [0, 2]
    assert module.evaluate_w3_07(bad)


def test_w3_08_requires_real_cancellation_measurement_and_pre_local_boundary() -> None:
    module = _load_slice3_module()
    evidence = {
        "cancel_endpoint_status": 200,
        "cancel_requested": True,
        "accepted_observations": 1,
        "cancellation_before_local_completion": True,
        "source_io_after_cancel": 0,
        "local_obligations_exactly_once": True,
        "obligation_counts_by_type": {"RECONCILE": 1, "ELIGIBILITY": 1, "SCORE": 1},
        "obligations_all_succeeded": True,
        "cancellation_responsiveness_ms": 12.0,
        "final_run_status": "CANCELLED",
        "final_state_honest": True,
    }
    assert module.evaluate_w3_08(evidence) == []
    bad = dict(evidence)
    bad["cancellation_responsiveness_ms"] = None
    assert module.evaluate_w3_08(bad)
    bad = dict(evidence)
    bad["cancellation_before_local_completion"] = False
    assert module.evaluate_w3_08(bad)


def test_w3_09_requires_actual_reverse_order_conflict_across_restart() -> None:
    module = _load_slice3_module()
    evidence = {
        "trusted_active": True,
        "aggregator_conflict": True,
        "reverse_completion_observed": True,
        "restart_between_evidence": True,
        "older_effective_evidence": True,
        "canonical_active": True,
        "aggregator_did_not_close": True,
    }
    assert module.evaluate_w3_09(evidence) == []
    bad = dict(evidence)
    bad["reverse_completion_observed"] = False
    assert module.evaluate_w3_09(bad)


def test_w3_10_health_evaluator_accepts_complete_healthy_record() -> None:
    module = _load_slice3_module()
    assert module.evaluate_w3_10(*_healthy_w3_10_inputs()) == []


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda d, w, p, r, a: d.update(exit_code=1), "Doctor exit code"),
        (
            lambda d, w, p, r, a: d["checks"][0]["details"]["pragmas"].update(journal_mode="DELETE"),
            "journal_mode",
        ),
        (lambda d, w, p, r, a: w[0].update(integrity_check=["corrupt"]), "integrity_check"),
        (lambda d, w, p, r, a: p.update(residual_target_pids=[123]), "residual target"),
        (lambda d, w, p, r, a: r.update(p95_local_search_latency_ms=None), "resource measurement"),
        (lambda d, w, p, r, a: a.update(approved=False), "approval"),
    ],
)
def test_w3_10_health_evaluator_rejects_missing_or_unhealthy_evidence(mutator, expected):
    module = _load_slice3_module()
    doctor, workloads, processes, resources, approval = _healthy_w3_10_inputs()
    mutator(doctor, workloads, processes, resources, approval)
    failures = module.evaluate_w3_10(doctor, workloads, processes, resources, approval)
    assert any(expected.lower() in failure.lower() for failure in failures)


def test_w3_10_has_no_embedded_release_ceiling_constants() -> None:
    module = _load_slice3_module()
    assert not hasattr(module, "RESOURCE_CEILINGS")
    source = SCRIPT.read_text(encoding="utf-8")
    assert "1_073_741_824" not in source
    assert "2_147_483_648" not in source


def test_resource_approval_is_bound_to_exact_frozen_package(tmp_path: Path) -> None:
    module = _load_slice3_module()
    path = tmp_path / "approval.json"
    path.write_text(
        json.dumps(
            {
                "approved": True,
                "candidate_commit": module.FROZEN_CANDIDATE,
                "build_id": module.FROZEN_BUILD_ID,
                "package_sha256": module.FROZEN_PACKAGE_SHA256,
                "ceilings": {name: 10 for name in module.RESOURCE_MEASUREMENTS},
            }
        ),
        encoding="utf-8",
    )
    assert module.load_resource_approval(path)["approved"] is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["build_id"] = "wrong"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        module.load_resource_approval(path)


def test_w3_10_real_health_failure_is_not_downgraded_by_missing_measurement() -> None:
    module = _load_slice3_module()
    doctor, workloads, processes, resources, approval = _healthy_w3_10_inputs()
    doctor["exit_code"] = 1
    resources["cancellation_responsiveness_ms"] = None

    failures = module.evaluate_w3_10(doctor, workloads, processes, resources, approval)

    assert module.classify_w3_10_failures(failures) == module.FAIL


def test_incomplete_exe_matrix_records_blocked_promotion_state(tmp_path: Path) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness(
        "exe",
        Path("unused"),
        tmp_path,
        candidate_commit=FROZEN_CANDIDATE,
        build_id=FROZEN_BUILD_ID,
        package_sha256=FROZEN_PACKAGE_SHA256,
    )
    harness._require_exe_binding = lambda: {}
    harness._assert_exe_no_checkout_behavior = lambda: None
    _install_fake_checks(harness, module, missing="W3-06")

    assert harness.run_slice3() == 1

    record = json.loads((tmp_path / "native-acceptance.json").read_text(encoding="utf-8"))
    assert record["promotion"] == "BLOCKED_NATIVE_ACCEPTANCE"


def test_fixture_byte_accounting_does_not_count_blocked_response_before_release() -> None:
    module = _load_slice3_module()
    state = module.FixtureState()
    release = module.threading.Event()
    reply = module.FixtureReply(200, b"payload", "text/plain", release=release)
    state.add("/blocked", reply)
    result = {}

    def invoke() -> None:
        result["reply"] = state.take("/blocked", {"host": "127.0.0.1"})

    thread = module.threading.Thread(target=invoke, daemon=True)
    thread.start()
    deadline = module.time.monotonic() + 2
    while state.count("/blocked") == 0 and module.time.monotonic() < deadline:
        module.time.sleep(0.01)

    assert state.total_http_bytes == state._request_bytes("/blocked", {"host": "127.0.0.1"})

    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert state.total_http_bytes == (
        state._request_bytes("/blocked", {"host": "127.0.0.1"})
        + state._response_bytes(reply)
    )


def test_w3_05_requires_exact_reused_representation_binding_and_active_members() -> None:
    module = _load_slice3_module()
    evidence = {
        "conditional_304": True,
        "expected_members": ["a", "b"],
        "before_members": ["a", "b"],
        "after_members": ["a", "b"],
        "membership_reused": ["a", "b"],
        "content_revisions_unchanged": True,
        "absence_transitions": 0,
        "compatible_representation": True,
        "members_active": True,
        "reused_representation_id": "rep-1",
        "membership_representation_ids": ["rep-1"],
    }
    assert module.evaluate_w3_05(evidence) == []

    wrong_rep = dict(evidence)
    wrong_rep["membership_representation_ids"] = ["rep-2"]
    assert module.evaluate_w3_05(wrong_rep)

    inactive = dict(evidence)
    inactive["members_active"] = False
    assert module.evaluate_w3_05(inactive)


def test_residual_process_detection_rejects_reused_pid_identity(tmp_path: Path) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness("dev", None, tmp_path)
    harness._owned_process_identities = {(123, "created-a"), (456, "")}
    snapshot = {
        123: {"creation": "created-b", "name": "JobScraper.exe"},
        456: {"creation": "", "name": "JobScraper.exe"},
    }

    assert harness._residual_owned_pids(snapshot) == [456]


def test_w3_08_rejects_duplicate_obligation_types_hidden_by_status_groups() -> None:
    module = _load_slice3_module()
    evidence = {
        "cancel_endpoint_status": 200,
        "cancel_requested": True,
        "accepted_observations": 1,
        "cancellation_before_local_completion": True,
        "source_io_after_cancel": 0,
        "local_obligations_exactly_once": True,
        "obligation_counts_by_type": {"RECONCILE": 2, "ELIGIBILITY": 1, "SCORE": 1},
        "obligations_all_succeeded": True,
        "cancellation_responsiveness_ms": 12.0,
        "final_run_status": "CANCELLED",
        "final_state_honest": True,
    }

    assert module.evaluate_w3_08(evidence)


def test_w3_08_requires_cancelled_terminal_run_after_recovery() -> None:
    module = _load_slice3_module()
    evidence = {
        "cancel_endpoint_status": 200,
        "cancel_requested": True,
        "accepted_observations": 1,
        "cancellation_before_local_completion": True,
        "source_io_after_cancel": 0,
        "local_obligations_exactly_once": True,
        "obligation_counts_by_type": {"RECONCILE": 1, "ELIGIBILITY": 1, "SCORE": 1},
        "obligations_all_succeeded": True,
        "cancellation_responsiveness_ms": 12.0,
        "final_state_honest": True,
        "final_run_status": "RUNNING",
    }

    assert module.evaluate_w3_08(evidence)


def test_fixture_timeout_records_the_actual_504_status() -> None:
    module = _load_slice3_module()

    class TimeoutRelease:
        def wait(self, timeout=None):
            return False

    state = module.FixtureState()
    state.add(
        "/timeout",
        module.FixtureReply(200, b"planned", "text/plain", release=TimeoutRelease()),
    )

    reply = state.take("/timeout", {"host": "127.0.0.1"})

    assert reply.status == 504
    assert state.snapshot()[0]["status"] == 504


def test_browser_peak_memory_stays_missing_until_a_descendant_is_observed(tmp_path: Path) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness("dev", None, tmp_path)

    assert harness._browser_tree_peak_bytes is None


def test_w3_10_requires_full_fixture_http_message_accounting_not_body_bytes_only() -> None:
    module = _load_slice3_module()
    doctor, workloads, processes, resources, approval = _healthy_w3_10_inputs()
    resources["http_bytes_source"] = "durable_fetch_attempts"

    failures = module.evaluate_w3_10(doctor, workloads, processes, resources, approval)

    assert any("HTTP byte measurement" in failure for failure in failures)


def test_w3_10_requires_at_least_one_durable_fetch_attempt_in_workload_corpus() -> None:
    module = _load_slice3_module()
    doctor, workloads, processes, resources, approval = _healthy_w3_10_inputs()
    workloads[0]["fetch_attempt_count"] = 0
    workloads[0]["fetch_bytes_total"] = 0
    resources["total_http_bytes"] = 0

    failures = module.evaluate_w3_10(doctor, workloads, processes, resources, approval)

    assert any("durable fetch attempt" in failure for failure in failures)


def test_resource_sampler_peak_temporary_disk_is_absolute_usage_not_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_slice3_module()
    harness = module.Slice3Harness("dev", None, tmp_path / "evidence")
    root = tmp_path / "root"
    (root / "db").mkdir(parents=True)
    payload = root / "payload.bin"
    payload.write_bytes(b"x" * 128)

    monkeypatch.setattr(harness, "_process_snapshot", lambda: {})
    sampler = module._ResourceSampler(harness, root, 999999)
    sampler.sample()

    assert sampler.peak_temp >= harness._directory_size(root)


def test_w3_05_requires_the_named_fixture_members_not_merely_any_stable_set() -> None:
    module = _load_slice3_module()
    evidence = {
        "conditional_304": True,
        "expected_members": ["a", "b"],
        "before_members": ["a"],
        "after_members": ["a"],
        "membership_reused": ["a"],
        "content_revisions_unchanged": True,
        "absence_transitions": 0,
        "compatible_representation": True,
        "members_active": True,
        "reused_representation_id": "rep-1",
        "membership_representation_ids": ["rep-1"],
    }

    assert module.evaluate_w3_05(evidence)


def test_w3_07_requires_successful_final_run_truth() -> None:
    module = _load_slice3_module()
    evidence = {
        "pinned_ranks": [0, 1, 2],
        "rank_outcomes": ["FAILED", "SATISFIED", "SKIPPED_NOT_NEEDED"],
        "activated_ranks": [0, 1],
        "final_group_outcome": "SATISFIED",
        "final_run_terminal": True,
        "final_run_status": "PARTIAL",
    }

    assert module.evaluate_w3_07(evidence)


def test_w3_03_requires_same_run_source_plan_identity_across_restart() -> None:
    module = _load_slice3_module()
    evidence = {
        "checkpoint_persisted": True,
        "same_plan_identity_survived": False,
        "same_plan_resumed": True,
        "terminal_reached": True,
        "already_succeeded_refetches": 0,
        "union_expected": ["a", "b"],
        "union_observed": ["a", "b"],
        "duplicates": 0,
    }

    assert module.evaluate_w3_03(evidence)

def test_json_reply_defaults_to_200_and_preserves_explicit_status() -> None:
    module = _load_slice3_module()

    normal = module._json_reply({"ok": True})
    explicit = module._json_reply({"error": "rate_limited"}, status=429)

    assert normal.status == 200
    assert json.loads(normal.body.decode("utf-8")) == {"ok": True}
    assert explicit.status == 429