"""Memory interface (ARCHITECTURE 7.8).

Implementation working memory (deque) + semantic (sqlite-vec) ở Phase 7.
Memory timeout → fallback working-only, soft fail (spec 8.7.6).
"""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from interfaces.base import Service


class MemoryTier(str, Enum):
    WORKING = "working"
    SESSION = "session"
    PERSISTENT = "persistent"


class MemoryEntry(BaseModel):
    entry_id: str
    content: str
    timestamp: datetime
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(0.5, ge=0.0, le=1.0)
    tier: MemoryTier = MemoryTier.WORKING
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryService(Service):
    @abstractmethod
    async def write(self, entry: MemoryEntry) -> None:
        """Ghi 1 entry."""

    @abstractmethod
    async def query(
        self,
        query_text: str,
        top_k: int = 3,
        tier: MemoryTier | None = None,
    ) -> list[MemoryEntry]:
        """Truy hồi entry liên quan. Timeout → trả list rỗng, không raise."""

    @abstractmethod
    async def forget(self, entry_id: str) -> None:
        """Xoá 1 entry."""
