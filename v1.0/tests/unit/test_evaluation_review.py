from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.evaluation.harness import ScenarioEvaluationHarness
from services.evaluation.review import build_live_artifact, finalize_human_review
from services.evaluation.scenario_loader import load_scenario_suite
from services.evaluation.types import ObservedOutcome


ROOT = Path(__file__).resolve().parents[2]


def _continuity_artifact() -> dict:
    suite = load_scenario_suite(ROOT / "eval" / "scenarios" / "mai_agent_v1.yaml")
    harness = ScenarioEvaluationHarness(suite)
    result = harness.evaluate(ObservedOutcome(
        scenario_id="continuity.promise_evidence",
        action="continue_thread",
        invariants={"invented_event_ids": 0},
        source_refs=("session:private@example.com:turn:3",),
    ))
    return build_live_artifact(suite, (result,), run_id="test-run")


def test_live_artifact_hashes_evidence_and_waits_for_human() -> None:
    artifact = _continuity_artifact()
    encoded = json.dumps(artifact)
    assert artifact["status"] == "pending_human_review"
    assert artifact["raw_model_output_included"] is False
    assert "private@example.com" not in encoded
    assert artifact["results"][0]["source_refs"][0].startswith("evidence:")


def test_human_review_is_explicit_and_sanitized() -> None:
    artifact = _continuity_artifact()
    artifact["human_review"]["reviewer_role"] = "operator"
    rubric = artifact["results"][0]["human_rubric"][0]
    rubric["score"] = 5
    rubric["note"] = "Checked private@example.com against evidence."
    reviewed = finalize_human_review(artifact)
    assert reviewed["status"] == "passed"
    assert reviewed["human_review"]["complete"] is True
    assert "private@example.com" not in reviewed["results"][0]["human_rubric"][0]["note"]


def test_missing_human_score_never_auto_passes() -> None:
    artifact = _continuity_artifact()
    artifact["human_review"]["reviewer_role"] = "operator"
    with pytest.raises(ValueError, match="rubric score"):
        finalize_human_review(artifact)
