from __future__ import annotations

import json
from pathlib import Path

from scripts.check_finetune_readiness import main as readiness_main
from scripts.compare_model_candidate import main as compare_main


def test_readiness_cli_returns_not_ready_without_side_effect(tmp_path: Path) -> None:
    stats = tmp_path / "stats.json"
    output = tmp_path / "result.json"
    stats.write_text(json.dumps({"stats": {
        "baseline_versioned": True,
        "eligible_sft_turns": 0,
        "eligible_dpo_pairs": 0,
        "correction_dpo_pairs": 0,
        "reviewed_good_turns": 0,
        "holdout_sessions": 0,
        "schema_frozen": True,
        "persona_frozen": True,
        "context_frozen": True,
        "agenda_frozen": True,
    }}), encoding="utf-8")
    assert readiness_main(["--stats", str(stats), "--output", str(output)]) == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "not_ready"
    assert result["production_model_changed"] is False


def test_candidate_cli_does_not_auto_promote(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps({
        "contract_id": "mai-agent-v1", "model_id": "base",
        "human_review_complete": True,
        "metrics": {
            "persona_score": 3, "continuity_score": 3, "repetition_score": 3,
            "safety_violations": 0, "p95_latency_ms": 1000,
        },
    }), encoding="utf-8")
    candidate.write_text(json.dumps({
        "contract_id": "mai-agent-v1", "model_id": "candidate",
        "human_review_complete": True,
        "metrics": {
            "persona_score": 4, "continuity_score": 4, "repetition_score": 4,
            "safety_violations": 0, "p95_latency_ms": 1050,
        },
    }), encoding="utf-8")
    assert compare_main(["--baseline", str(baseline), "--candidate", str(candidate)]) == 0
