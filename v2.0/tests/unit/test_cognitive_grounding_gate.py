"""Deterministic fail-closed grounding behavior for Brain proposals."""
from __future__ import annotations

from dataclasses import replace

import pytest

from interfaces.cognition import (
    CognitiveGroundingOutcome,
    CognitiveMode,
    CognitiveTurn,
    CognitiveUncertainty,
)
from services.cognition.grounding_gate import CognitiveGroundingGate
from services.operations.metrics import MetricsCollector
from tests.unit.test_cognitive_brain_shadow import _config, _context


def _turn(
    context,
    *,
    config=None,
    mode: CognitiveMode = CognitiveMode.SPEAK,
    uncertainty: CognitiveUncertainty = CognitiveUncertainty.LOW,
    evidence_refs: tuple[str, ...] = ("agent:chat:m1",),
) -> CognitiveTurn:
    contract = config or _config()
    waiting = mode is CognitiveMode.WAIT
    return CognitiveTurn(
        config=contract,
        context=context,
        schema_version=context.schema_version,
        turn_id=f"source-turn:{mode.value}:{uncertainty.value}:{len(evidence_refs)}",
        context_id=context.context_id,
        mode=mode,
        attention_target_id=None if waiting else "agent:chat:m1",
        intent=None if waiting else "answer grounded chat",
        speech_text=None if waiting else "Tớ đang trả lời theo điều vừa quan sát.",
        action_proposal=None,
        focus_proposal=None,
        memory_proposals=(),
        evidence_refs=evidence_refs,
        uncertainty=uncertainty,
        reason_codes=("intentional_wait" if waiting else "propose_speech",),
    )


@pytest.mark.asyncio
async def test_low_and_medium_grounded_speech_pass_unchanged() -> None:
    config = _config()
    context = _context(config)
    gate = CognitiveGroundingGate(config)
    await gate.start()
    for uncertainty in (CognitiveUncertainty.LOW, CognitiveUncertainty.MEDIUM):
        source = _turn(context, uncertainty=uncertainty)
        decision = gate.evaluate(context, source)
        assert decision.outcome is CognitiveGroundingOutcome.PASSED
        assert decision.effective_turn is source
        assert decision.source_turn_id == source.turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uncertainty", [CognitiveUncertainty.HIGH, CognitiveUncertainty.UNKNOWN],
)
async def test_above_yaml_uncertainty_threshold_becomes_wait(
    uncertainty: CognitiveUncertainty,
) -> None:
    config = _config()
    context = _context(config)
    gate = CognitiveGroundingGate(config)
    await gate.start()
    source = _turn(context, uncertainty=uncertainty)
    decision = gate.evaluate(context, source)
    effective = decision.effective_turn
    assert decision.outcome is CognitiveGroundingOutcome.SUPPRESSED_UNCERTAINTY
    assert decision.source_mode is CognitiveMode.SPEAK
    assert decision.source_uncertainty is uncertainty
    assert effective.mode is CognitiveMode.WAIT
    assert effective.speech_text is None and effective.intent is None
    assert effective.evidence_refs == ()
    assert effective.reason_codes == ("insufficient_evidence",)


@pytest.mark.asyncio
async def test_empty_and_non_current_evidence_become_wait() -> None:
    config = _config()
    original = _context(config)
    gate = CognitiveGroundingGate(config)
    await gate.start()

    empty = gate.evaluate(original, _turn(original, evidence_refs=()))
    assert empty.outcome is CognitiveGroundingOutcome.SUPPRESSED_EMPTY_EVIDENCE
    assert empty.effective_turn.mode is CognitiveMode.WAIT

    conversation = replace(
        original.conversation_state,
        config=config,
        evidence_refs=(),
    )
    same_identity_without_evidence = replace(
        original,
        config=config,
        chat_digest=None,
        conversation_state=conversation,
    )
    source = _turn(original)
    unknown = gate.evaluate(same_identity_without_evidence, source)
    assert unknown.outcome is CognitiveGroundingOutcome.SUPPRESSED_UNKNOWN_EVIDENCE
    assert unknown.effective_turn.mode is CognitiveMode.WAIT


@pytest.mark.asyncio
async def test_wait_stays_wait_and_is_classified_as_suppressed() -> None:
    config = _config()
    context = _context(config)
    source = _turn(
        context,
        mode=CognitiveMode.WAIT,
        uncertainty=CognitiveUncertainty.UNKNOWN,
        evidence_refs=(),
    )
    gate = CognitiveGroundingGate(config)
    await gate.start()
    decision = gate.evaluate(context, source)
    assert decision.outcome is CognitiveGroundingOutcome.SUPPRESSED_WAIT
    assert decision.effective_turn is source


@pytest.mark.asyncio
async def test_same_input_replays_same_effective_wait_identity() -> None:
    config = _config()
    context = _context(config)
    source = _turn(context, uncertainty=CognitiveUncertainty.HIGH)
    first_gate = CognitiveGroundingGate(config)
    second_gate = CognitiveGroundingGate(config)
    await first_gate.start()
    await second_gate.start()
    assert first_gate.evaluate(context, source) == second_gate.evaluate(context, source)


@pytest.mark.asyncio
async def test_yaml_threshold_changes_policy_without_code_change() -> None:
    base = _config()
    config = replace(
        base,
        grounding_uncertainty_threshold=CognitiveUncertainty.LOW,
    )
    context = _context(config)
    gate = CognitiveGroundingGate(config)
    await gate.start()
    decision = gate.evaluate(
        context, _turn(
            context, config=config, uncertainty=CognitiveUncertainty.MEDIUM,
        ),
    )
    assert decision.outcome is CognitiveGroundingOutcome.SUPPRESSED_UNCERTAINTY


@pytest.mark.asyncio
async def test_gate_metrics_are_bounded_and_stopped_gate_fails_closed() -> None:
    config = _config()
    context = _context(config)
    metrics = MetricsCollector()
    gate = CognitiveGroundingGate(config, metrics=metrics)
    with pytest.raises(RuntimeError, match="not running"):
        gate.evaluate(context, _turn(context))
    await gate.start()
    gate.evaluate(context, _turn(context))
    snapshot = gate.get_metrics()
    assert snapshot["cognitive_grounding_gate_evaluated_total"] == 1
    assert snapshot["cognitive_grounding_gate_failures_total"] == 1
    assert snapshot["cognitive_grounding_gate_pass_rate"] == 1.0
    assert metrics.cognition_grounding_snapshot() == {"FAILURE": 1, "PASSED": 1}
    with pytest.raises(ValueError, match="unsupported"):
        metrics.record_cognitive_grounding_decision("viewer-specific-label")
