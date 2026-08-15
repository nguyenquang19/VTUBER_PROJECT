"""Test ConversationStateMachine (ARCHITECTURE 7.10, test spec 12.6).

Bao gồm property-based test với hypothesis:
- Random sequence of triggers → state luôn valid
- Emergency stop từ mọi state → PAUSED
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from orchestrator.config_loader import ConfigLoader
from orchestrator.event_bus import EventBus
from orchestrator.state_machine import (
    ACTION_NAMES,
    ConversationState,
    ConversationStateMachine,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 9 trigger name khớp bảng 7.10.2
ALL_TRIGGERS = (
    "trigger_received",
    "first_token",
    "llm_fail",
    "tts_complete",
    "interrupted",
    "cooldown_elapsed",
    "queued_trigger_pending",
    "emergency_stop",
    "resume",
)


def make_sm(**kw) -> ConversationStateMachine:
    """State machine với auto_cooldown off — test tự điều khiển timing."""
    kw.setdefault("auto_cooldown", False)
    return ConversationStateMachine(**kw)


async def fire(sm: ConversationStateMachine, trigger: str) -> bool:
    """Gọi trigger, trả False nếu transition không hợp lệ ở state hiện tại."""
    from transitions.core import MachineError

    try:
        return bool(await getattr(sm, trigger)())
    except (MachineError, AttributeError):
        return False


class TestStructure:
    def test_exactly_five_states(self) -> None:
        """N1 YAGNI: đúng 5 state (7.10.1), không LISTENING/INTERRUPTED/ERROR."""
        assert len(ConversationStateMachine.states) == 5
        assert set(ConversationStateMachine.states) == {
            "IDLE", "THINKING", "SPEAKING", "COOLDOWN", "PAUSED"
        }

    def test_exactly_nine_transitions(self) -> None:
        """N1 YAGNI: đúng 9 transition (7.10.2), không phải 18."""
        assert len(ConversationStateMachine._get_transitions()) == 9

    def test_transition_table_matches_spec(self) -> None:
        """Đối chiếu từng dòng với bảng 7.10.2."""
        table = {
            (t["trigger"], t["source"]): t["dest"]
            for t in ConversationStateMachine._get_transitions()
        }
        assert table[("trigger_received", "IDLE")] == "THINKING"
        assert table[("first_token", "THINKING")] == "SPEAKING"
        assert table[("llm_fail", "THINKING")] == "COOLDOWN"
        assert table[("tts_complete", "SPEAKING")] == "COOLDOWN"
        assert table[("interrupted", "SPEAKING")] == "COOLDOWN"
        assert table[("cooldown_elapsed", "COOLDOWN")] == "IDLE"
        assert table[("queued_trigger_pending", "COOLDOWN")] == "THINKING"
        assert table[("emergency_stop", "*")] == "PAUSED"
        assert table[("resume", "PAUSED")] == "IDLE"

    def test_starts_in_idle(self) -> None:
        assert make_sm().current_state is ConversationState.IDLE

    def test_no_auto_transitions_generated(self) -> None:
        """auto_transitions=False → không có to_PAUSED() ngoài 9 transition (N1)."""
        sm = make_sm()
        assert not hasattr(sm, "to_PAUSED")
        assert not hasattr(sm, "to_IDLE")

    def test_invalid_initial_state_raises(self) -> None:
        with pytest.raises(ValueError, match="initial_state không hợp lệ"):
            make_sm(initial_state="LISTENING")


class TestNormalFlow:
    async def test_happy_path(self) -> None:
        """Scenario normal_flow (12.6): IDLE→THINKING→SPEAKING→COOLDOWN→IDLE."""
        sm = make_sm()
        seen = [sm.state]
        for trigger in ("trigger_received", "first_token", "tts_complete", "cooldown_elapsed"):
            assert await fire(sm, trigger) is True
            seen.append(sm.state)
        assert seen == ["IDLE", "THINKING", "SPEAKING", "COOLDOWN", "IDLE"]

    async def test_llm_fail_skips_speaking(self) -> None:
        sm = make_sm()
        await fire(sm, "trigger_received")
        assert await fire(sm, "llm_fail") is True
        assert sm.current_state is ConversationState.COOLDOWN

    async def test_cooldown_to_thinking_when_queue_pending(self) -> None:
        sm = make_sm()
        sm.set_queue_predicate(lambda: asyncio.sleep(0, result=True))
        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        await fire(sm, "tts_complete")
        assert await fire(sm, "queued_trigger_pending") is True
        assert sm.current_state is ConversationState.THINKING

    async def test_queued_trigger_blocked_when_queue_empty(self) -> None:
        sm = make_sm()  # không set predicate → has_queued_trigger False
        await fire(sm, "trigger_received")
        await fire(sm, "llm_fail")
        assert await fire(sm, "queued_trigger_pending") is False
        assert sm.current_state is ConversationState.COOLDOWN

    async def test_invalid_transition_leaves_state_unchanged(self) -> None:
        sm = make_sm()
        assert await fire(sm, "first_token") is False  # IDLE không có first_token
        assert sm.current_state is ConversationState.IDLE


class TestInterrupt:
    async def test_interrupt_sets_flag_not_new_state(self) -> None:
        """7.10.1: bỏ state INTERRUPTED, dùng flag last_turn_interrupted."""
        sm = make_sm()
        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        assert sm.last_turn_interrupted is False
        assert await fire(sm, "interrupted") is True
        assert sm.current_state is ConversationState.COOLDOWN
        assert sm.last_turn_interrupted is True

    async def test_flag_cleared_on_next_turn(self) -> None:
        sm = make_sm()
        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        await fire(sm, "interrupted")
        await fire(sm, "cooldown_elapsed")
        await fire(sm, "trigger_received")  # turn mới
        assert sm.last_turn_interrupted is False

    async def test_normal_completion_leaves_flag_false(self) -> None:
        sm = make_sm()
        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        await fire(sm, "tts_complete")
        assert sm.last_turn_interrupted is False

    async def test_history_records_interrupted(self) -> None:
        sm = make_sm()
        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        await fire(sm, "interrupted")
        last = sm.history()[-1]
        assert last.trigger == "interrupted"
        assert last.interrupted is True


class TestEmergencyStop:
    """DoD Phase 0: Emergency stop → PAUSED từ MỌI state."""

    @pytest.mark.parametrize(
        "path",
        [
            [],                                             # IDLE
            ["trigger_received"],                           # THINKING
            ["trigger_received", "first_token"],            # SPEAKING
            ["trigger_received", "llm_fail"],               # COOLDOWN
            ["emergency_stop"],                             # PAUSED (idempotent)
        ],
    )
    async def test_emergency_stop_from_every_state(self, path: list[str]) -> None:
        sm = make_sm()
        for trigger in path:
            await fire(sm, trigger)
        assert await fire(sm, "emergency_stop") is True
        assert sm.current_state is ConversationState.PAUSED

    async def test_resume_returns_to_idle(self) -> None:
        sm = make_sm()
        await fire(sm, "trigger_received")
        await fire(sm, "emergency_stop")
        assert await fire(sm, "resume") is True
        assert sm.current_state is ConversationState.IDLE

    async def test_resume_only_from_paused(self) -> None:
        sm = make_sm()
        assert await fire(sm, "resume") is False
        assert sm.current_state is ConversationState.IDLE

    async def test_paused_blocks_normal_triggers(self) -> None:
        sm = make_sm()
        await fire(sm, "emergency_stop")
        for trigger in ("trigger_received", "first_token", "tts_complete", "cooldown_elapsed"):
            assert await fire(sm, trigger) is False
        assert sm.current_state is ConversationState.PAUSED

    async def test_on_paused_hook_fires(self) -> None:
        sm = make_sm()
        calls: list[str] = []
        sm.set_action("on_paused", lambda e: asyncio.sleep(0, result=calls.append("paused")))
        await fire(sm, "emergency_stop")
        assert calls == ["paused"]


class TestActionHooks:
    async def test_hooks_fire_in_order(self) -> None:
        sm = make_sm()
        calls: list[str] = []

        def hook(name: str):
            async def _h(event):
                calls.append(name)
            return _h

        for name in ("load_context_and_start_llm", "start_tts", "finalize_turn"):
            sm.set_action(name, hook(name))

        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        await fire(sm, "tts_complete")
        assert calls == ["load_context_and_start_llm", "start_tts", "finalize_turn"]

    async def test_unknown_action_name_rejected(self) -> None:
        sm = make_sm()
        with pytest.raises(ValueError, match="Action không hợp lệ"):
            sm.set_action("do_something_weird", lambda e: asyncio.sleep(0))

    async def test_all_action_names_registerable(self) -> None:
        sm = make_sm()
        for name in ACTION_NAMES:
            sm.set_action(name, lambda e: asyncio.sleep(0))

    async def test_action_failure_does_not_break_state_machine(self) -> None:
        """N7 fail-safe: action lỗi → state đã chuyển, machine không kẹt."""
        sm = make_sm()

        async def boom(event):
            raise RuntimeError("LLM spawn failed")

        sm.set_action("load_context_and_start_llm", boom)
        assert await fire(sm, "trigger_received") is True
        assert sm.current_state is ConversationState.THINKING
        # vẫn đi tiếp được
        assert await fire(sm, "llm_fail") is True

    async def test_fallback_hook_on_llm_fail(self) -> None:
        sm = make_sm()
        calls: list[str] = []
        sm.set_action(
            "use_fallback_response",
            lambda e: asyncio.sleep(0, result=calls.append("fallback")),
        )
        await fire(sm, "trigger_received")
        await fire(sm, "llm_fail")
        assert calls == ["fallback"]

    async def test_queue_predicate_exception_treated_as_empty(self) -> None:
        sm = make_sm()

        async def boom() -> bool:
            raise RuntimeError("queue down")

        sm.set_queue_predicate(boom)
        await fire(sm, "trigger_received")
        await fire(sm, "llm_fail")
        assert await fire(sm, "queued_trigger_pending") is False


class TestCooldownTimer:
    async def test_auto_transition_to_idle(self) -> None:
        sm = ConversationStateMachine(cooldown_ms=20, auto_cooldown=True)
        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        await fire(sm, "tts_complete")
        assert sm.current_state is ConversationState.COOLDOWN
        await sm.wait_cooldown(timeout=2)
        assert sm.current_state is ConversationState.IDLE

    async def test_auto_transition_to_thinking_when_queued(self) -> None:
        sm = ConversationStateMachine(cooldown_ms=20, auto_cooldown=True)
        sm.set_queue_predicate(lambda: asyncio.sleep(0, result=True))
        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        await fire(sm, "tts_complete")
        await sm.wait_cooldown(timeout=2)
        assert sm.current_state is ConversationState.THINKING

    async def test_emergency_stop_cancels_cooldown_timer(self) -> None:
        """Timer đang chạy không được kéo PAUSED về IDLE."""
        sm = ConversationStateMachine(cooldown_ms=50, auto_cooldown=True)
        await fire(sm, "trigger_received")
        await fire(sm, "llm_fail")            # → COOLDOWN, timer start
        await fire(sm, "emergency_stop")      # → PAUSED, timer phải bị cancel
        await asyncio.sleep(0.15)             # quá thời gian cooldown
        assert sm.current_state is ConversationState.PAUSED

    async def test_shutdown_cancels_timer(self) -> None:
        sm = ConversationStateMachine(cooldown_ms=50, auto_cooldown=True)
        await fire(sm, "trigger_received")
        await fire(sm, "llm_fail")
        await sm.shutdown()
        await asyncio.sleep(0.12)
        assert sm.current_state is ConversationState.COOLDOWN  # timer không chạy

    async def test_wait_cooldown_noop_when_no_timer(self) -> None:
        sm = make_sm()
        await sm.wait_cooldown(timeout=0.1)


class TestLoggingAndMetrics:
    async def test_history_records_each_transition(self) -> None:
        sm = make_sm()
        await fire(sm, "trigger_received")
        await fire(sm, "first_token")
        hist = sm.history()
        assert [(h.from_state, h.to_state) for h in hist] == [
            ("IDLE", "THINKING"),
            ("THINKING", "SPEAKING"),
        ]

    async def test_history_limit_respected(self) -> None:
        sm = make_sm(history_limit=3)
        for _ in range(4):
            await fire(sm, "trigger_received")
            await fire(sm, "first_token")
            await fire(sm, "tts_complete")
            await fire(sm, "cooldown_elapsed")
        assert len(sm.history()) == 3

    async def test_transition_counts(self) -> None:
        sm = make_sm()
        for _ in range(2):
            await fire(sm, "trigger_received")
            await fire(sm, "first_token")
            await fire(sm, "tts_complete")
            await fire(sm, "cooldown_elapsed")
        counts = sm.transition_counts()
        assert counts[("IDLE", "THINKING")] == 2
        assert counts[("THINKING", "SPEAKING")] == 2

    async def test_metrics_shape(self) -> None:
        sm = make_sm()
        await fire(sm, "trigger_received")
        m = sm.get_metrics()
        assert m["state_current"] == "THINKING"
        assert m["state_transitions_total"] == 1
        assert m["state_time_in_state_ms"] >= 0

    async def test_publishes_to_event_bus(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("state_change")
        sm = make_sm(event_bus=bus)
        await fire(sm, "trigger_received")
        event = await sub.get()
        assert event is not None
        assert event.payload["from"] == "IDLE"
        assert event.payload["to"] == "THINKING"
        assert event.payload["trigger"] == "trigger_received"

    async def test_previous_state_tracked(self) -> None:
        sm = make_sm()
        await fire(sm, "trigger_received")
        assert sm.previous_state == "IDLE"
        await fire(sm, "first_token")
        assert sm.previous_state == "THINKING"


class TestFromConfig:
    def test_reads_state_machine_yaml(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        sm = ConversationStateMachine.from_config(loader, auto_cooldown=False)
        assert sm.cooldown_ms == 500
        assert sm.current_state is ConversationState.IDLE

    def test_config_cooldown_matches_system_yaml(self) -> None:
        """state_machine.yaml và system.yaml không được nói 2 số khác nhau."""
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        assert loader.get("state_machine", "state_machine.cooldown_ms") == loader.get(
            "system", "conversation.cooldown_ms"
        )

    def test_config_watchdog_thresholds_match_system_yaml(self) -> None:
        """max_time_in_state (state_machine.yaml) khớp max_state_duration (system.yaml)."""
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        sm_limits = loader.require("state_machine", "state_machine.auto_recovery.max_time_in_state_seconds")
        sys_limits = loader.require("system", "conversation.max_state_duration")
        for state, ms in sys_limits.items():
            assert sm_limits[state] == ms / 1000, f"{state}: 2 config nói 2 số khác nhau"

    def test_config_states_cover_all_five(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        limits = loader.require(
            "state_machine", "state_machine.auto_recovery.max_time_in_state_seconds"
        )
        assert set(limits) == set(ConversationStateMachine.states)


# ---------- Property-based tests (spec 12.6) ----------

VALID_STATES = set(ConversationStateMachine.states)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(ALL_TRIGGERS), min_size=0, max_size=25))
async def test_property_random_triggers_keep_state_valid(triggers: list[str]) -> None:
    """Random sequence of triggers → state machine always valid (12.6)."""
    sm = make_sm()
    for trigger in triggers:
        await fire(sm, trigger)
        assert sm.state in VALID_STATES, f"state lạ sau {trigger}: {sm.state}"
        # flag interrupted chỉ có nghĩa khi đã từng SPEAKING
        assert isinstance(sm.last_turn_interrupted, bool)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(ALL_TRIGGERS), min_size=0, max_size=25))
async def test_property_emergency_stop_always_reaches_paused(triggers: list[str]) -> None:
    """Emergency stop từ mọi state → PAUSED (12.6 + DoD Phase 0)."""
    sm = make_sm()
    for trigger in triggers:
        await fire(sm, trigger)
    assert await fire(sm, "emergency_stop") is True
    assert sm.current_state is ConversationState.PAUSED


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(ALL_TRIGGERS), min_size=0, max_size=20))
async def test_property_resume_always_recovers_to_idle(triggers: list[str]) -> None:
    """Sau bất kỳ chuỗi nào: emergency_stop + resume phải về IDLE (recovery path)."""
    sm = make_sm()
    for trigger in triggers:
        await fire(sm, trigger)
    await fire(sm, "emergency_stop")
    await fire(sm, "resume")
    assert sm.current_state is ConversationState.IDLE


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(ALL_TRIGGERS), min_size=1, max_size=20))
async def test_property_history_matches_transition_chain(triggers: list[str]) -> None:
    """History phải là chuỗi liên tục: to_state của bước n = from_state của bước n+1."""
    sm = make_sm()
    for trigger in triggers:
        await fire(sm, trigger)
    hist = sm.history()
    for earlier, later in zip(hist, hist[1:]):
        assert earlier.to_state == later.from_state, f"history đứt: {earlier} → {later}"
    if hist:
        assert hist[-1].to_state == sm.state


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(ALL_TRIGGERS), min_size=1, max_size=20))
async def test_property_speaking_only_reachable_from_thinking(triggers: list[str]) -> None:
    """Chỉ có 1 đường vào SPEAKING: THINKING --first_token--> SPEAKING (7.10.2)."""
    sm = make_sm()
    for trigger in triggers:
        await fire(sm, trigger)
    for record in sm.history():
        if record.to_state == "SPEAKING":
            assert record.from_state == "THINKING"
            assert record.trigger == "first_token"
