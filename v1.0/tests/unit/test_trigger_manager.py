"""Test TriggerManager (ARCHITECTURE 7.9.2, test spec 12.7).

Phase 0 skeleton: classify 4 type, priority queue, spam, rate limit, ambient.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from interfaces.input import EventSource, InputEvent
from interfaces.trigger import TriggerAction, TriggerType
from orchestrator.config_loader import ConfigLoader
from orchestrator.event_bus import EventBus
from orchestrator.trigger_manager import SimpleRateLimiter, TriggerManager

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_manager(**overrides) -> TriggerManager:
    defaults = dict(
        priorities={
            "operator_voice": 100,
            "chat_mention": 60,
            "chat_normal": 30,
            "ambient_talk": 10,
        },
        keywords=["mai", "ông"],
        rate_window_s=10.0,
        rate_max=3,
        queue_max_size=30,
        default_ttl_s=30,
        ambient_enabled=True,
        ambient_silence_s=60,
        spam_min_length=2,
        spam_patterns=["^k+$", "^h+a+$"],
    )
    defaults.update(overrides)
    return TriggerManager(**defaults)


def chat(content: str, source: EventSource = EventSource.CHAT_TWITCH, user: str = "viewer") -> InputEvent:
    return InputEvent(
        event_id="e",
        timestamp=datetime.now(timezone.utc),
        source=source,
        user_name=user,
        content=content,
    )


class TestRateLimiter:
    def test_allows_up_to_max(self) -> None:
        rl = SimpleRateLimiter(window_seconds=10, max_events=3)
        assert [rl.check(100.0) for _ in range(4)] == [True, True, True, False]

    def test_window_slides(self) -> None:
        rl = SimpleRateLimiter(window_seconds=10, max_events=2)
        assert rl.check(100.0) is True
        assert rl.check(101.0) is True
        assert rl.check(102.0) is False
        # sau 10s, slot cũ hết hạn
        assert rl.check(111.5) is True


class TestClassify:
    async def test_dashboard_is_operator(self) -> None:
        m = make_manager()
        d = await m.process_event(chat("dừng lại", source=EventSource.DASHBOARD))
        assert d.priority == 100  # operator_voice

    async def test_mention_by_keyword(self) -> None:
        m = make_manager()
        d = await m.process_event(chat("mai ơi chơi gì"))
        assert d.priority == 60

    async def test_mention_keyword_ong(self) -> None:
        m = make_manager()
        d = await m.process_event(chat("ông thấy sao"))
        assert d.priority == 60

    async def test_normal_chat(self) -> None:
        m = make_manager()
        d = await m.process_event(chat("trận này hay quá"))
        assert d.priority == 30

    async def test_classify_case_insensitive(self) -> None:
        m = make_manager()
        d = await m.process_event(chat("MAI oi"))
        assert d.priority == 60


class TestSpam:
    async def test_too_short_is_spam(self) -> None:
        m = make_manager()
        d = await m.process_event(chat("k"))
        assert d.action is TriggerAction.SKIP
        assert d.reason == "spam"

    async def test_kkkk_is_spam(self) -> None:
        m = make_manager()
        assert (await m.process_event(chat("kkkkk"))).reason == "spam"

    async def test_hahaha_is_spam(self) -> None:
        m = make_manager()
        assert (await m.process_event(chat("haaaa"))).reason == "spam"

    async def test_normal_text_not_spam(self) -> None:
        m = make_manager()
        assert (await m.process_event(chat("hay quá"))).action is TriggerAction.QUEUE

    async def test_spam_counted_in_metrics(self) -> None:
        m = make_manager()
        await m.process_event(chat("k"))
        assert m.get_metrics()["trigger_skipped_total"] == 1


class TestRateLimit:
    """Rate limit CHỈ áp chat_normal — operator/mention luôn qua (7.9.2)."""

    async def test_chat_normal_rate_limited(self) -> None:
        m = make_manager(rate_max=2)
        actions = [(await m.process_event(chat(f"tin {i}"))).action for i in range(4)]
        assert actions[:2] == [TriggerAction.QUEUE, TriggerAction.QUEUE]
        assert actions[2:] == [TriggerAction.SKIP, TriggerAction.SKIP]

    async def test_mention_not_rate_limited(self) -> None:
        m = make_manager(rate_max=1)
        for i in range(5):
            d = await m.process_event(chat(f"mai tin {i}"))
            assert d.action is TriggerAction.QUEUE

    async def test_operator_not_rate_limited(self) -> None:
        m = make_manager(rate_max=1)
        for i in range(5):
            d = await m.process_event(chat(f"lệnh {i}", source=EventSource.DASHBOARD))
            assert d.action is TriggerAction.QUEUE

    async def test_rate_limit_reason(self) -> None:
        m = make_manager(rate_max=1)
        await m.process_event(chat("tin 1"))
        d = await m.process_event(chat("tin 2"))
        assert d.reason == "rate_limited"


class TestPriorityQueue:
    """Priority: operator > mention > normal (test spec 12.7)."""

    async def test_get_returns_highest_priority_first(self) -> None:
        m = make_manager()
        await m.process_event(chat("tin thường"))                              # 30
        await m.process_event(chat("mai ơi"))                                  # 60
        await m.process_event(chat("lệnh", source=EventSource.DASHBOARD))      # 100

        assert (await m.get_next_trigger()).type is TriggerType.OPERATOR_VOICE
        assert (await m.get_next_trigger()).type is TriggerType.CHAT_MENTION
        assert (await m.get_next_trigger()).type is TriggerType.CHAT_NORMAL

    async def test_fifo_within_same_priority(self) -> None:
        m = make_manager()
        await m.process_event(chat("mai một"))
        await m.process_event(chat("mai hai"))
        assert (await m.get_next_trigger()).event.content == "mai một"
        assert (await m.get_next_trigger()).event.content == "mai hai"

    async def test_empty_queue_returns_none_when_no_ambient(self) -> None:
        m = make_manager(ambient_enabled=False)
        assert await m.get_next_trigger() is None


class TestQueueOverflow:
    async def test_drop_lowest_priority_when_full(self) -> None:
        m = make_manager(queue_max_size=2)
        await m.process_event(chat("tin thường"))                          # 30
        await m.process_event(chat("mai ơi"))                              # 60
        # queue đầy (2). Thêm operator (100) → drop tin thường (30)
        d = await m.process_event(chat("lệnh", source=EventSource.DASHBOARD))
        assert d.action is TriggerAction.QUEUE

        types = []
        while (t := await m.get_next_trigger()) is not None:
            types.append(t.type)
        assert TriggerType.OPERATOR_VOICE in types
        assert TriggerType.CHAT_MENTION in types
        assert TriggerType.CHAT_NORMAL not in types  # đã bị drop

    async def test_reject_new_when_not_higher_than_lowest(self) -> None:
        m = make_manager(queue_max_size=1)
        await m.process_event(chat("mai ơi"))  # 60
        # queue đầy, thêm chat_normal (30) < 60 → drop trigger mới
        d = await m.process_event(chat("tin thường"))
        assert d.action is TriggerAction.SKIP
        assert d.reason == "queue_full_lower_priority"

    async def test_dropped_counted(self) -> None:
        m = make_manager(queue_max_size=1)
        await m.process_event(chat("mai một"))
        await m.process_event(chat("mai hai"))  # cùng prio → drop 1
        assert m.get_metrics()["trigger_dropped_total"] == 1


class TestTTLExpiry:
    async def test_expired_triggers_pruned(self) -> None:
        m = make_manager(default_ttl_s=30)
        await m.process_event(chat("mai ơi"))
        # ép trigger cũ hơn TTL
        old = m._heap[0][2]
        aged = old.model_copy(update={"created_at": datetime.now(timezone.utc) - timedelta(seconds=60)})
        m._heap[0] = (m._heap[0][0], m._heap[0][1], aged)

        assert await m.get_next_trigger() is None  # bị prune
        assert m.get_metrics()["trigger_expired_total"] == 1


class TestAmbient:
    """Ambient: 1 threshold cứng 60s, KHÔNG probability (7.9.2 / N1)."""

    async def test_ambient_after_silence(self) -> None:
        m = make_manager(ambient_silence_s=60)
        m.last_speak_time = datetime.now(timezone.utc) - timedelta(seconds=61)
        t = await m.get_next_trigger()
        assert t is not None
        assert t.type is TriggerType.AMBIENT_TALK

    async def test_no_ambient_before_threshold(self) -> None:
        m = make_manager(ambient_silence_s=60)
        m.last_speak_time = datetime.now(timezone.utc) - timedelta(seconds=30)
        assert await m.get_next_trigger() is None

    async def test_ambient_disabled(self) -> None:
        m = make_manager(ambient_enabled=False, ambient_silence_s=60)
        m.last_speak_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        assert await m.get_next_trigger() is None

    async def test_queue_takes_precedence_over_ambient(self) -> None:
        m = make_manager(ambient_silence_s=60)
        m.last_speak_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        await m.process_event(chat("mai ơi"))
        t = await m.get_next_trigger()
        assert t.type is TriggerType.CHAT_MENTION  # queue trước ambient

    async def test_mark_spoke_resets_silence(self) -> None:
        m = make_manager(ambient_silence_s=60)
        m.last_speak_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        m.mark_spoke()
        assert await m.get_next_trigger() is None


class TestQueueManagement:
    async def test_clear_queue(self) -> None:
        m = make_manager()
        await m.process_event(chat("mai một"))
        await m.process_event(chat("mai hai"))
        await m.clear_queue("emergency")
        stats = await m.get_queue_stats()
        assert stats.size == 0

    async def test_queue_stats_by_type(self) -> None:
        m = make_manager()
        await m.process_event(chat("mai ơi"))
        await m.process_event(chat("tin thường"))
        await m.process_event(chat("lệnh", source=EventSource.DASHBOARD))
        stats = await m.get_queue_stats()
        assert stats.size == 3
        assert stats.by_type["chat_mention"] == 1
        assert stats.by_type["chat_normal"] == 1
        assert stats.by_type["operator_voice"] == 1

    async def test_publishes_trigger_queued_event(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("trigger_queued")
        m = make_manager(event_bus=bus)
        await m.process_event(chat("mai ơi"))
        event = await sub.get()
        assert event.payload["type"] == "chat_mention"
        assert event.payload["priority"] == 60


class TestServiceContract:
    async def test_health_healthy_when_not_full(self) -> None:
        m = make_manager()
        h = await m.health_check()
        assert h.is_ok is True

    async def test_health_degraded_when_full(self) -> None:
        m = make_manager(queue_max_size=1)
        await m.process_event(chat("mai ơi"))
        h = await m.health_check()
        assert h.is_ok is False


class TestFromConfig:
    def test_loads_real_triggers_yaml(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = TriggerManager.from_config(loader)
        assert m._priorities["operator_voice"] == 100
        assert m._priorities["chat_mention"] == 60
        assert m._priorities["chat_normal"] == 30
        assert m._priorities["ambient_talk"] == 10

    async def test_config_only_four_types(self) -> None:
        """N1 YAGNI: config chỉ có đúng 4 priority."""
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        priorities = loader.require("triggers", "triggers.priorities")
        assert set(priorities) == {"operator_voice", "chat_mention", "chat_normal", "ambient_talk"}

    def test_config_ambient_matches_system_yaml(self) -> None:
        """ambient silence threshold phải khớp giữa triggers.yaml và system.yaml."""
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        assert loader.get("triggers", "triggers.ambient.min_silence_seconds") == loader.get(
            "system", "ambient_talk.min_silence_seconds"
        )
