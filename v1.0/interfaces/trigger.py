"""Trigger & turn-taking interface (ARCHITECTURE 7.9.5).

N1 YAGNI: đúng 4 trigger type (7.9.1), không thêm donation/subscribe/question/...
"""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from interfaces.base import Service
from interfaces.input import InputEvent


class TriggerType(str, Enum):
    OPERATOR_VOICE = "operator_voice"   # priority 100 (MVP: operator text từ dashboard)
    CHAT_MENTION = "chat_mention"       # priority 60 (gọi tên Mai)
    CHAT_NORMAL = "chat_normal"         # priority 30 (chat thường)
    AMBIENT_TALK = "ambient_talk"       # priority 10 (Mai tự nói khi im lặng)


class TriggerAction(str, Enum):
    RESPOND = "respond"
    QUEUE = "queue"
    SKIP = "skip"
    INTERRUPT_CURRENT = "interrupt_current"


class TriggerDecision(BaseModel):
    action: TriggerAction
    priority: int | None = None
    reason: str = ""
    queue_position: int | None = None


class Trigger(BaseModel):
    trigger_id: str
    type: TriggerType
    event: InputEvent
    priority: int
    created_at: datetime
    ttl_seconds: int = 30
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, now: datetime) -> bool:
        return (now - self.created_at).total_seconds() > self.ttl_seconds


class QueueStats(BaseModel):
    size: int
    max_size: int
    by_type: dict[str, int] = Field(default_factory=dict)
    dropped_total: int = 0
    expired_total: int = 0


class TriggerManagerInterface(Service):
    @abstractmethod
    async def process_event(self, event: InputEvent) -> TriggerDecision:
        """Classify + spam/rate check → QUEUE hoặc SKIP."""

    @abstractmethod
    async def get_next_trigger(self) -> Trigger | None:
        """Lấy trigger ưu tiên cao nhất; queue rỗng + im lặng lâu → ambient."""

    @abstractmethod
    async def clear_queue(self, reason: str) -> None:
        """Xoá queue (vd khi emergency stop)."""

    @abstractmethod
    async def get_queue_stats(self) -> QueueStats:
        """Thống kê queue cho dashboard."""
