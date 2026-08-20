"""Working and semantic memory service contract.

The implementation uses bounded deque working memory and sqlite-vec semantic memory.
Semantic timeout falls back to working-only memory without failing the turn.
"""
from __future__ import annotations

import math
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from interfaces.base import Service


class MemoryTier(str, Enum):
    WORKING = "working"
    SESSION = "session"
    PERSISTENT = "persistent"


@dataclass(frozen=True)
class MemoryEntry:
    entry_id: str
    content: str
    timestamp: datetime
    tags: tuple[str, ...] = ()
    importance: float = 0.5
    tier: MemoryTier = MemoryTier.WORKING
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_id", _required_text(self.entry_id, "entry_id"))
        object.__setattr__(self, "content", _required_text(self.content, "content"))
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))
        if not isinstance(self.tags, tuple):
            raise ValueError("tags must be a tuple")
        tags = tuple(_required_text(item, "tag") for item in self.tags)
        if len(tags) != len(set(tags)):
            raise ValueError("tags must be unique")
        object.__setattr__(self, "tags", tags)
        if isinstance(self.importance, bool) or not isinstance(self.importance, (int, float)):
            raise ValueError("importance must be numeric")
        importance = float(self.importance)
        if not math.isfinite(importance) or not 0.0 <= importance <= 1.0:
            raise ValueError("importance must be finite and between zero and one")
        object.__setattr__(self, "importance", importance)
        if not isinstance(self.tier, MemoryTier):
            raise ValueError("tier must be MemoryTier")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        frozen = _freeze_mapping(self.metadata)
        _validate_outcome_metadata(frozen)
        object.__setattr__(self, "metadata", frozen)


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
        viewer_id: str | None = None,
    ) -> list[MemoryEntry]:
        """Truy hồi entry liên quan. Timeout → trả list rỗng, không raise.

        `viewer_id` filter — chỉ trả entry của viewer đó (Phase 7.F multi-viewer).
        None = không lọc theo viewer (default).
        """

    @abstractmethod
    async def forget(self, entry_id: str) -> None:
        """Xoá 1 entry."""

    async def export_viewer(self, viewer_id: str) -> list[MemoryEntry]:
        """Return all entries for one pseudonymous viewer for privacy export."""
        return await self.query("", top_k=10000, viewer_id=viewer_id)

    async def forget_viewer(self, viewer_id: str) -> int:
        """Delete all entries for one pseudonymous viewer and return the count."""
        entries = await self.export_viewer(viewer_id)
        for entry in entries:
            await self.forget(entry.entry_id)
        return len(entries)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{name} must not be empty")
    return clean


def _freeze_mapping(value: Mapping[Any, Any]) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, "metadata key")
        if key in frozen:
            raise ValueError("metadata keys must be unique")
        frozen[key] = _freeze_value(raw_value)
    return MappingProxyType(frozen)


def _freeze_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata floats must be finite")
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    raise ValueError("metadata contains an unsupported value")


def _validate_outcome_metadata(metadata: Mapping[str, Any]) -> None:
    if "action_status" not in metadata:
        return
    status = metadata["action_status"]
    if status not in {"succeeded", "delivered", "failed", "cancelled", "timeout", "unknown"}:
        raise ValueError("metadata.action_status is invalid")
    if status in {"succeeded", "delivered"}:
        if metadata.get("verified") is not True:
            raise ValueError("successful action memory requires verified=true")
        _required_text(metadata.get("outcome_id"), "metadata.outcome_id")
        _required_text(metadata.get("provenance"), "metadata.provenance")
