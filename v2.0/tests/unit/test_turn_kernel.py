"""Deterministic single-owner and one-opportunity Turn Kernel behavior."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from interfaces.cognition import CognitiveHardState
from interfaces.state import AgentStateSnapshot, GoalSnapshot
from interfaces.turn_kernel import KernelConfig, TurnOwner, TurnRolloutMode
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
    decision: DirectorDecision, *, mode: str = "shadow",
) -> tuple[TurnKernel, _Compatibility, _Scheduler]:
    events: list[str] = []
    compatibility = _Compatibility(decision, events)
    scheduler = _Scheduler(events)
    kernel = TurnKernel(
        config=_kernel_config(mode),
        cognition_config=_config(),
        compatibility=compatibility,
        brain_scheduler=scheduler,
        hard_state_provider=_hard_state,
        session_id="session-real",
        clock=lambda: datetime.fromtimestamp(NOW, timezone.utc),
    )
    compatibility.kernel = kernel
    return kernel, compatibility, scheduler


def test_s4_rejects_unreleased_public_rollout_modes() -> None:
    for mode in ("canary", "primary", "released"):
        with pytest.raises(ValueError, match="S4 runtime"):
            _kernel_config(mode)


def test_kernel_config_requires_the_exact_hard_reason_allowlist() -> None:
    raw = yaml.safe_load((ROOT / "config" / "kernel.yaml").read_text(encoding="utf-8"))
    raw["reason_codes"].remove("emergency")
    with pytest.raises(ValueError, match="hard-preflight allowlist"):
        KernelConfig.from_mapping(raw)


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
