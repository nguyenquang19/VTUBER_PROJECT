"""Fine-tune readiness and candidate promotion gates for M8."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ReadinessStats:
    baseline_versioned: bool
    eligible_sft_turns: int
    eligible_dpo_pairs: int
    correction_dpo_pairs: int
    reviewed_good_turns: int
    holdout_sessions: int
    schema_frozen: bool
    persona_frozen: bool
    context_frozen: bool
    agenda_frozen: bool


@dataclass(frozen=True)
class FineTuneThresholds:
    min_sft_turns: int
    min_dpo_pairs: int
    min_correction_dpo_pairs: int
    min_reviewed_good: int
    min_holdout_sessions: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FineTuneThresholds":
        return cls(
            min_sft_turns=int(value["min_sft_turns"]),
            min_dpo_pairs=int(value["min_dpo_pairs"]),
            min_correction_dpo_pairs=int(value["min_correction_dpo_pairs"]),
            min_reviewed_good=int(value["min_reviewed_good"]),
            min_holdout_sessions=int(value["min_holdout_sessions"]),
        )


def assess_finetune_readiness(
    stats: ReadinessStats, thresholds: FineTuneThresholds, *, contract_id: str,
) -> dict[str, Any]:
    checks = {
        "baseline_versioned": stats.baseline_versioned,
        "sft_volume": stats.eligible_sft_turns >= thresholds.min_sft_turns,
        "dpo_volume": stats.eligible_dpo_pairs >= thresholds.min_dpo_pairs,
        "correction_dpo_volume": (
            stats.correction_dpo_pairs >= thresholds.min_correction_dpo_pairs
        ),
        "reviewed_good_volume": stats.reviewed_good_turns >= thresholds.min_reviewed_good,
        "holdout_sessions": stats.holdout_sessions >= thresholds.min_holdout_sessions,
        "schema_frozen": stats.schema_frozen,
        "persona_frozen": stats.persona_frozen,
        "context_frozen": stats.context_frozen,
        "agenda_frozen": stats.agenda_frozen,
    }
    return {
        "schema_version": 1,
        "contract_id": contract_id,
        "status": "ready" if all(checks.values()) else "not_ready",
        "checks": checks,
        "stats": asdict(stats),
        "thresholds": asdict(thresholds),
        "failed_checks": [key for key, passed in checks.items() if not passed],
        "allowed_next_stage": "sft_spike" if all(checks.values()) else "collect_and_review_data",
        "production_model_changed": False,
    }


@dataclass(frozen=True)
class CandidateMetrics:
    contract_id: str
    model_id: str
    persona_score: float
    continuity_score: float
    repetition_score: float
    safety_violations: int
    p95_latency_ms: float
    human_review_complete: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateMetrics":
        metrics = value.get("metrics") or value
        return cls(
            contract_id=str(value["contract_id"]),
            model_id=str(value["model_id"]),
            persona_score=float(metrics["persona_score"]),
            continuity_score=float(metrics["continuity_score"]),
            repetition_score=float(metrics["repetition_score"]),
            safety_violations=int(metrics["safety_violations"]),
            p95_latency_ms=float(metrics["p95_latency_ms"]),
            human_review_complete=bool(value.get("human_review_complete", False)),
        )


def compare_candidate(
    baseline: CandidateMetrics,
    candidate: CandidateMetrics,
    *,
    max_safety_regression: int,
    max_latency_increase_percent: float,
) -> dict[str, Any]:
    same_contract = candidate.contract_id == baseline.contract_id
    checks = {
        "same_contract": same_contract,
        "different_model": candidate.model_id != baseline.model_id,
        "human_review_complete": candidate.human_review_complete,
        "persona_improved": candidate.persona_score > baseline.persona_score,
        "continuity_improved": candidate.continuity_score > baseline.continuity_score,
        "repetition_improved": candidate.repetition_score > baseline.repetition_score,
        "safety_within_budget": (
            candidate.safety_violations
            <= baseline.safety_violations + int(max_safety_regression)
        ),
        "latency_within_budget": (
            candidate.p95_latency_ms
            <= baseline.p95_latency_ms * (1 + float(max_latency_increase_percent) / 100)
        ),
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "contract_id": baseline.contract_id,
        "baseline_model_id": baseline.model_id,
        "candidate_model_id": candidate.model_id,
        "status": "passed" if passed else "failed",
        "checks": checks,
        "promotion_allowed": passed,
        "automatic_promotion": False,
        "rollback_model_id": baseline.model_id,
    }


def build_candidate_manifest(
    *,
    candidate_id: str,
    candidate_path: str,
    baseline_model_id: str,
    contract_id: str,
    training_stage: str,
    sft_parent_id: str | None = None,
) -> dict[str, Any]:
    if training_stage not in {"sft", "dpo"}:
        raise ValueError("training stage must be sft or dpo")
    if training_stage == "dpo" and not sft_parent_id:
        raise ValueError("DPO candidate requires an SFT parent")
    if not all(item.strip() for item in (candidate_id, candidate_path, baseline_model_id, contract_id)):
        raise ValueError("candidate manifest identifiers are required")
    return {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_path": candidate_path,
        "baseline_model_id": baseline_model_id,
        "rollback_model_id": baseline_model_id,
        "contract_id": contract_id,
        "training_stage": training_stage,
        "sft_parent_id": sft_parent_id,
        "production_model_changed": False,
        "promotion_requires_manual_approval": True,
    }
