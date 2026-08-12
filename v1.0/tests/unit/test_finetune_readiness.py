from __future__ import annotations

import pytest

from services.evaluation.readiness import (
    CandidateMetrics,
    FineTuneThresholds,
    ReadinessStats,
    assess_finetune_readiness,
    build_candidate_manifest,
    compare_candidate,
)


THRESHOLDS = FineTuneThresholds(2000, 200, 50, 200, 5)


def _stats(**changes) -> ReadinessStats:
    value = {
        "baseline_versioned": True,
        "eligible_sft_turns": 2000,
        "eligible_dpo_pairs": 200,
        "correction_dpo_pairs": 50,
        "reviewed_good_turns": 200,
        "holdout_sessions": 5,
        "schema_frozen": True,
        "persona_frozen": True,
        "context_frozen": True,
        "agenda_frozen": True,
    }
    value.update(changes)
    return ReadinessStats(**value)


def test_readiness_requires_every_data_and_freeze_gate() -> None:
    ready = assess_finetune_readiness(_stats(), THRESHOLDS, contract_id="mai-agent-v1")
    blocked = assess_finetune_readiness(
        _stats(eligible_sft_turns=1999), THRESHOLDS, contract_id="mai-agent-v1",
    )
    assert ready["status"] == "ready"
    assert ready["allowed_next_stage"] == "sft_spike"
    assert blocked["status"] == "not_ready"
    assert blocked["production_model_changed"] is False


def _metrics(model: str, **changes) -> CandidateMetrics:
    value = {
        "contract_id": "mai-agent-v1", "model_id": model,
        "persona_score": 3.0, "continuity_score": 3.0, "repetition_score": 3.0,
        "safety_violations": 0, "p95_latency_ms": 1000,
        "human_review_complete": True,
    }
    value.update(changes)
    return CandidateMetrics(**value)


def test_candidate_must_win_behavior_without_safety_or_latency_regression() -> None:
    result = compare_candidate(
        _metrics("base"),
        _metrics("candidate", persona_score=4, continuity_score=4, repetition_score=4,
                 p95_latency_ms=1100),
        max_safety_regression=0, max_latency_increase_percent=10,
    )
    assert result["status"] == "passed"
    assert result["promotion_allowed"] is True
    assert result["automatic_promotion"] is False
    assert result["rollback_model_id"] == "base"


def test_candidate_fails_when_human_review_or_safety_fails() -> None:
    result = compare_candidate(
        _metrics("base"),
        _metrics("candidate", persona_score=4, continuity_score=4, repetition_score=4,
                 safety_violations=1, human_review_complete=False),
        max_safety_regression=0, max_latency_increase_percent=10,
    )
    assert result["status"] == "failed"
    assert result["promotion_allowed"] is False


def test_dpo_manifest_requires_sft_parent_and_keeps_rollback() -> None:
    with pytest.raises(ValueError, match="SFT parent"):
        build_candidate_manifest(
            candidate_id="dpo-1", candidate_path="models/candidates/dpo-1.gguf",
            baseline_model_id="base", contract_id="mai-agent-v1", training_stage="dpo",
        )
    manifest = build_candidate_manifest(
        candidate_id="dpo-1", candidate_path="models/candidates/dpo-1.gguf",
        baseline_model_id="base", contract_id="mai-agent-v1", training_stage="dpo",
        sft_parent_id="sft-1",
    )
    assert manifest["rollback_model_id"] == "base"
    assert manifest["production_model_changed"] is False
