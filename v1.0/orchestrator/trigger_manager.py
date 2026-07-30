"""Trigger Manager skeleton (ARCHITECTURE 7.9.2, Phase 0 task 8).

Phase 0 scope: classify 4 type, priority queue, spam detection, rate limit
chat_normal, ambient threshold. Số liệu từ `config/triggers.yaml` (N6).

CHƯA làm (Phase 2, PROCESS.md):
- Interrupt policy enforcement (7.9.3) — cần tích hợp state machine
- Ambient content generation (7.9.4) — cần LLM
- Viewer profile / priority boost (đã YAGNI-out ở 7.9.2)
"""
from __future__ import annotations

import heapq
import itertools
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from interfaces.base import HealthStatus
from interfaces.input import EventSource, InputEvent
from interfaces.trigger import (
    QueueStats,
    Trigger,
    TriggerAction,
    TriggerDecision,
    TriggerManagerInterface,
    TriggerType,
)
from orchestrator.logger import get_logger


class SimpleRateLimiter:
    """Sliding window: cho tối đa `max_events` trong `window_seconds`."""

    def __init__(self, window_seconds: float, max_events: int) -> None:
        self.window_seconds = window_seconds
        self.max_events = max_events
        self._events: deque[float] = deque()

    def check(self, now: float) -> bool:
        """True nếu còn quota (và consume 1 slot). False nếu bị chặn."""
        cutoff = now - self.window_seconds
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        if len(self._events) >= self.max_events:
            return False
        self._events.append(now)
        return True


class TriggerManager(TriggerManagerInterface):
    """Priority queue + classify + spam + rate limit + ambient threshold."""

    service_id = "trigger_manager"

    def __init__(
        self,
        priorities: dict[str, int],
        keywords: list[str],
        rate_window_s: float,
        rate_max: int,
        queue_max_size: int,
        default_ttl_s: int,
        ambient_enabled: bool,
        ambient_silence_s: int,
        spam_min_length: int,
        spam_patterns: list[str],
        event_bus: Any = None,
    ) -> None:
        self._priorities = priorities
        self._keywords = [k.lower() for k in keywords]
        self._rate_limiter = SimpleRateLimiter(rate_window_s, rate_max)
        self._queue_max_size = queue_max_size
        self._default_ttl_s = default_ttl_s
        self._ambient_enabled = ambient_enabled
        self._ambient_silence_s = ambient_silence_s
        self._spam_min_length = spam_min_length
        self._spam_patterns = [re.compile(p) for p in spam_patterns]
        self._event_bus = event_bus
        self._log = get_logger("trigger_manager")

        # Priority queue: heap of (-priority, seq, trigger). seq giữ FIFO trong
        # cùng priority; -priority vì heapq là min-heap còn ta muốn max-priority.
        self._heap: list[tuple[int, int, Trigger]] = []
        self._counter = itertools.count()
        self.last_speak_time = datetime.now(timezone.utc)

        self._dropped_total = 0
        self._expired_total = 0
        self._skipped_total = 0

    # ---------- factory ----------

    @classmethod
    def from_config(cls, loader, event_bus: Any = None) -> TriggerManager:
        p = loader.section("triggers").get("triggers", {})
        return cls(
            priorities=p.get("priorities", {}),
            keywords=p.get("chat_keywords", []),
            rate_window_s=float(p.get("rate_limits", {}).get("chat_normal", {}).get("window_seconds", 10)),
            rate_max=int(p.get("rate_limits", {}).get("chat_normal", {}).get("max_responses", 3)),
            queue_max_size=int(p.get("queue", {}).get("max_size", 30)),
            default_ttl_s=int(p.get("queue", {}).get("default_ttl_seconds", 30)),
            ambient_enabled=bool(p.get("ambient", {}).get("enabled", True)),
            ambient_silence_s=int(p.get("ambient", {}).get("min_silence_seconds", 60)),
            spam_min_length=int(p.get("spam", {}).get("min_length", 2)),
            spam_patterns=list(p.get("spam", {}).get("blocked_patterns", [])),
            event_bus=event_bus,
        )

    # ---------- Service ----------

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        size = len(self._heap)
        if size >= self._queue_max_size:
            return HealthStatus.degraded(self.service_id, "queue full", size=size)
        return HealthStatus.healthy(self.service_id, size=size)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "trigger_queue_size": len(self._heap),
            "trigger_dropped_total": self._dropped_total,
            "trigger_expired_total": self._expired_total,
            "trigger_skipped_total": self._skipped_total,
        }

    # ---------- classify (7.9.2) ----------

    def _classify(self, event: InputEvent) -> TriggerType:
        # MVP: operator điều khiển qua dashboard (text) → operator priority.
        # Voice operator (Phase 5) cũng map về đây.
        if event.source in (EventSource.VOICE_OPERATOR, EventSource.DASHBOARD):
            return TriggerType.OPERATOR_VOICE
        if event.source == EventSource.SYSTEM_TIMER:
            return TriggerType.AMBIENT_TALK
        text = event.content.lower()
        if any(kw in text for kw in self._keywords):
            return TriggerType.CHAT_MENTION
        return TriggerType.CHAT_NORMAL

    def _is_spam(self, event: InputEvent) -> bool:
        text = event.content.strip()
        if len(text) < self._spam_min_length:
            return True
        low = text.lower()
        return any(p.match(low) for p in self._spam_patterns)

    def _priority_for(self, ttype: TriggerType) -> int:
        return int(self._priorities.get(ttype.value, 0))

    # ---------- TriggerManagerInterface ----------

    async def process_event(self, event: InputEvent) -> TriggerDecision:
        ttype = self._classify(event)

        if self._is_spam(event):
            self._skipped_total += 1
            return TriggerDecision(action=TriggerAction.SKIP, reason="spam")

        # Rate limit CHỈ chat_normal (operator/mention luôn qua — 7.9.2)
        if ttype == TriggerType.CHAT_NORMAL:
            now = datetime.now(timezone.utc).timestamp()
            if not self._rate_limiter.check(now):
                self._skipped_total += 1
                return TriggerDecision(action=TriggerAction.SKIP, reason="rate_limited")

        priority = self._priority_for(ttype)
        trigger = Trigger(
            trigger_id=str(uuid.uuid4()),
            type=ttype,
            event=event,
            priority=priority,
            created_at=datetime.now(timezone.utc),
            ttl_seconds=self._default_ttl_s,
        )

        if not self._enqueue(trigger):
            return TriggerDecision(
                action=TriggerAction.SKIP, priority=priority, reason="queue_full_lower_priority"
            )

        if self._event_bus is not None:
            self._event_bus.publish(
                "trigger_queued", {"type": ttype.value, "priority": priority}
            )
        return TriggerDecision(
            action=TriggerAction.QUEUE, priority=priority, queue_position=len(self._heap)
        )

    def _enqueue(self, trigger: Trigger) -> bool:
        """Đẩy vào heap. Queue đầy → drop lowest priority (drop_policy)."""
        if len(self._heap) >= self._queue_max_size:
            # Phần tử priority thấp nhất = max của (-priority) = phần tử "lớn nhất" trong heap.
            lowest = max(self._heap)  # (-priority, seq, trigger): -prio lớn nhất = prio thấp nhất
            if -lowest[0] >= trigger.priority:
                # Trigger mới không cao hơn cái thấp nhất hiện có → drop trigger mới
                self._dropped_total += 1
                return False
            self._heap.remove(lowest)
            heapq.heapify(self._heap)
            self._dropped_total += 1
        heapq.heappush(self._heap, (-trigger.priority, next(self._counter), trigger))
        return True

    def _prune_expired(self) -> None:
        now = datetime.now(timezone.utc)
        kept = [item for item in self._heap if not item[2].is_expired(now)]
        expired = len(self._heap) - len(kept)
        if expired:
            self._expired_total += expired
            self._heap = kept
            heapq.heapify(self._heap)

    async def get_next_trigger(self) -> Trigger | None:
        self._prune_expired()
        if not self._heap:
            if self._should_ambient_talk():
                return self._create_ambient_trigger()
            return None
        _, _, trigger = heapq.heappop(self._heap)
        return trigger

    def _should_ambient_talk(self) -> bool:
        if not self._ambient_enabled:
            return False
        silence = (datetime.now(timezone.utc) - self.last_speak_time).total_seconds()
        return silence > self._ambient_silence_s

    def _create_ambient_trigger(self) -> Trigger:
        return Trigger(
            trigger_id=str(uuid.uuid4()),
            type=TriggerType.AMBIENT_TALK,
            event=InputEvent(
                event_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                source=EventSource.SYSTEM_TIMER,
                content="",
                metadata={"mode": "ambient"},
            ),
            priority=self._priority_for(TriggerType.AMBIENT_TALK),
            created_at=datetime.now(timezone.utc),
            ttl_seconds=self._default_ttl_s,
        )

    def mark_spoke(self) -> None:
        """Orchestrator gọi sau khi Mai nói xong → reset đồng hồ im lặng."""
        self.last_speak_time = datetime.now(timezone.utc)

    async def clear_queue(self, reason: str) -> None:
        cleared = len(self._heap)
        self._heap.clear()
        self._log.info("trigger_queue_cleared", reason=reason, cleared=cleared)

    async def get_queue_stats(self) -> QueueStats:
        by_type: dict[str, int] = {}
        for _, _, trigger in self._heap:
            by_type[trigger.type.value] = by_type.get(trigger.type.value, 0) + 1
        return QueueStats(
            size=len(self._heap),
            max_size=self._queue_max_size,
            by_type=by_type,
            dropped_total=self._dropped_total,
            expired_total=self._expired_total,
        )
