"""Event bus nội bộ: asyncio queue pub/sub (ARCHITECTURE 8.1, Phase 0 task 5).

Fan-out: mỗi subscriber có queue riêng, publish copy vào tất cả queue của topic.
Queue có bound — đầy thì drop theo `overflow_policy` chứ không block producer
(chat flood không được làm nghẽn LLM/TTS).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from orchestrator.logger import get_logger

#: topic đặc biệt: subscriber nhận MỌI event (dùng cho dashboard/log)
TOPIC_ALL = "*"


@dataclass
class Event:
    topic: str
    payload: Any
    metadata: dict[str, Any] = field(default_factory=dict)


class Subscription:
    """Handle của 1 subscriber. Iterate bằng `async for`."""

    def __init__(self, bus: EventBus, topic: str, max_queue_size: int) -> None:
        self._bus = bus
        self.topic = topic
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=max_queue_size)
        self._closed = False
        self.dropped_count = 0

    def _deliver(self, event: Event, overflow_policy: str) -> bool:
        """Đưa event vào queue. False nếu bị drop."""
        if self._closed:
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            if overflow_policy == "drop_oldest":
                try:
                    self._queue.get_nowait()          # bỏ event cũ nhất
                    self._queue.put_nowait(event)
                    self.dropped_count += 1
                    return True
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    self.dropped_count += 1
                    return False
            # drop_newest
            self.dropped_count += 1
            return False

    async def get(self) -> Event | None:
        """Lấy event kế tiếp. None nghĩa là subscription đã đóng."""
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)
        await self._queue.put(None)  # đánh thức consumer đang chờ

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class EventBus:
    def __init__(self, max_queue_size: int = 500, overflow_policy: str = "drop_oldest") -> None:
        self._subs: dict[str, list[Subscription]] = {}
        self._max_queue_size = max_queue_size
        self._overflow_policy = overflow_policy
        self._log = get_logger("event_bus")
        self._published_count = 0
        self._dropped_count = 0

    @classmethod
    def from_config(cls, loader) -> EventBus:
        return cls(
            max_queue_size=int(loader.get("system", "event_bus.max_queue_size", 500)),
            overflow_policy=str(loader.get("system", "event_bus.overflow_policy", "drop_oldest")),
        )

    def subscribe(self, topic: str) -> Subscription:
        """Đăng ký nhận event của `topic`. Dùng TOPIC_ALL để nhận mọi topic."""
        sub = Subscription(self, topic, self._max_queue_size)
        self._subs.setdefault(topic, []).append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.topic)
        if not subs:
            return
        try:
            subs.remove(sub)
        except ValueError:
            return
        if not subs:
            del self._subs[sub.topic]

    def publish(self, topic: str, payload: Any = None, **metadata: Any) -> int:
        """Publish event. Trả số subscriber nhận được (không tính bị drop).

        Sync (không await) để caller ở mọi ngữ cảnh gọi được — put_nowait không block.
        """
        event = Event(topic=topic, payload=payload, metadata=metadata)
        delivered = 0
        for target in (topic, TOPIC_ALL):
            for sub in list(self._subs.get(target, ())):
                if sub._deliver(event, self._overflow_policy):
                    delivered += 1
                else:
                    self._dropped_count += 1
        self._published_count += 1
        return delivered

    def subscriber_count(self, topic: str | None = None) -> int:
        if topic is None:
            return sum(len(v) for v in self._subs.values())
        return len(self._subs.get(topic, ()))

    def topics(self) -> list[str]:
        return sorted(self._subs)

    def get_metrics(self) -> dict[str, int]:
        return {
            "event_bus_published_total": self._published_count,
            "event_bus_dropped_total": self._dropped_count,
            "event_bus_subscribers": self.subscriber_count(),
        }

    async def close(self) -> None:
        """Đóng mọi subscription."""
        for subs in list(self._subs.values()):
            for sub in list(subs):
                await sub.close()
        self._subs.clear()
