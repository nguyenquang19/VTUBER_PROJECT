"""Strict canonical configuration shared by the Phase 12 memory chain."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from interfaces.memory import MemoryEntry, MemoryTier


@dataclass(frozen=True)
class MemoryRuntimeConfig:
    working_maxlen: int
    query_timeout_s: float
    default_top_k: int
    max_query_top_k: int
    content_max_chars: int
    metadata_max_items: int
    metadata_text_max_chars: int
    tags_max: int
    tag_max_chars: int
    extractor_min_chars: int
    extractor_promote_intensity: int
    pending_writes_max: int

    def __post_init__(self) -> None:
        for name in (
            "working_maxlen", "default_top_k", "max_query_top_k",
            "content_max_chars", "metadata_max_items", "metadata_text_max_chars",
            "tags_max", "tag_max_chars", "extractor_min_chars", "pending_writes_max",
        ):
            _positive_int(getattr(self, name), f"memory.{name}")
        _positive_number(self.query_timeout_s, "memory.query_timeout_s")
        if self.default_top_k > self.max_query_top_k:
            raise ValueError("memory.default_top_k must not exceed max_query_top_k")
        if (
            isinstance(self.extractor_promote_intensity, bool)
            or not isinstance(self.extractor_promote_intensity, int)
            or not 0 <= self.extractor_promote_intensity <= 10
        ):
            raise ValueError("memory.extractor_promote_intensity must be an integer from 0 to 10")

    @classmethod
    def from_loader(cls, loader: Any) -> "MemoryRuntimeConfig":
        raw = loader.get("system", "memory", None)
        if not isinstance(raw, Mapping):
            raise ValueError("memory must be a mapping")
        expected = {
            "working_maxlen", "query_timeout_s", "default_top_k", "max_query_top_k",
            "content_max_chars", "metadata_max_items", "metadata_text_max_chars",
            "tags_max", "tag_max_chars", "extractor_min_chars",
            "extractor_promote_intensity", "pending_writes_max",
        }
        unknown = set(raw) - expected
        missing = expected - set(raw)
        if unknown:
            raise ValueError(f"memory contains unknown keys: {sorted(unknown)}")
        if missing:
            raise ValueError(f"memory is missing keys: {sorted(missing)}")
        return cls(**{name: raw[name] for name in expected})


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def validate_memory_query(
    query_text: Any,
    top_k: Any,
    tier: Any,
    viewer_id: Any,
    *,
    max_top_k: int,
) -> tuple[str, int, MemoryTier | None, str | None]:
    if not isinstance(query_text, str):
        raise ValueError("memory query_text must be a string")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= max_top_k:
        raise ValueError("memory top_k is outside the configured bound")
    if tier is not None and not isinstance(tier, MemoryTier):
        raise ValueError("memory tier filter must be MemoryTier")
    if viewer_id is not None:
        if not isinstance(viewer_id, str) or not viewer_id.strip():
            raise ValueError("memory viewer_id must be a non-empty string")
        viewer_id = viewer_id.strip()
    return query_text.strip(), top_k, tier, viewer_id


def validate_memory_entry(entry: Any, config: MemoryRuntimeConfig) -> MemoryEntry:
    if not isinstance(entry, MemoryEntry):
        raise ValueError("memory write requires MemoryEntry")
    if len(entry.content) > config.content_max_chars:
        raise ValueError("memory content exceeds configured bound")
    if len(entry.tags) > config.tags_max or any(
        len(tag) > config.tag_max_chars for tag in entry.tags
    ):
        raise ValueError("memory tags exceed configured bound")
    if _item_count(entry.metadata) > config.metadata_max_items:
        raise ValueError("memory metadata exceeds configured item bound")
    if _text_exceeds(entry.metadata, config.metadata_text_max_chars):
        raise ValueError("memory metadata text exceeds configured bound")
    return entry


def _item_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return len(value) + sum(_item_count(item) for item in value.values())
    if isinstance(value, tuple):
        return len(value) + sum(_item_count(item) for item in value)
    return 0


def _text_exceeds(value: Any, limit: int) -> bool:
    if isinstance(value, str):
        return len(value) > limit
    if isinstance(value, Mapping):
        return any(len(key) > limit or _text_exceeds(item, limit) for key, item in value.items())
    if isinstance(value, tuple):
        return any(_text_exceeds(item, limit) for item in value)
    return False
