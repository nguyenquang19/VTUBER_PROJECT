"""Deterministic single-owner and one-opportunity Turn Kernel behavior."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from interfaces.cognition import CognitiveHardState, CognitiveMode
from interfaces.state import AgentStateSnapshot, GoalSnapshot
from interfaces.turn_kernel import (
    KernelConfig,
    PublicTurnRoute,
    TurnOwner,
    TurnRolloutMode,
    TurnRouteOutcome,
)
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.director import DirectorAction, DirectorDecision, ReadMode
from services.kernel.turn_kernel import TurnKernel
from tests.unit.test_cognitive_brain_shadow import _config


ROOT = Path(__file__).resolve().parents[2]
NOW = 1_777_000_000.0


class _Scheduler:
    def __init__(self, events: list[str]) -> None:
        self.offers = []
        self.preemptions = 0
        self.events = events
        self.fail_offer = False
        self.shadow_gate: asyncio.Event | None = None
        self.shadow_task: asyncio.Task[bool] | None = None
        self.resolve_result = None
        self.resolve_calls = 0
        self.running = True

    def offer(self, value) -> bool:
        self.events.append("brain_offer")
        if self.fail_offer:
            raise RuntimeError("synthetic offer failure")
        self.offers.append(value)
        if self.shadow_gate is not None:
            self.shadow_task = asyncio.create_task(self._slow_shadow())
        return True

    async def _slow_shadow(self) -> bool:
        assert self.shadow_gate is not None
        await self.shadow_gate.wait()
        return True

    def preempt_for_live(self) -> None:
        self.preemptions += 1

    def snapshot(self):
        return SimpleNamespace(running=self.running, healthy=self.running)

    async def resolve_public(self, value):
        self.resolve_calls += 1
        return self.resolve_result


class _Filter:
    def __init__(self, *, passed: bool = True, reason: str = "") -> None:
        self.passed = passed
        self.reason = reason
        self.calls: list[str] = []

    async def check(self, text: str, context: dict[str, object]):
        assert context == {"source": "cognitive_brain_public"}
        self.calls.append(text)
        return SimpleNamespace(passed=self.passed, reason=self.reason)


class _Compatibility:
    def __init__(self, decision: DirectorDecision, events: list[str]) -> None:
        self.decision = decision
        self.events = events
        self.kernel: TurnKernel | None = None
        self.started = 0
        self.stopped = 0

    @property
    def turn_in_progress(self) -> bool:
        return False

    async def start_compatibility(self) -> None:
        self.started += 1

    async def stop_compatibility(self) -> None:
        self.stopped += 1

    async def tick_once(self) -> DirectorAction:
        assert self.kernel is not None
        value = _input()
        self.kernel.observe_decision(self.decision, value, "decision-1")
        self.events.append("public_execute")
        self.kernel.observe_verified_outcome(self.decision, value, "decision-1")
        return self.decision.action


def _input() -> DirectorInput:
    return DirectorInput(
        now=NOW, agent_state=AgentStateSnapshot(), goals=GoalSnapshot(),
    )


def _kernel_config(mode: str = "shadow") -> KernelConfig:
    raw = yaml.safe_load((ROOT / "config" / "kernel.yaml").read_text(encoding="utf-8"))
    raw["rollout_mode"] = mode
    return KernelConfig.from_mapping(raw)


def _hard_state(value: DirectorInput) -> CognitiveHardState:
    config = _config()
    return CognitiveHardState(
        config=config,
        schema_version=config.schema_version,
        emergency=False,
        operator_hold=False,
        safety_hold=value.safety_hold,
        permission_hold=False,
        transaction_conflict=False,
        critical_state=False,
        source_failure_codes=(),
    )


def _build(
    decision: DirectorDecision, *, mode: str = "shadow", output_filter=None,
    hard_state_provider=_hard_state,
) -> tuple[TurnKernel, _Compatibility, _Scheduler]:
    events: list[str] = []
    compatibility = _Compatibility(decision, events)
    scheduler = _Scheduler(events)
    kernel = TurnKernel(
        config=_kernel_config(mode),
        cognition_config=_config(),
        compatibility=compatibility,
        brain_scheduler=scheduler,
        hard_state_provider=hard_state_provider,
        output_filter=output_filter,
        session_id="session-real",
        clock=lambda: datetime.fromtimestamp(NOW, timezone.utc),
    )
    compatibility.kernel = kernel
    return kernel, compatibility, scheduler


def test_s4_rejects_unreleased_public_rollout_modes() -> None:
    for mode in ("canary", "primary", "released"):
        with pytest.raises(ValueError, match="runtime allows"):
            _kernel_config(mode)


def test_kernel_config_requires_the_exact_hard_reason_allowlist() -> None:
    raw = yaml.safe_load((ROOT / "config" / "kernel.yaml").read_text(encoding="utf-8"))
    raw["reason_codes"].remove("emergency")
    with pytest.raises(ValueError, match="hard-preflight allowlist"):
        KernelConfig.from_mapping(raw)


def test_public_route_rejects_owner_mode_outcome_or_provenance_mismatch() -> None:
    kwargs = {
        "config": _kernel_config("brain"),
        "cognition_config": _config(),
        "schema_version": 1,
        "opportunity_id": "opportunity:typed-route",
        "owner": TurnOwner.BRAIN,
        "outcome": TurnRouteOutcome.BRAIN_SPEAK,
        "mode": CognitiveMode.SPEAK,
        "speech_text": "Câu đã grounded.",
        "source_turn_id": "brain-turn:typed-route",
        "evidence_refs": ("agent:chat:typed-route",),
        "reason_code": "brain_speak",
    }
    assert PublicTurnRoute(**kwargs).owner is TurnOwner.BRAIN
    with pytest.raises(ValueError, match="outcome must match"):
        PublicTurnRoute(**{**kwargs, "outcome": TurnRouteOutcome.BRAIN_WAIT})
    with pytest.raises(ValueError, match="source_turn_id"):
        PublicTurnRoute(**{**kwargs, "source_turn_id": None})
    with pytest.raises(ValueError, match="evidence_refs"):
        PublicTurnRoute(**{**kwargs, "evidence_refs": ()})
    with pytest.raises(ValueError, match="compatibility outcome"):
        PublicTurnRoute(**{
            **kwargs,
            "owner": TurnOwner.COMPATIBILITY,
            "speech_text": None,
            "source_turn_id": None,
            "evidence_refs": (),
        })


@pytest.mark.asyncio
async def test_idle_heartbeat_never_becomes_a_brain_opportunity() -> None:
    decision = DirectorDecision(DirectorAction.WAIT, "main", "idle")
    kernel, _, scheduler = _build(decision)
    for _ in range(20):
        assert await kernel.tick_once() is DirectorAction.WAIT
    assert scheduler.offers == []
    assert kernel.recent_selections() == ()


@pytest.mark.asyncio
async def test_one_public_turn_opens_one_selection_and_one_brain_opportunity() -> None:
    chat = DirectorChatRef(
        msg_id="input-1", text="Mai ơi", kind="chat", score=20,
        created_at=NOW, is_super=True,
    )
    decision = DirectorDecision(
        DirectorAction.ACK_DONATION, "main", "superchat_priority",
        refs=(chat,), read_mode=ReadMode.ACK,
    )
    kernel, _, scheduler = _build(decision)
    assert await kernel.tick_once() is DirectorAction.ACK_DONATION
    assert len(scheduler.offers) == 1
    opportunity = scheduler.offers[0]
    assert opportunity.kind.value == "DONATION_OR_OPERATOR"
    assert opportunity.material_change_ref == "agent:chat:input-1"
    assert opportunity.context_request.trigger_event_ref == "agent:chat:input-1"
    assert opportunity.context_request.session_id == "session-real"
    assert opportunity.compatibility.decision_ref == "decision-1"
    assert scheduler.events == ["brain_offer", "public_execute"]
    selection = kernel.recent_selections()[0]
    assert selection.owner is TurnOwner.COMPATIBILITY
    assert selection.rollout_mode is TurnRolloutMode.SHADOW


@pytest.mark.asyncio
async def test_off_keeps_public_compatibility_and_does_not_offer_brain_work() -> None:
    chat = DirectorChatRef(
        msg_id="input-2", text="Mai ơi", kind="chat", score=20,
        created_at=NOW,
    )
    decision = DirectorDecision(
        DirectorAction.READ_CHAT, "main", "chat", refs=(chat,),
    )
    kernel, _, scheduler = _build(decision, mode="off")
    assert await kernel.tick_once() is DirectorAction.READ_CHAT
    assert scheduler.offers == []
    assert scheduler.events == ["public_execute"]
    assert kernel.recent_selections()[0].rollout_mode is TurnRolloutMode.OFF


def test_input_activity_preempts_only_subordinate_brain_work() -> None:
    kernel, _, scheduler = _build(
        DirectorDecision(DirectorAction.WAIT, "main", "idle"),
    )
    kernel.notify_input_activity()
    assert scheduler.preemptions == 1


@pytest.mark.asyncio
async def test_shadow_offer_failure_cannot_change_public_result() -> None:
    chat = DirectorChatRef(
        msg_id="input-offer-fail", text="Mai ơi", kind="chat", score=20,
        created_at=NOW,
    )
    decision = DirectorDecision(
        DirectorAction.READ_CHAT, "main", "chat", refs=(chat,),
    )
    kernel, _, scheduler = _build(decision)
    scheduler.fail_offer = True
    assert await kernel.tick_once() is DirectorAction.READ_CHAT
    assert scheduler.events == ["brain_offer", "public_execute"]
    assert kernel.recent_selections()[0].owner is TurnOwner.COMPATIBILITY


@pytest.mark.asyncio
async def test_slow_shadow_work_is_never_awaited_by_public_turn() -> None:
    chat = DirectorChatRef(
        msg_id="input-slow-shadow", text="Mai ơi", kind="chat", score=20,
        created_at=NOW,
    )
    decision = DirectorDecision(
        DirectorAction.READ_CHAT, "main", "chat", refs=(chat,),
    )
    kernel, _, scheduler = _build(decision)
    scheduler.shadow_gate = asyncio.Event()
    assert await kernel.tick_once() is DirectorAction.READ_CHAT
    assert scheduler.shadow_task is not None
    assert scheduler.shadow_task.done() is False
    scheduler.shadow_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scheduler.shadow_task


def test_kernel_config_and_selection_values_are_immutable() -> None:
    config = _kernel_config()
    with pytest.raises(Exception):
        config.rollout_mode = TurnRolloutMode.OFF  # type: ignore[misc]
    assert replace(config, rollout_mode=TurnRolloutMode.OFF).rollout_mode is TurnRolloutMode.OFF


def _grounded(*, mode: CognitiveMode, speech: str | None = None, source_mode=None):
    effective = SimpleNamespace(
        mode=mode,
        speech_text=speech,
        evidence_refs=("agent:chat:canary",) if speech else (),
    )
    return SimpleNamespace(
        effective_turn=effective,
        source_turn_id="brain-turn:canary",
        source_mode=source_mode or mode,
    )


def _canary_decision(*, owner: bool = True, moderator: bool = False) -> DirectorDecision:
    ref = DirectorChatRef(
        msg_id="canary", text="Mai ơi", kind="chat", score=20,
        created_at=NOW, is_owner=owner, is_moderator=moderator,
    )
    return DirectorDecision(
        DirectorAction.READ_CHAT, "main", "chat", refs=(ref,),
    )


@pytest.mark.asyncio
async def test_public_brain_routes_only_configured_canary_role_after_flag() -> None:
    output_filter = _Filter()
    decision = _canary_decision()
    kernel, _, scheduler = _build(
        decision, mode="brain", output_filter=output_filter,
    )
    scheduler.resolve_result = _grounded(
        mode=CognitiveMode.SPEAK, speech="Chào cậu nhé.",
    )
    kernel.set_public_brain_enabled(True)
    route = await kernel.route_decision(decision, _input(), "decision-public")
    assert route is not None
    assert route.owner is TurnOwner.BRAIN
    assert route.outcome is TurnRouteOutcome.BRAIN_SPEAK
    assert route.speech_text == "Chào cậu nhé."
    assert scheduler.resolve_calls == 1
    assert output_filter.calls == ["Chào cậu nhé."]
    assert kernel.recent_selections()[-1].owner is TurnOwner.BRAIN

    regular = _canary_decision(owner=False)
    regular_route = await kernel.route_decision(regular, _input(), "decision-regular")
    assert regular_route is not None
    assert regular_route.owner is TurnOwner.COMPATIBILITY
    assert regular_route.reason_code == "compatibility_outside_canary"
    assert scheduler.resolve_calls == 1
    assert len(scheduler.offers) == 1


@pytest.mark.asyncio
async def test_public_brain_route_replay_is_deterministic() -> None:
    decision = _canary_decision(moderator=True)
    routes = []
    for _ in range(2):
        kernel, _, scheduler = _build(
            decision, mode="brain", output_filter=_Filter(),
        )
        scheduler.resolve_result = _grounded(
            mode=CognitiveMode.SPEAK, speech="Cùng một câu grounded.",
        )
        kernel.set_public_brain_enabled(True)
        routes.append(
            await kernel.route_decision(decision, _input(), "decision-replay")
        )

    assert routes[0] == routes[1]


@pytest.mark.asyncio
async def test_public_brain_wait_and_action_are_fail_closed_without_fallback() -> None:
    decision = _canary_decision()
    kernel, _, scheduler = _build(decision, mode="brain", output_filter=_Filter())
    kernel.set_public_brain_enabled(True)
    scheduler.resolve_result = _grounded(mode=CognitiveMode.WAIT)
    route = await kernel.route_decision(decision, _input(), "decision-wait")
    assert route is not None
    assert route.owner is TurnOwner.BRAIN
    assert route.outcome is TurnRouteOutcome.BRAIN_WAIT
    assert route.speech_text is None

    scheduler.resolve_result = _grounded(
        mode=CognitiveMode.WAIT, source_mode=CognitiveMode.PROPOSE_ACTION,
    )
    route = await kernel.route_decision(decision, _input(), "decision-action")
    assert route is not None
    assert route.owner is TurnOwner.BRAIN
    assert route.reason_code == "brain_action_suppressed"


@pytest.mark.asyncio
async def test_public_brain_failures_and_filter_rejection_fallback() -> None:
    decision = _canary_decision()
    output_filter = _Filter(passed=False, reason="unsafe")
    kernel, _, scheduler = _build(
        decision, mode="brain", output_filter=output_filter,
    )
    kernel.set_public_brain_enabled(True)
    scheduler.resolve_result = _grounded(
        mode=CognitiveMode.SPEAK, speech="unsafe output",
    )
    route = await kernel.route_decision(decision, _input(), "decision-filter")
    assert route is not None
    assert route.owner is TurnOwner.COMPATIBILITY
    assert route.outcome is TurnRouteOutcome.FALLBACK
    assert route.reason_code == "fallback_filter_reject"

    scheduler.resolve_result = None
    route = await kernel.route_decision(decision, _input(), "decision-timeout")
    assert route is not None
    assert route.owner is TurnOwner.COMPATIBILITY
    assert route.reason_code == "fallback_brain"


@pytest.mark.asyncio
async def test_hard_preflight_and_flag_off_never_call_public_brain() -> None:
    decision = _canary_decision()

    def hard(value: DirectorInput) -> CognitiveHardState:
        config = _config()
        return CognitiveHardState(
            config=config,
            schema_version=config.schema_version,
            emergency=False,
            operator_hold=False,
            safety_hold=True,
            permission_hold=False,
            transaction_conflict=False,
            critical_state=False,
            source_failure_codes=(),
        )

    kernel, _, scheduler = _build(
        decision, mode="brain", output_filter=_Filter(), hard_state_provider=hard,
    )
    kernel.set_public_brain_enabled(True)
    route = await kernel.route_decision(decision, _input(), "decision-hold")
    assert route is not None
    assert route.owner is TurnOwner.COMPATIBILITY
    assert route.reason_code == "compatibility_hard_hold"
    assert route.mode is CognitiveMode.WAIT
    assert scheduler.resolve_calls == 0
    assert scheduler.offers == []

    kernel, _, scheduler = _build(decision, mode="brain", output_filter=_Filter())
    route = await kernel.route_decision(decision, _input(), "decision-flag-off")
    assert route is not None
    assert route.reason_code == "compatibility_flag_off"
    assert scheduler.resolve_calls == 0
    assert len(scheduler.offers) == 1


def test_public_flag_requires_brain_mode_healthy_scheduler_and_filter() -> None:
    decision = _canary_decision()
    kernel, _, _ = _build(decision, mode="shadow", output_filter=_Filter())
    with pytest.raises(RuntimeError, match="rollout mode BRAIN"):
        kernel.set_public_brain_enabled(True)

    kernel, _, scheduler = _build(decision, mode="brain", output_filter=_Filter())
    scheduler.running = False
    with pytest.raises(RuntimeError, match="healthy"):
        kernel.set_public_brain_enabled(True)

    kernel, _, scheduler = _build(decision, mode="brain")
    with pytest.raises(RuntimeError, match="output filter"):
        kernel.set_public_brain_enabled(True)
