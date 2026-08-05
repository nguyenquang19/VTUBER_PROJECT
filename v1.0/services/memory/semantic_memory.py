"""SemanticMemoryService — impl MemoryService, wire embedder + store (Phase 7.D).

DoD Phase 7: query <150ms P95, timeout → fallback (7.E) trả working-only.
Đây là service PRIMARY của memory chain — timeout hard 150ms trên query,
fail-safe N7 trả list rỗng khi timeout (KHÔNG raise, KHÔNG giết pipeline).

Write không áp timeout (chậm không ảnh hưởng UX — write async background trong turn).
Embed + store đều sync → wrap asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry, MemoryService, MemoryTier
from orchestrator.logger import get_logger
from services.memory.embedder import BgeM3Embedder
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
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._timeout_s = query_timeout_s
        self._default_top_k = default_top_k
        self._log = get_logger("memory")

        self._writes_total = 0
        self._queries_total = 0
        self._timeouts_total = 0
        self._errors_total = 0
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
        return cls(
            store=store,
            embedder=embedder,
            query_timeout_s=float(loader.get(
                "system", "memory.query_timeout_s", 0.15
            )),
            default_top_k=int(loader.get(
                "system", "memory.default_top_k", 3
            )),
        )

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
            "memory_last_query_ms": self._last_query_ms,
            **self._embedder.get_metrics(),
        }

    # ---------- MemoryService API ----------

    async def write(self, entry: MemoryEntry) -> None:
        """Ghi 1 entry. Embed + insert đều sync → to_thread. Không áp timeout."""
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
        return [_stored_to_entry(s) for s in stored]

    async def forget(self, entry_id: str) -> None:
        try:
            deleted = await asyncio.to_thread(self._store.delete, entry_id)
            if not deleted:
                self._log.info("memory_forget_noop", entry_id=entry_id)
        except Exception as e:
            self._errors_total += 1
            self._log.error("memory_forget_failed", error=str(e), entry_id=entry_id)
            raise SemanticMemoryError(f"forget failed: {e}") from e


# ---------- helpers ----------


def _stored_to_entry(s: StoredEntry) -> MemoryEntry:
    """Convert StoredEntry (store dataclass) → MemoryEntry (interface pydantic).

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
        tags=list(s.tags),
        importance=s.importance,
        tier=MemoryTier(s.tier),
        metadata=meta,
    )


def new_entry_id() -> str:
    """Helper sinh entry_id UUID4 hex (auto-inject vào MemoryEntry khi caller cần)."""
    return uuid.uuid4().hex
