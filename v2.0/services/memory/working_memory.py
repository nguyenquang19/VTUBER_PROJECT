"""WorkingMemoryService — in-memory deque 20 entries (Phase 7.E).

Buffer NGẮN HẠN cho recent turns. KHÔNG persist (không đụng SQLite/embed).
Dùng làm fallback L1 khi SemanticMemoryService timeout (spec 8.7.6).

Query: trả N entry mới nhất theo filter tier/viewer_id (không semantic search).
Đủ dùng cho fallback vì working memory bản chất là RECENT context, không cần
"relevant" — mục đích chỉ để pipeline có gì đưa vào prompt khi semantic fail.

Fast (<1ms/op), thread-safe không cần (asyncio single-threaded).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry, MemoryService, MemoryTier
from orchestrator.logger import get_logger
from services.memory.config import MemoryRuntimeConfig, validate_memory_entry, validate_memory_query


class WorkingMemoryService(MemoryService):
    service_id = "memory_working"

    def __init__(
        self,
        maxlen: int = 20,
        *,
        config: MemoryRuntimeConfig | None = None,
    ) -> None:
        if config is None:
            config = MemoryRuntimeConfig(
                working_maxlen=maxlen, query_timeout_s=0.15, default_top_k=3,
                semantic_max_entries=10000,
                latency_sample_max=256,
                max_query_top_k=20, content_max_chars=4000, metadata_max_items=24,
                metadata_text_max_chars=512, tags_max=12, tag_max_chars=64,
                extractor_min_chars=15, extractor_promote_intensity=7,
                pending_writes_max=64,
            )
        self._config = config
        self.maxlen = config.working_maxlen
        self._buf: deque[MemoryEntry] = deque(maxlen=self.maxlen)
        self._log = get_logger("memory_working")

        self._writes_total = 0
        self._queries_total = 0
        self._evictions_total = 0
        self._duplicates_total = 0
        self._rejected_total = 0

    @classmethod
    def from_loader(cls, loader) -> "WorkingMemoryService":
        config = MemoryRuntimeConfig.from_loader(loader)
        return cls(config=config)

    # ---------- Service ----------

    async def start(self) -> None:
        self._log.info("working_memory_ready", maxlen=self.maxlen)

    async def stop(self) -> None:
        self._buf.clear()

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(
            self.service_id, size=len(self._buf), maxlen=self.maxlen,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "working_writes_total": self._writes_total,
            "working_queries_total": self._queries_total,
            "working_evictions_total": self._evictions_total,
            "working_duplicates_total": self._duplicates_total,
            "working_rejected_total": self._rejected_total,
            "working_size": len(self._buf),
        }

    # ---------- MemoryService ----------

    async def write(self, entry: MemoryEntry) -> None:
        try:
            validate_memory_entry(entry, self._config)
        except ValueError:
            self._rejected_total += 1
            raise
        existing = next(
            (item for item in self._buf if item.entry_id == entry.entry_id), None,
        )
        if existing is not None:
            if existing != entry:
                self._rejected_total += 1
                raise ValueError("memory entry_id collision has different content")
            self._duplicates_total += 1
            return
        if len(self._buf) == self.maxlen:
            self._evictions_total += 1
        self._buf.append(entry)
        self._writes_total += 1

    async def query(
        self,
        query_text: str,
        top_k: int = 3,
        tier: MemoryTier | None = None,
        viewer_id: str | None = None,
    ) -> list[MemoryEntry]:
        """Trả top_k entry MỚI NHẤT (LIFO), lọc tier/viewer_id nếu có.

        query_text ignored — working memory không semantic search, chỉ recent.
        """
        query_text, top_k, tier, viewer_id = validate_memory_query(
            query_text, top_k, tier, viewer_id,
            max_top_k=self._config.max_query_top_k,
        )
        self._queries_total += 1
        # reversed = mới nhất trước
        it = reversed(self._buf)
        if tier is not None:
            it = (e for e in it if e.tier == tier)
        if viewer_id is not None:
            it = (e for e in it if e.metadata.get("viewer_id") == viewer_id)
        return list(_take(it, top_k))

    async def forget(self, entry_id: str) -> None:
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("memory entry_id must be a non-empty string")
        entry_id = entry_id.strip()
        # deque không có delete-by-value hiệu quả, rebuild
        remaining = [e for e in self._buf if e.entry_id != entry_id]
        self._buf.clear()
        self._buf.extend(remaining)

    async def export_viewer(self, viewer_id: str) -> list[MemoryEntry]:
        if not isinstance(viewer_id, str) or not viewer_id.strip():
            raise ValueError("memory viewer_id must be a non-empty string")
        viewer_id = viewer_id.strip()
        return [entry for entry in self._buf if entry.metadata.get("viewer_id") == viewer_id]

    async def forget_viewer(self, viewer_id: str) -> int:
        if not isinstance(viewer_id, str) or not viewer_id.strip():
            raise ValueError("memory viewer_id must be a non-empty string")
        viewer_id = viewer_id.strip()
        entries = await self.export_viewer(viewer_id)
        remove_ids = {entry.entry_id for entry in entries}
        remaining = [entry for entry in self._buf if entry.entry_id not in remove_ids]
        self._buf.clear()
        self._buf.extend(remaining)
        return len(entries)

    # ---------- extras ----------

    def snapshot(self) -> list[MemoryEntry]:
        """Trả toàn bộ buffer hiện tại (mới nhất cuối). Dùng cho debug/dashboard."""
        return list(self._buf)


def _take(it, n: int):
    for i, x in enumerate(it):
        if i >= n:
            return
        yield x
