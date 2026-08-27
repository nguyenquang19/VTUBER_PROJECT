"""Bounded non-blocking scheduling and compatibility isolation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitiveCompatibilityObservation,
    CognitiveContextRequest,
    CognitiveHardState,
    CognitiveMode,
    CognitiveOpportunity,
    CognitiveOpportunityKind,
    CognitiveShadowOutcome,
    CognitiveTurn,
    CognitiveUncertainty,
)
from services.operations.metrics import MetricsCollector
from services.cognition.grounding_gate import CognitiveGroundingGate
from services.cognition.scheduler import CognitiveOpportunityScheduler
from tests.unit.test_cognitive_brain_shadow import _config, _context


NOW = datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc)


class _Builder:
    service_id = "builder"

    def __init__(self, context, gate: asyncio.Event | None = None) -> None:
        self.context = context
        self.gate = gate
        self.calls = 0
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, object]:
        return {}

    async def build(self, request):
        del request
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.context

    def recent(self, limit=None):
        del limit
        return ()

    def focus_snapshot(self):
        return None


class _Brain:
    service_id = "brain"

    def __init__(self, config, context, gate: asyncio.Event | None = None) -> None:
        self.config = config
        self.context = context
        self.gate = gate
        self.calls = 0
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, object]:
        return {}

    async def propose(self, context):
        assert context is self.context
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return CognitiveTurn(
            config=self.config, context=context, schema_version=1,
            turn_id=f"turn-{self.calls}", context_id=context.context_id,
            mode=CognitiveMode.WAIT, attention_target_id=None, intent=None,
            speech_text=None, action_proposal=None, focus_proposal=None,
            memory_proposals=(), evidence_refs=(),
            uncertainty=CognitiveUncertainty.UNKNOWN,
            reason_codes=("intentional_wait",),
        )


class _HighUncertaintySpeechBrain(_Brain):
    async def propose(self, context):
        assert context is self.context
        self.calls += 1
        return CognitiveTurn(
            config=self.config, context=context, schema_version=1,
            turn_id=f"unsafe-source-{self.calls}", context_id=context.context_id,
            mode=CognitiveMode.SPEAK, attention_target_id="agent:chat:m1",
            intent="answer the viewer", speech_text="Uncertain source wording.",
            action_proposal=None, focus_proposal=None, memory_proposals=(),
            evidence_refs=("agent:chat:m1",),
            uncertainty=CognitiveUncertainty.HIGH,
            reason_codes=("propose_speech",),
        )


class _FailingGroundingGate(CognitiveGroundingGate):
    def evaluate(self, context, turn):
        del context, turn
        raise RuntimeError("synthetic grounding failure")


def _opportunity(config, suffix: str, *, hold: bool = False) -> CognitiveOpportunity:
    hard = CognitiveHardState(
        config=config, schema_version=1, emergency=hold, operator_hold=False,
        safety_hold=False, permission_hold=False, transaction_conflict=False,
        critical_state=False, source_failure_codes=(),
    )
    compatibility = CognitiveCompatibilityObservation(
        config=config, schema_version=1, decision_ref=f"decision:{suffix}",
        mode=CognitiveMode.SPEAK, action_label="read_chat", reason_label="top_single",
    )
    request = CognitiveContextRequest(
        config=config, schema_version=1, request_id=f"request:{suffix}",
        session_id="stream:test", requested_at=NOW,
        trigger_event_ref="agent:chat:m1", hard_state=hard,
    )
    return CognitiveOpportunity(
        config=config, schema_version=1, opportunity_id=f"opportunity:{suffix}",
        kind=CognitiveOpportunityKind.CHAT_INPUT, opened_at=NOW,
        material_change_ref=f"material:{suffix}", context_request=request,
        compatibility=compatibility,
    )


async def _until(predicate, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_feature_off_creates_no_worker_context_or_brain_call() -> None:
    config = _config()
    context = _context(config)
    builder = _Builder(context)
    brain = _Brain(config, context)
    scheduler = CognitiveOpportunityScheduler(
        config=config, context_builder=builder, brain=brain,
        grounding_gate=CognitiveGroundingGate(config), clock=lambda: NOW,
    )
    assert scheduler.offer(_opportunity(config, "off")) is False
    assert builder.calls == 0 and brain.calls == 0
    assert scheduler.snapshot().running is False
    assert (await scheduler.health_check()).state.value == "stopped"
    assert scheduler.recent()[-1].outcome is CognitiveShadowOutcome.SKIPPED_DISABLED


@pytest.mark.asyncio
async def test_one_opportunity_produces_one_observer_record_and_bounded_metrics() -> None:
    config = _config()
    context = _context(config)
    metrics = MetricsCollector()
    builder = _Builder(context)
    brain = _Brain(config, context)
    scheduler = CognitiveOpportunityScheduler(
        config=config, context_builder=builder, brain=brain,
        grounding_gate=CognitiveGroundingGate(config, metrics=metrics),
        metrics=metrics, clock=lambda: NOW,
    )
    await scheduler.start()
    assert scheduler.offer(_opportunity(config, "one")) is True
    await _until(lambda: len(scheduler.recent()) == 1)
    record = scheduler.recent()[0]
    assert record.outcome is CognitiveShadowOutcome.PROPOSED
    assert record.grounding_decision is not None
    assert record.grounding_decision.outcome.value == "SUPPRESSED_WAIT"
    assert builder.calls == 1 and brain.calls == 1
    assert metrics.cognition_brain_snapshot()["turns"] == {"WAIT": 1}
    await scheduler.stop()
    assert scheduler.recent() == ()


@pytest.mark.asyncio
async def test_scheduler_retains_only_grounded_wait_for_unsafe_speech() -> None:
    config = _config()
    context = _context(config)
    builder = _Builder(context)
    brain = _HighUncertaintySpeechBrain(config, context)
    scheduler = CognitiveOpportunityScheduler(
        config=config, context_builder=builder, brain=brain,
        grounding_gate=CognitiveGroundingGate(config), clock=lambda: NOW,
    )
    await scheduler.start()
    assert scheduler.offer(_opportunity(config, "unsafe-speech")) is True
    await _until(lambda: len(scheduler.recent()) == 1)
    record = scheduler.recent()[0]
    assert record.outcome is CognitiveShadowOutcome.PROPOSED
    assert record.turn is not None and record.turn.mode is CognitiveMode.WAIT
    assert record.turn.speech_text is None and record.turn.intent is None
    assert record.grounding_decision is not None
    assert record.grounding_decision.source_turn_id == "unsafe-source-1"
    assert record.grounding_decision.source_mode is CognitiveMode.SPEAK
    assert record.grounding_decision.source_uncertainty is CognitiveUncertainty.HIGH
    assert record.grounding_decision.outcome.value == "SUPPRESSED_UNCERTAINTY"
    assert record.compatibility.mode is CognitiveMode.SPEAK
    await scheduler.stop()


@pytest.mark.asyncio
async def test_grounding_failure_retains_no_unsafe_cognition() -> None:
    config = _config()
    context = _context(config)
    scheduler = CognitiveOpportunityScheduler(
        config=config, context_builder=_Builder(context),
        brain=_HighUncertaintySpeechBrain(config, context),
        grounding_gate=_FailingGroundingGate(config), clock=lambda: NOW,
    )
    await scheduler.start()
    assert scheduler.offer(_opportunity(config, "gate-failure")) is True
    await _until(lambda: len(scheduler.recent()) == 1)
    record = scheduler.recent()[0]
    assert record.outcome is CognitiveShadowOutcome.SERVICE_ERROR
    assert record.turn is None and record.grounding_decision is None
    await scheduler.stop()


@pytest.mark.asyncio
async def test_hard_hold_and_same_material_never_call_brain_twice() -> None:
    config = _config()
    context = _context(config)
    builder = _Builder(context)
    brain = _Brain(config, context)
    scheduler = CognitiveOpportunityScheduler(
        config=config, context_builder=builder, brain=brain,
        grounding_gate=CognitiveGroundingGate(config), clock=lambda: NOW,
    )
    await scheduler.start()
    assert scheduler.offer(_opportunity(config, "hold", hold=True)) is False
    assert scheduler.offer(_opportunity(config, "same")) is True
    await _until(lambda: brain.calls == 1)
    assert scheduler.offer(_opportunity(config, "same")) is False
    assert brain.calls == 1
    assert {item.outcome for item in scheduler.recent()} >= {
        CognitiveShadowOutcome.SKIPPED_HARD_HOLD,
        CognitiveShadowOutcome.SKIPPED_NO_CHANGE,
    }
    await scheduler.stop()


@pytest.mark.asyncio
async def test_latest_wins_slot_supersedes_only_pending_work() -> None:
    config = _config()
    context = _context(config)
    gate = asyncio.Event()
    builder = _Builder(context, gate=gate)
    brain = _Brain(config, context)
    scheduler = CognitiveOpportunityScheduler(
        config=config, context_builder=builder, brain=brain,
        grounding_gate=CognitiveGroundingGate(config), clock=lambda: NOW,
    )
    await scheduler.start()
    scheduler.offer(_opportunity(config, "active"))
    await _until(lambda: builder.calls == 1)
    scheduler.offer(_opportunity(config, "old-pending"))
    scheduler.offer(_opportunity(config, "latest"))
    assert any(
        item.opportunity_id == "opportunity:old-pending"
        and item.outcome is CognitiveShadowOutcome.SUPERSEDED
        for item in scheduler.recent()
    )
    assert scheduler.snapshot().queue_depth == 1
    gate.set()
    await _until(lambda: brain.calls == 2)
    assert builder.calls == 2
    await scheduler.stop()


@pytest.mark.asyncio
async def test_live_preemption_cancels_shadow_without_creating_public_fallback() -> None:
    config = _config()
    context = _context(config)
    gate = asyncio.Event()
    builder = _Builder(context)
    brain = _Brain(config, context, gate=gate)
    scheduler = CognitiveOpportunityScheduler(
        config=config, context_builder=builder, brain=brain,
        grounding_gate=CognitiveGroundingGate(config), clock=lambda: NOW,
    )
    await scheduler.start()
    scheduler.offer(_opportunity(config, "preempt"))
    await _until(lambda: brain.calls == 1)
    scheduler.preempt_for_live()
    await _until(lambda: bool(scheduler.recent()))
    assert scheduler.recent()[-1].outcome is CognitiveShadowOutcome.PREEMPTED
    assert scheduler.snapshot().inflight_count == 0
    await scheduler.stop()
