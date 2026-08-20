"""Typed input-source service contract."""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from interfaces.base import Service


class EventSource(str, Enum):
    CHAT_TWITCH = "chat_twitch"
    CHAT_YOUTUBE = "chat_youtube"
    CHAT_DISCORD = "chat_discord"    # Phase Platform.B
    VOICE_OPERATOR = "voice_operator"
    SYSTEM_TIMER = "system_timer"
    DASHBOARD = "dashboard"


class InputEvent(BaseModel):
    event_id: str
    timestamp: datetime
    source: EventSource
    user_id: str | None = None
    user_name: str | None = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class InputService(Service):
    @abstractmethod
    def event_stream(self) -> AsyncIterator[InputEvent]:
        """Yield input events as they arrive."""
