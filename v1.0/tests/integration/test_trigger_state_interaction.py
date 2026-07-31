"""Integration: Trigger ↔ State Machine (ARCHITECTURE 12.8 + DoD P2, milestone 2.E).

Bug turn-taking sống ở interaction giữa trigger manager + state machine → test
2 hệ thống chạy chung qua TurnOrchestrator (think giả, không cần LLM/TTS thật).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from interfaces.input import EventSource, InputEvent
from interfaces.trigger import TriggerType
from orchestrator.state_machine import ConversationStateMachine
from orchestrator.state_watchdog import StateWatchdog
from orchestrator.trigger_manager import TriggerManager
from orchestrator.turn_orchestrator import TurnOrchestrator

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_tm(interrupt_ms: int = 2000, **over) -> TriggerManager:
    kw = dict(
        priorities={"operator_voice": 100, "chat_mention": 60, "chat_normal": 30, "ambient_talk": 10},
        keywords=["mai"],
        rate_window_s=10.0, rate_max=3, queue_max_size=30, default_ttl_s=30,
        ambient_enabled=True, ambient_silence_s=60, spam_min_length=2,
        spam_patterns=["^k+$", "^h+a+$"],
        interrupt_allow_ms={
            TriggerType.OPERATOR_VOICE: interrupt_ms,
            TriggerType.CHAT_MENTION: 999999,
            TriggerType.CHAT_NORMAL: 999999,
        },
    )
    kw.update(over)
    return TriggerManager(**kw)


def chat(content: str) -> InputEvent:
    return InputEvent(event_id="c", timestamp=datetime.now(timezone.utc),
                      source=EventSource.CHAT_TWITCH, content=content)


def operator(content: str = "làm gì đi") -> InputEvent:
    return InputEvent(event_id="o", timestamp=datetime.now(timezone.utc),
                      source=EventSource.DASHBOARD, content=content)


def build(think=None, speak_seconds=0.0, interrupt_ms=2000, watchdog=False):
    tm = make_tm(interrupt_ms=interrupt_ms)
    sm = ConversationStateMachine(cooldown_ms=20)
    wd = StateWatchdog(sm, {"THINKING": 10, "SPEAKING": 30}, check_interval_s=0.05) if watchdog else None

    async def default_think(_trig):
        return "reply"

    orch = TurnOrchestrator(tm, sm, think=think or default_think,
                            speak_seconds=speak_seconds, watchdog=wd, poll_s=0.02)
    return orch, tm, sm


# ---------- DoD: trigger-level (không cần orchestrator) ----------

class TestPriorityAndLimits:
    async def test_priority_operator_mention_normal(self) -> None:
        tm = make_tm()
        await tm.process_event(chat("chào cậu"))       # normal
        await tm.process_event(chat("mai ơi"))         # mention (keyword)
        await tm.process_event(operator())             # operator
        order = [(await tm.get_next_trigger()).type for _ in range(3)]
        assert order == [TriggerType.OPERATOR_VOICE, TriggerType.CHAT_MENTION, TriggerType.CHAT_NORMAL]

    async def test_spam_60_per_minute_rate_limited(self) -> None:
        tm = make_tm()  # rate 3 / 10s
        for i in range(60):
            await tm.process_event(chat(f"tin so {i}"))
        stats = await tm.get_queue_stats()
        assert stats.size == 3          # chỉ 3 lọt (rate limit)
        assert stats.skipped_total == 57

    async def test_ambient_after_silence(self) -> None:
        tm = make_tm()
        tm.last_speak_time = datetime.now(timezone.utc) - timedelta(seconds=61)
        trig = await tm.get_next_trigger()
        assert trig is not None and trig.type is TriggerType.AMBIENT_TALK

    async def test_no_ambient_before_silence(self) -> None:
        tm = make_tm()
        tm.last_speak_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        assert await tm.get_next_trigger() is None


# ---------- DoD: interaction (qua orchestrator) ----------

class TestInteraction:
    async def test_two_triggers_both_processed(self) -> None:
        orch, tm, sm = build()
        await orch.start()
        try:
            await orch.send_event(chat("hi"))
            await asyncio.sleep(0.001)
            await orch.send_event(chat("mai"))
            for _ in range(200):
                if orch.processed_turns >= 2:
                    break
                await asyncio.sleep(0.01)
            assert orch.processed_turns >= 2
            assert await orch.trigger_queue_size() == 0
        finally:
            await orch.stop()

    async def test_trigger_during_thinking_queued(self) -> None:
        async def slow_think(_t):
            await asyncio.sleep(0.15)
            return "reply"

        orch, tm, sm = build(think=slow_think)
        await orch.start()
        try:
            await orch.send_event(chat("hi"))
            assert await orch.wait_for_state("THINKING")
            await orch.send_event(chat("hello"))       # đến khi đang THINKING
            assert (await orch.trigger_queue_size()) == 1   # queue, không interrupt
            for _ in range(200):
                if orch.processed_turns >= 2:
                    break
                await asyncio.sleep(0.01)
            assert orch.processed_turns >= 2
        finally:
            await orch.stop()

    async def test_spam_during_speaking_dropped(self) -> None:
        orch, tm, sm = build(speak_seconds=0.4)
        await orch.start()
        try:
            await orch.send_event(chat("mai oi"))
            assert await orch.wait_for_state("SPEAKING")
            for _ in range(20):
                await orch.send_event(chat("kkkkk"))   # spam ^k+$
            assert (await orch.trigger_queue_size()) == 0
        finally:
            await orch.stop()

    async def test_operator_interrupts_speaking(self) -> None:
        orch, tm, sm = build(speak_seconds=0.5, interrupt_ms=0)  # interrupt ngay
        await orch.start()
        try:
            await orch.send_event(chat("kể chuyện"))
            assert await orch.wait_for_state("SPEAKING")
            d = await orch.send_event(operator("stop"))
            assert d.action.value == "interrupt_current"
            for _ in range(200):
                if orch.processed_turns >= 2:
                    break
                await asyncio.sleep(0.01)
            assert tm.get_metrics()["trigger_interrupt_total"] == 1
            assert orch.processed_turns >= 2
        finally:
            await orch.stop()

    async def test_emergency_stop_from_speaking(self) -> None:
        orch, tm, sm = build(speak_seconds=0.5)
        await orch.start()
        try:
            await orch.send_event(chat("hello"))
            assert await orch.wait_for_state("SPEAKING")
            await orch.emergency_stop()
            assert orch.state == "PAUSED"
            assert (await orch.trigger_queue_size()) == 0
        finally:
            await orch.stop()

    async def test_resume_after_emergency(self) -> None:
        orch, tm, sm = build(speak_seconds=0.3)
        await orch.start()
        try:
            await orch.send_event(chat("hello"))
            await orch.wait_for_state("SPEAKING")
            await orch.emergency_stop()
            await orch.resume()
            assert await orch.wait_for_state("IDLE")
        finally:
            await orch.stop()


class TestWatchdogWiring:
    async def test_start_stop_with_watchdog_clean(self) -> None:
        orch, tm, sm = build(watchdog=True)
        await orch.start()
        await asyncio.sleep(0.1)
        await asyncio.wait_for(orch.stop(), timeout=3.0)   # KHÔNG hang
