from __future__ import annotations

from pathlib import Path

from services.evaluation.harness import ScenarioEvaluationHarness
from services.evaluation.scenario_loader import load_scenario_suite
from interfaces.evaluation import EvalOutcome, ObservedOutcome


ROOT = Path(__file__).resolve().parents[2]


def harness() -> ScenarioEvaluationHarness:
    return ScenarioEvaluationHarness(
        load_scenario_suite(ROOT / "eval" / "scenarios" / "mai_agent_v1.yaml")
    )


def test_matching_sourced_observation_passes() -> None:
    result = harness().evaluate(ObservedOutcome(
        scenario_id="agency.goal_expiry", state="expired",
        invariants={"resurrected": False}, source_refs=("simulation:m3:goal-1",),
    ))
    assert result.outcome is EvalOutcome.PASSED


def test_mismatch_fails_without_asserting_exact_text() -> None:
    result = harness().evaluate(ObservedOutcome(
        scenario_id="continuity.ambiguous_reference", action="continue_thread",
        invariants={"named_unverified_viewer": True}, source_refs=("event:e1",),
    ))
    assert result.outcome is EvalOutcome.FAILED
    assert result.checks == {
        "action": False, "invariant:named_unverified_viewer": False,
    }


def test_missing_source_is_not_counted_as_pass() -> None:
    result = harness().evaluate(ObservedOutcome(
        scenario_id="safety.jailbreak_deflect", action="deflect",
        invariants={"unsafe_tool_calls": 0},
    ))
    assert result.outcome is EvalOutcome.NOT_OBSERVED
    assert result.reason == "missing_source_refs"


def test_human_rubric_is_reported_but_not_auto_scored() -> None:
    result = harness().evaluate(ObservedOutcome(
        scenario_id="persona.gentle_override", action="acknowledge",
        invariants={"tease_selected": False}, source_refs=("event:e1",),
    ))
    assert result.outcome is EvalOutcome.PASSED
    assert result.human_review_required is True
