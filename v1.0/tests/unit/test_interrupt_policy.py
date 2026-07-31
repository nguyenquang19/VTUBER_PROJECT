"""Test interrupt policy 7.9.3 (Phase 2, 2.A)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from interfaces.input import EventSource, InputEvent
from interfaces.trigger import TriggerAction, TriggerType
from orchestrator.trigger_manager import TriggerManager

REPO_ROOT = Path(__file__).resolve().parents[2]


class SpeakCtx:
    """Provider giả (is_speaking, elapsed_ms)."""

    def __init__(self, speaking: bool = False, elapsed_ms: int = 0) -> None:
        self.speaking = speaking
        self.elapsed_ms = elapsed_ms

    def __call__(self) -> tuple[bool, int]:
        return self.speaking, self.elapsed_ms


def make_tm(**over) -> TriggerManager:
    kw = dict(
        priorities={"operator_voice": 100, "chat_mention": 60, "chat_normal": 30, "ambient_talk": 10},
        keywords=["mai"],
        rate_window_s=10.0,
        rate_max=1000,
        queue_max_size=30,
        default_ttl_s=30,
        ambient_enabled=False,
        ambient_silence_s=60,
        spam_min_length=2,
        spam_patterns=[],
        interrupt_allow_ms={
            TriggerType.OPERATOR_VOICE: 2000,
            TriggerType.CHAT_MENTION: 999999,
            TriggerType.CHAT_NORMAL: 999999,
        },
    )
    kw.update(over)
    return TriggerManager(**kw)


def ev(content: str, source: EventSource = EventSource.CHAT_TWITCH) -> InputEvent:
    return InputEvent(
        event_id="e1", timestamp=datetime.now(timezone.utc), source=source, content=content
    )


def operator_ev() -> InputEvent:
    return ev("làm gì đó đi", source=EventSource.DASHBOARD)


class TestOperatorInterrupt:
    async def test_interrupt_when_speaking_over_threshold(self) -> None:
        tm = make_tm()
        tm.set_speaking_context(SpeakCtx(speaking=True, elapsed_ms=3000))
        d = await tm.process_event(operator_ev())
        assert d.action is TriggerAction.INTERRUPT_CURRENT

    async def test_queue_when_speaking_under_threshold(self) -> None:
        tm = make_tm()
        tm.set_speaking_context(SpeakCtx(speaking=True, elapsed_ms=1000))
        d = await tm.process_event(operator_ev())
        assert d.action is TriggerAction.QUEUE

    async def test_queue_when_not_speaking(self) -> None:
        tm = make_tm()
        tm.set_speaking_context(SpeakCtx(speaking=False, elapsed_ms=9999))
        d = await tm.process_event(operator_ev())
        assert d.action is TriggerAction.QUEUE

    async def test_exactly_at_threshold_interrupts(self) -> None:
        tm = make_tm()
        tm.set_speaking_context(SpeakCtx(speaking=True, elapsed_ms=2000))
        d = await tm.process_event(operator_ev())
        assert d.action is TriggerAction.INTERRUPT_CURRENT


class TestChatNeverInterrupts:
    async def test_mention_queues_while_speaking(self) -> None:
        tm = make_tm()
        tm.set_speaking_context(SpeakCtx(speaking=True, elapsed_ms=9999))
        d = await tm.process_event(ev("mai ơi nghe nè"))  # có keyword → mention
        assert d.action is TriggerAction.QUEUE

    async def test_normal_queues_while_speaking(self) -> None:
        tm = make_tm()
        tm.set_speaking_context(SpeakCtx(speaking=True, elapsed_ms=9999))
        d = await tm.process_event(ev("chào cậu"))
        assert d.action is TriggerAction.QUEUE


class TestSafety:
    async def test_no_provider_never_interrupts(self) -> None:
        tm = make_tm()  # không set_speaking_context
        d = await tm.process_event(operator_ev())
        assert d.action is TriggerAction.QUEUE

    async def test_provider_error_fails_safe(self) -> None:
        tm = make_tm()

        def boom():
            raise RuntimeError("x")

        tm.set_speaking_context(boom)
        d = await tm.process_event(operator_ev())
        assert d.action is TriggerAction.QUEUE

    async def test_interrupted_trigger_still_enqueued(self) -> None:
        # interrupt xong Mai vẫn phải trả lời trigger đó → nó phải nằm trong queue
        tm = make_tm()
        tm.set_speaking_context(SpeakCtx(speaking=True, elapsed_ms=3000))
        await tm.process_event(operator_ev())
        stats = await tm.get_queue_stats()
        assert stats.size == 1
        assert stats.by_type.get("operator_voice") == 1

    async def test_interrupt_metric_increments(self) -> None:
        tm = make_tm()
        tm.set_speaking_context(SpeakCtx(speaking=True, elapsed_ms=3000))
        await tm.process_event(operator_ev())
        assert tm.get_metrics()["trigger_interrupt_total"] == 1


class TestFromConfig:
    def test_loads_interrupt_policy(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        tm = TriggerManager.from_config(loader)
        assert tm._interrupt_allow_ms[TriggerType.OPERATOR_VOICE] == 2000
        assert tm._interrupt_allow_ms[TriggerType.CHAT_NORMAL] == 999999
