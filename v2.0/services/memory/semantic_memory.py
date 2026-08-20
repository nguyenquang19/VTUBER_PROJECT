"""SemanticMemoryService joining the configured embedder and vector store.

Query latency is bounded at 150 ms; timeout falls back to working-only memory.
This is the primary service in the memory chain; its query timeout
fail-safe N7 trả list rỗng khi timeout (KHÔNG raise, KHÔNG giết pipeline).

Write không áp timeout (chậm không ảnh hưởng UX — write async background trong turn).
Embed + store đều sync → wrap asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry, MemoryService, MemoryTier
from orchestrator.logger import get_logger
from services.memory.embedder import BgeM3Embedder
from services.memory.config import MemoryRuntimeConfig, validate_memory_entry, validate_memory_query
from services.memory.sqlite_vec_store import SqliteVecStore, StoredEntry


class SemanticMemoryError(Exception):
    pass


class SemanticMemoryService(MemoryService):
    service_id = "memory"

    def __init__(
        self,
        store: SqliteVecStore,
        embedder: BgeM3Embedder,
        query_timeout_s: float = 0.15,   # DoD 150ms P95
        default_top_k: int = 3,
        config: MemoryRuntimeConfig | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        if config is None:
            config = MemoryRuntimeConfig(
                working_maxlen=20, query_timeout_s=query_timeout_s,
                default_top_k=default_top_k, max_query_top_k=20,
                content_max_chars=4000, metadata_max_items=24,
                metadata_text_max_chars=512, tags_max=12, tag_max_chars=64,
                extractor_min_chars=15, extractor_promote_intensity=7,
                pending_writes_max=64,
            )
        self._config = config
        self._timeout_s = config.query_timeout_s
        self._default_top_k = config.default_top_k
        self._log = get_logger("memory")

        self._writes_total = 0
        self._queries_total = 0
        self._timeouts_total = 0
        self._errors_total = 0
        self._rejected_total = 0
        self._duplicates_total = 0
        self._last_query_ms: float | None = None

    @classmethod
    def from_loader(
        cls,
        loader,
        store: SqliteVecStore | None = None,
        embedder: BgeM3Embedder | None = None,
    ) -> "SemanticMemoryService":
        """`store` và `embedder` inject để test / share instance. Nếu None,
        tự build từ config (semantic memory owns cả 2)."""
        if store is None:
            db_path = loader.get("system", "paths.db_file", "data/mai.db")
            store = SqliteVecStore(db_path=db_path)
        if embedder is None:
            embedder = BgeM3Embedder.from_loader(loader)
        return cls(store=store, embedder=embedder, config=MemoryRuntimeConfig.from_loader(loader))

    # ---------- Service lifecycle ----------

    async def start(self) -> None:
        if not self._embedder.is_loaded():
            await asyncio.to_thread(self._embedder.load)
        self._log.info(
            "memory_ready",
            store_count=await asyncio.to_thread(self._store.count),
            timeout_s=self._timeout_s,
        )

    async def stop(self) -> None:
        await asyncio.to_thread(self._store.close)
        self._embedder.clear_cache()

    async def health_check(self) -> HealthStatus:
        if not self._embedder.is_loaded():
            return HealthStatus.unhealthy(self.service_id, "embedder chưa load")
        return HealthStatus.healthy(self.service_id, timeout_s=self._timeout_s)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "memory_writes_total": self._writes_total,
            "memory_queries_total": self._queries_total,
            "memory_timeouts_total": self._timeouts_total,
            "memory_errors_total": self._errors_total,
            "memory_rejected_total": self._rejected_total,
            "memory_duplicates_total": self._duplicates_total,
            "memory_last_query_ms": self._last_query_ms,
            **self._embedder.get_metrics(),
        }

    # ---------- MemoryService API ----------

    async def write(self, entry: MemoryEntry) -> None:
        """Ghi 1 entry. Embed + insert đều sync → to_thread. Không áp timeout."""
        try:
            validate_memory_entry(entry, self._config)
        except ValueError:
            self._rejected_total += 1
            raise
        fetch = getattr(self._store, "fetch_by_id", None)
        if callable(fetch):
            existing = await asyncio.to_thread(fetch, entry.entry_id)
            if existing is not None:
                if not _stored_matches(existing, entry):
                    self._rejected_total += 1
                    raise ValueError("memory entry_id collision has different content")
                self._duplicates_total += 1
                return
        try:
            vec = await asyncio.to_thread(self._embedder.embed, entry.content)
            await asyncio.to_thread(
                self._store.insert,
                entry.entry_id,
                entry.content,
                vec,
                timestamp=entry.timestamp,
                tier=entry.tier.value,
                importance=entry.importance,
                tags=entry.tags,
                metadata=entry.metadata,
                viewer_id=entry.metadata.get("viewer_id"),
                session_id=entry.metadata.get("session_id"),
            )
            self._writes_total += 1
        except Exception as e:
            self._errors_total += 1
            self._log.error("memory_write_failed", error=str(e), entry_id=entry.entry_id)
            raise SemanticMemoryError(f"write failed: {e}") from e

    async def query(
        self,
        query_text: str,
        top_k: int = 3,
        tier: MemoryTier | None = None,
        viewer_id: str | None = None,
    ) -> list[MemoryEntry]:
        """Retrieve top_k. Hard timeout 150ms → fail-safe trả []."""
        try:
            query_text, top_k, tier, viewer_id = validate_memory_query(
                query_text, top_k, tier, viewer_id,
                max_top_k=self._config.max_query_top_k,
            )
        except ValueError:
            self._rejected_total += 1
            raise
        self._queries_total += 1
        t0 = time.perf_counter()
        try:
            results = await asyncio.wait_for(
                self._retrieve(query_text, top_k, tier, viewer_id),
                timeout=self._timeout_s,
            )
            self._last_query_ms = (time.perf_counter() - t0) * 1000
            return results
        except asyncio.TimeoutError:
            self._timeouts_total += 1
            self._last_query_ms = self._timeout_s * 1000
            self._log.warning(
                "memory_query_timeout",
                query=query_text[:50], timeout_s=self._timeout_s,
            )
            return []  # N7 fail-safe: pipeline tiếp tục với working-only (7.E)
        except Exception as e:
            self._errors_total += 1
            self._last_query_ms = (time.perf_counter() - t0) * 1000
            self._log.error("memory_query_failed", error=str(e))
            return []  # fail-safe

    async def _retrieve(
        self,
        query_text: str,
        top_k: int,
        tier: MemoryTier | None,
        viewer_id: str | None,
    ) -> list[MemoryEntry]:
        vec = await asyncio.to_thread(self._embedder.embed, query_text)
        tier_str = tier.value if tier else None
        stored = await asyncio.to_thread(
            self._store.query_knn, vec, top_k, tier=tier_str, viewer_id=viewer_id,
        )
        entries: list[MemoryEntry] = []
        for item in stored:
            try:
                entries.append(_stored_to_entry(item))
            except (TypeError, ValueError):
                self._rejected_total += 1
        return entries

    async def forget(self, entry_id: str) -> None:
        entry_id = _required_identity(entry_id, "entry_id")
        try:
            deleted = await asyncio.to_thread(self._store.delete, entry_id)
            if not deleted:
                self._log.info("memory_forget_noop", entry_id=entry_id)
        except Exception as e:
            self._errors_total += 1
            self._log.error("memory_forget_failed", error=str(e), entry_id=entry_id)
            raise SemanticMemoryError(f"forget failed: {e}") from e

    async def export_viewer(self, viewer_id: str) -> list[MemoryEntry]:
        viewer_id = _required_identity(viewer_id, "viewer_id")
        try:
            stored = await asyncio.to_thread(self._store.list_by_viewer, viewer_id)
            entries: list[MemoryEntry] = []
            for item in stored:
                try:
                    entries.append(_stored_to_entry(item))
                except (TypeError, ValueError):
                    self._rejected_total += 1
            return entries
        except Exception as e:
            self._errors_total += 1
            raise SemanticMemoryError(f"viewer export failed: {e}") from e

    async def forget_viewer(self, viewer_id: str) -> int:
        viewer_id = _required_identity(viewer_id, "viewer_id")
        try:
            return await asyncio.to_thread(self._store.delete_by_viewer, viewer_id)
        except Exception as e:
            self._errors_total += 1
            raise SemanticMemoryError(f"viewer forget failed: {e}") from e


# ---------- helpers ----------


def _stored_to_entry(s: StoredEntry) -> MemoryEntry:
    """Convert a legacy-compatible store row into the immutable memory contract.

    distance của StoredEntry được nhét vào metadata để caller inspect ranking.
    viewer_id/session_id cũng gom vào metadata (interface không có field riêng).
    """
    meta = dict(s.metadata)
    if s.viewer_id is not None:
        meta["viewer_id"] = s.viewer_id
    if s.session_id is not None:
        meta["session_id"] = s.session_id
    if s.distance is not None:
        meta["distance"] = s.distance
    return MemoryEntry(
        entry_id=s.entry_id,
        content=s.content,
        timestamp=s.timestamp,
        tags=tuple(s.tags),
        importance=s.importance,
        tier=MemoryTier(s.tier),
        metadata=meta,
    )


def new_entry_id() -> str:
    """Helper sinh entry_id UUID4 hex (auto-inject vào MemoryEntry khi caller cần)."""
    return uuid.uuid4().hex


def _required_identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"memory {name} must be a non-empty string")
    return value.strip()


def _stored_matches(stored: StoredEntry, entry: MemoryEntry) -> bool:
    timestamp = stored.timestamp
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (
        stored.content == entry.content
        and timestamp.astimezone(timezone.utc) == entry.timestamp
        and stored.tier == entry.tier.value
        and float(stored.importance) == entry.importance
        and tuple(stored.tags) == entry.tags
        and dict(stored.metadata) == dict(entry.metadata)
    )
