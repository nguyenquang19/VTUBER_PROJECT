from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from orchestrator.metrics_collector import MetricsCollector
from scripts.run_mood_ab_review import main
from services.evaluation.mood_ab import MoodABReview


ROOT = Path(__file__).resolve().parents[2]


def _service(*, min_turns: int = 3, metrics=None) -> MoodABReview:
    return MoodABReview(
        seed=17, min_turns=min_turns, min_appropriate_ratio=0.8, metrics=metrics,
    )


def _comparisons(count: int) -> tuple[dict, ...]:
    return tuple({
        "turn_ref": f"turn-{index}", "event_category": "chat_compliment",
        "input": f"input {index}", "context": "public stream context",
        "v1_output": f"legacy output {index}",
        "v2_output": f"v2 output {index}",
    } for index in range(count))


def _complete(artifact: dict, *, appropriate: bool = True) -> dict:
    artifact["human_review"] = {"reviewer_role": "operator", "complete": True}
    for row in artifact["rows"]:
        preferred = "A" if "v2 output" in row["candidate_a"] else "B"
        row["review"] = {
            "emotional_appropriate": appropriate,
            "persona": 4,
            "naturalness": 4,
            "overacting": False,
            "preferred": preferred,
            "note": "appropriate grounded comparison",
        }
    return artifact


def test_blind_sheet_is_deterministic_sanitized_and_never_auto_passes() -> None:
    service = _service()
    first = service.build(_comparisons(3))
    second = _service().build(_comparisons(3))
    assert first == second
    assert first["status"] == "pending_human_review"
    assert "passed" not in first
    assert first["raw_transcript_included"] is False
    assert first["same_input_context_per_pair"] is True
    assert first["rows"][0]["input"] == "input 0"
    assert all(row["turn_ref"].startswith("turn:") for row in first["rows"])


def test_internal_category_is_preserved_without_weakening_text_pii_masking() -> None:
    comparisons = list(_comparisons(3))
    comparisons[0]["event_category"] = "chat_genuine_sad_share"
    comparisons[0]["context"] = "contact private@example.com"
    artifact = _service().build(tuple(comparisons))
    assert artifact["rows"][0]["event_category"] == "chat_genuine_sad_share"
    assert "private@example.com" not in artifact["rows"][0]["context"]


def test_finalize_requires_minimum_turns_and_human_fields() -> None:
    artifact = _service(min_turns=3).build(_comparisons(2))
    with pytest.raises(ValueError, match="at least 3"):
        _service(min_turns=3).finalize(_complete(artifact))


def test_v2_equal_or_better_and_eighty_percent_gate_recommends_cutover() -> None:
    metrics = MetricsCollector()
    service = _service(min_turns=5, metrics=metrics)
    artifact = _complete(service.build(_comparisons(5)))
    final = service.finalize(artifact)
    assert final["passed"] is True
    assert final["cutover_recommended"] is True
    assert final["human_review"]["v2_wins"] == 5
    assert metrics.mood_ab_review_snapshot() == {
        "passed": 1, "pending_human_review": 1,
    }


def test_human_gate_fails_when_appropriateness_is_below_threshold() -> None:
    service = _service(min_turns=5)
    artifact = _complete(service.build(_comparisons(5)))
    artifact["rows"][0]["review"]["emotional_appropriate"] = False
    artifact["rows"][1]["review"]["emotional_appropriate"] = False
    final = service.finalize(artifact)
    assert final["passed"] is False
    assert final["human_review"]["appropriate_ratio"] == 0.6


def test_cli_builds_pending_artifact_and_masks_pii(tmp_path: Path, capsys) -> None:
    source = tmp_path / "comparisons.json"
    output = tmp_path / "review.json"
    rows = list(_comparisons(25))
    rows[0]["v2_output"] = "contact private@example.com"
    rows[0]["context"] = "email private@example.com"
    source.write_text(json.dumps({"comparisons": rows}), encoding="utf-8")
    assert main(["--input", str(source), "--output", str(output)]) == 1
    capsys.readouterr()
    rendered = output.read_text(encoding="utf-8")
    assert "private@example.com" not in rendered
    assert json.loads(rendered)["turn_count"] == 25


def test_failed_human_gate_artifact_never_claims_cutover() -> None:
    status = json.loads(
        (ROOT / "docs" / "baselines" / "m10_mood_v2_shadow_status.json").read_text(
            encoding="utf-8",
        )
    )
    assert status["human_ab_gate"]["cutover_approved"] is False
    assert status["human_ab_gate"]["status"] == "failed"
    assert status["human_ab_gate"]["appropriate_ratio"] < 0.8
    cutover = json.loads(
        (ROOT / "docs" / "baselines" / "m10_hybrid_cutover.json").read_text(
            encoding="utf-8",
        )
    )
    assert cutover["approval_source"] == "operator_explicit_full_live_confirmation"
    assert cutover["formal_25_case_gate"] == "waived_by_operator"
