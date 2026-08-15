from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> dict:
    return json.loads((ROOT / "docs" / "baselines" / name).read_text(encoding="utf-8"))


def test_baseline_is_versioned_without_faking_live_or_human_results() -> None:
    baseline = _read("m8_evaluation_baseline.json")
    assert baseline["contract_id"] == "mai-agent-v1"
    assert baseline["scenario_suite"]["scenario_count"] == 12
    assert baseline["scenario_suite"]["live_observation_status"] == "not_run"
    assert baseline["scenario_suite"]["human_review_status"] == "pending"
    assert baseline["candidate_evaluation"]["production_model_changed"] is False


def test_m8_readiness_truthfully_blocks_finetune() -> None:
    readiness = _read("m8_data_readiness.json")
    assert readiness["status"] == "not_ready"
    assert readiness["stats"]["eligible_sft_turns"] == 0
    assert readiness["stats"]["eligible_dpo_pairs"] == 0
    assert readiness["allowed_next_stage"] == "collect_and_review_data"
    assert readiness["production_model_changed"] is False
