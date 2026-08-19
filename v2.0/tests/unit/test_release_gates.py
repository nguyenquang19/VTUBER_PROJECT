from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from orchestrator.config_loader import ConfigLoader
from services.evaluation.release_gates import (
    ProductReleaseGateEvaluator,
    ProductReleaseGatePolicy,
)


POLICY = ProductReleaseGatePolicy(
    target_product_version="2.0.0",
    max_trace_refs=16,
    min_completed_reviews=1,
    min_aggregate_improvement=0.001,
    max_ai_smell_increase=0.0,
    max_blind_ab_regressions=0,
    correctness_maximums={
        "unauthorized_executed_actions": 0,
        "unavailable_capability_executed_actions": 0,
        "duplicate_committed_actions": 0,
        "false_committed_world_states": 0,
        "transaction_inconsistencies": 0,
        "unbounded_state_or_queue_growth": 0,
    },
)


def _evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "marker": "mai_product_release_evidence",
        "sanitized": True,
        "target_product_version": "2.0.0",
        "correctness": {name: 0 for name in POLICY.correctness_maximums},
        "closed_loop": {
            "stages": [
                "world", "capability_availability", "decision", "execute", "verify", "commit",
                "world_changed", "capability_changed", "next_decision_sees_change",
            ],
            "trace_refs": ["traj:closed-loop-001"],
        },
        "v1_regression": {
            "persona": True, "conversation_continuity": True, "relationship": True,
            "delivery_reliability": True, "safety": True, "dedup_repetition": True,
            "self_talk_transaction": True, "thread_commit_semantics": True,
            "graceful_shutdown": True, "recovery": True,
        },
        "human_like": {
            "completed_reviews": 2,
            "baseline_aggregate": 3.4,
            "candidate_aggregate": 3.7,
            "baseline_ai_smell_ratio": 0.4,
            "candidate_ai_smell_ratio": 0.3,
            "core_metrics_preserved": True,
            "character_preserved": True,
            "blind_ab_regressions": 0,
        },
        "operations": {
            "preflight_product_version": "2.0.0",
            "preflight_ready": True,
            "release_evidence_sanitized": True,
            "backup_restore_verified": True,
            "permissions_deny_by_default": True,
            "no_secret_or_pii_leak": True,
            "emergency_stop_passed": True,
            "graceful_shutdown_passed": True,
            "rollback_rehearsed": True,
        },
    }


def test_release_gate_eligible_evidence_never_authorizes_or_mutates_release() -> None:
    evaluator = ProductReleaseGateEvaluator(POLICY, enabled=True)

    report = evaluator.evaluate(_evidence())

    assert report["release_eligible"] is True
    assert report["legacy_retirement_allowed"] is True
    assert report["release_authorized"] is False
    assert report["release_mutation_required"] is True
    assert report["failed_gates"] == []
    assert evaluator.get_metrics() == {
        "release_gate_eligible_total": 1,
        "release_gate_evaluated_total": 1,
    }


def test_release_gate_blocks_any_correctness_violation_and_keeps_other_results_visible() -> None:
    evaluator = ProductReleaseGateEvaluator(POLICY, enabled=True)
    evidence = _evidence()
    correctness = evidence["correctness"]
    assert isinstance(correctness, dict)
    correctness["duplicate_committed_actions"] = 1

    report = evaluator.evaluate(evidence)

    assert report["release_eligible"] is False
    assert report["failed_gates"] == ["A_correctness"]
    assert report["gates"]["A_correctness"]["failed_checks"] == ["duplicate_committed_actions"]
    assert report["gates"]["E_operations_security"]["status"] == "passed"


def test_release_gate_blocks_quality_regression_and_wrong_preflight_version() -> None:
    evaluator = ProductReleaseGateEvaluator(POLICY, enabled=True)
    evidence = _evidence()
    quality = evidence["human_like"]
    operations = evidence["operations"]
    assert isinstance(quality, dict) and isinstance(operations, dict)
    quality["candidate_aggregate"] = 3.4
    quality["candidate_ai_smell_ratio"] = 0.5
    quality["blind_ab_regressions"] = 1
    operations["preflight_product_version"] = "1.4.3"

    report = evaluator.evaluate(evidence)

    assert report["release_eligible"] is False
    assert report["failed_gates"] == ["D_human_like_quality", "E_operations_security"]
    assert "aggregate_improved" in report["gates"]["D_human_like_quality"]["failed_checks"]
    assert "ai_smell_not_regressed" in report["gates"]["D_human_like_quality"]["failed_checks"]
    assert "blind_ab_not_regressed" in report["gates"]["D_human_like_quality"]["failed_checks"]
    assert report["gates"]["E_operations_security"]["failed_checks"] == ["preflight_product_version"]


def test_release_gate_rejects_extra_or_unsanitized_evidence_without_echoing_value() -> None:
    evaluator = ProductReleaseGateEvaluator(POLICY, enabled=True)
    evidence = deepcopy(_evidence())
    evidence["prompt"] = "private secret must not be echoed"

    report = evaluator.evaluate(evidence)

    assert report["release_eligible"] is False
    assert report["failed_gates"] == ["invalid_evidence"]
    assert "private secret" not in str(report)
    assert evaluator.get_metrics()["release_gate_invalid_total"] == 1


def test_release_gate_policy_is_loaded_from_evaluation_yaml() -> None:
    loader = ConfigLoader(Path(__file__).resolve().parents[2] / "config")
    loader.load_all()

    policy = ProductReleaseGatePolicy.from_loader(loader)

    assert policy.target_product_version == "2.0.0"
    assert policy.correctness_maximums["false_committed_world_states"] == 0
    assert policy.min_aggregate_improvement > 0