"""Test EventBus: pub/sub fan-out, bounded queue, overflow policy."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from orchestrator.event_bus import TOPIC_ALL, EventBus

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestPubSub:
    async def test_subscriber_receives_published_event(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("state_change")
        bus.publish("state_change", {"to": "THINKING"})
        event = await sub.get()
        assert event is not None
        assert event.topic == "state_change"
        assert event.payload["to"] == "THINKING"

    async def test_fan_out_to_multiple_subscribers(self) -> None:
        bus = EventBus()
        a = bus.subscribe("tick")
        b = bus.subscribe("tick")
        assert bus.publish("tick", 1) == 2
        assert (await a.get()).payload == 1
        assert (await b.get()).payload == 1

    async def test_other_topics_not_delivered(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("wanted")
        assert bus.publish("unwanted", 1) == 0
        assert sub.qsize() == 0

    async def test_topic_all_receives_everything(self) -> None:
        bus = EventBus()
        watcher = bus.subscribe(TOPIC_ALL)
        bus.publish("a", 1)
        bus.publish("b", 2)
        assert (await watcher.get()).topic == "a"
        assert (await watcher.get()).topic == "b"

    async def test_topic_all_plus_specific_both_get_it(self) -> None:
        bus = EventBus()
        watcher = bus.subscribe(TOPIC_ALL)
        specific = bus.subscribe("a")
        assert bus.publish("a", 1) == 2
        assert (await watcher.get()).payload == 1
        assert (await specific.get()).payload == 1

    async def test_metadata_carried(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("t")
        bus.publish("t", "payload", turn_id=42, source="dashboard")
        event = await sub.get()
        assert event.metadata == {"turn_id": 42, "source": "dashboard"}

    async def test_publish_with_no_subscribers_is_safe(self) -> None:
        bus = EventBus()
        assert bus.publish("nobody_listening", 1) == 0

    async def test_async_iteration(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("t")
        for i in range(3):
            bus.publish("t", i)

        got = []
        async def consume():
            async for event in sub:
                got.append(event.payload)
                if len(got) == 3:
                    await sub.close()

        await asyncio.wait_for(consume(), timeout=2)
        assert got == [0, 1, 2]

    async def test_context_manager_closes(self) -> None:
        bus = EventBus()
        async with bus.subscribe("t") as sub:
            bus.publish("t", 1)
            assert (await sub.get()).payload == 1
        assert bus.subscriber_count("t") == 0


class TestUnsubscribe:
    async def test_close_removes_subscription(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("t")
        assert bus.subscriber_count("t") == 1
        await sub.close()
        assert bus.subscriber_count("t") == 0
        assert bus.publish("t", 1) == 0

    async def test_close_is_idempotent(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("t")
        await sub.close()
        await sub.close()

    async def test_closed_subscription_yields_sentinel(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("t")
        await sub.close()
        assert await sub.get() is None

    async def test_topic_removed_when_last_sub_leaves(self) -> None:
        bus = EventBus()
        a = bus.subscribe("t")
        b = bus.subscribe("t")
        await a.close()
        assert bus.topics() == ["t"]
        await b.close()
        assert bus.topics() == []

    async def test_bus_close_closes_all(self) -> None:
        bus = EventBus()
        a = bus.subscribe("x")
        b = bus.subscribe("y")
        await bus.close()
        assert bus.subscriber_count() == 0
        assert await a.get() is None
        assert await b.get() is None


class TestOverflow:
    async def test_drop_oldest_keeps_newest(self) -> None:
        bus = EventBus(max_queue_size=2, overflow_policy="drop_oldest")
        sub = bus.subscribe("t")
        for i in range(4):
            bus.publish("t", i)
        # queue giữ 2 event mới nhất
        assert [(await sub.get()).payload for _ in range(2)] == [2, 3]
        assert sub.dropped_count == 2

    async def test_drop_newest_keeps_oldest(self) -> None:
        bus = EventBus(max_queue_size=2, overflow_policy="drop_newest")
        sub = bus.subscribe("t")
        for i in range(4):
            bus.publish("t", i)
        assert [(await sub.get()).payload for _ in range(2)] == [0, 1]
        assert sub.dropped_count == 2

    async def test_drop_newest_reports_zero_delivered(self) -> None:
        bus = EventBus(max_queue_size=1, overflow_policy="drop_newest")
        bus.subscribe("t")
        assert bus.publish("t", 1) == 1
        assert bus.publish("t", 2) == 0  # queue đầy, drop

    async def test_publish_never_blocks_producer(self) -> None:
        """Chat flood không được làm nghẽn producer."""
        bus = EventBus(max_queue_size=5)
        bus.subscribe("chat")
        # 1000 event vào queue size 5 — phải xong ngay, không treo
        await asyncio.wait_for(
            asyncio.to_thread(lambda: [bus.publish("chat", i) for i in range(1000)]),
            timeout=5,
        )

    async def test_slow_subscriber_does_not_affect_fast_one(self) -> None:
        bus = EventBus(max_queue_size=2, overflow_policy="drop_oldest")
        slow = bus.subscribe("t")
        fast = bus.subscribe("t")
        bus.publish("t", 1)
        assert (await fast.get()).payload == 1   # fast đọc kịp
        for i in range(2, 6):
            bus.publish("t", i)
        # slow bị drop, fast vẫn đọc được event mới
        assert slow.dropped_count > 0
        assert fast.qsize() > 0


class TestMetrics:
    async def test_counts_published_and_dropped(self) -> None:
        bus = EventBus(max_queue_size=1, overflow_policy="drop_newest")
        bus.subscribe("t")
        bus.publish("t", 1)
        bus.publish("t", 2)  # drop
        m = bus.get_metrics()
        assert m["event_bus_published_total"] == 2
        assert m["event_bus_dropped_total"] == 1
        assert m["event_bus_subscribers"] == 1


class TestFromConfig:
    def test_reads_real_config(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        bus = EventBus.from_config(loader)
        assert bus._max_queue_size == 500
        assert bus._overflow_policy == "drop_oldest"
