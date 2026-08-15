"""Test dashboard snapshot Phase 2: triggers skipped/interrupt + watchdog (2.D)."""
from __future__ import annotations

from datetime import datetime, timezone

from dashboard.dashboard_server import DashboardServer
from interfaces.input import EventSource, InputEvent
from interfaces.trigger import TriggerType
from orchestrator.state_machine import ConversationStateMachine
from orchestrator.state_watchdog import StateWatchdog
from orchestrator.trigger_manager import TriggerManager


def make_tm() -> TriggerManager:
    return TriggerManager(
        priorities={"operator_voice": 100, "chat_mention": 60, "chat_normal": 30, "ambient_talk": 10},
        keywords=["mai"],
        rate_window_s=10.0, rate_max=1000, queue_max_size=30, default_ttl_s=30,
        ambient_enabled=False, ambient_silence_s=60, spam_min_length=2, spam_patterns=[],
        interrupt_allow_ms={TriggerType.OPERATOR_VOICE: 2000},
    )


def ev(content: str, source=EventSource.CHAT_TWITCH) -> InputEvent:
    return InputEvent(event_id="e", timestamp=datetime.now(timezone.utc), source=source, content=content)


class TestTriggersSnapshot:
    async def test_skipped_and_interrupt_in_stats(self) -> None:
        tm = make_tm()
        tm.set_speaking_context(lambda: (True, 3000))
        await tm.process_event(ev("x"))                          # < min_length → spam skip
        await tm.process_event(ev("làm gì đi", EventSource.DASHBOARD))  # operator → interrupt
        stats = await tm.get_queue_stats()
        assert stats.skipped_total == 1
        assert stats.interrupt_total == 1

    async def test_dashboard_exposes_triggers_fields(self) -> None:
        tm = make_tm()
        server = DashboardServer(trigger_manager=tm)
        snap = await server.build_snapshot()
        assert "skipped_total" in snap["triggers"]
        assert "interrupt_total" in snap["triggers"]


class TestWatchdogSnapshot:
    async def test_watchdog_section_present(self) -> None:
        sm = ConversationStateMachine(auto_cooldown=False)
        wd = StateWatchdog(sm, max_time_in_state={"THINKING": 10}, check_interval_s=1.0)
        server = DashboardServer(state_machine=sm, watchdog=wd)
        snap = await server.build_snapshot()
        assert "watchdog" in snap
        assert snap["watchdog"]["deadlocks_total"] == 0
        assert "THINKING" in snap["watchdog"]["watched_states"]

    async def test_no_watchdog_no_section(self) -> None:
        server = DashboardServer(state_machine=ConversationStateMachine(auto_cooldown=False))
        snap = await server.build_snapshot()
        assert "watchdog" not in snap


class TestMoodSnapshot:
    async def test_mood_section_has_five_dims(self) -> None:
        # TASK 8: dashboard build_snapshot với emotion → snap["mood"].current_mood 5 dim
        class FakeEmotion:
            def snapshot(self):
                return {
                    "current_mood": {"vui": 6, "buon": 1, "buc": 0, "bon_chon": 3, "nguong": 2},
                    "mood_pos": {}, "mood_target": {}, "active_flags": [],
                }
        server = DashboardServer(emotion=FakeEmotion())
        snap = await server.build_snapshot()
        assert "mood" in snap
        for dim in ("vui", "buon", "buc", "bon_chon", "nguong"):
            assert dim in snap["mood"]["current_mood"]

    async def test_no_emotion_no_mood_section(self) -> None:
        server = DashboardServer()
        snap = await server.build_snapshot()
        assert "mood" not in snap
