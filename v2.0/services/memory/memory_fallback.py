"""MemoryFallbackManager — chain semantic (L0) → working (L1) (Phase 7.E).

Spec 8.7.6 fallback: SemanticMemoryService timeout hoặc empty → fallback về
WorkingMemoryService. Đây là orchestrator layer, IMPLEMENT MemoryService interface
để pipeline có thể swap in/out mà không đổi code call site.

Write policy: forward TỚI CẢ 2 tier — semantic persist DB, working giữ cache
recent (cho lần sau nếu semantic fail). Semantic write lỗi KHÔNG chặn working
(N7 partial success).

Query policy: semantic first, empty/error → working. Semantic đã fail-safe
trả [] khi timeout (7.D), nên "empty" cover cả timeout case.

Không dùng generic FallbackManager (0.D) vì:
- Write cần fan-out cả 2 tier, không phải chain
- Query chain đơn giản (2 level), không cần circuit breaker
"""
from __future__ import annotations

from typing import Any

from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry, MemoryService, MemoryTier
from orchestrator.logger import get_logger


class MemoryFallbackManager(MemoryService):
    service_id = "memory_fallback"

    def __init__(
        self,
        primary: MemoryService,     # SemanticMemoryService (7.D)
        fallback: MemoryService,    # WorkingMemoryService (7.E)
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._log = get_logger("memory_fallback")

        self._queries_primary_hit = 0
        self._queries_fallback_hit = 0
        self._writes_partial = 0     # semantic fail nhưng working thành công

    async def start(self) -> None:
        await self._primary.start()
        await self._fallback.start()

    async def stop(self) -> None:
        await self._primary.stop()
        await self._fallback.stop()

    async def health_check(self) -> HealthStatus:
        p = await self._primary.health_check()
        f = await self._fallback.health_check()
        if not p.is_ok and not f.is_ok:
            return HealthStatus.unhealthy(self.service_id, "cả 2 tier fail")
        return HealthStatus.healthy(
            self.service_id, primary_ok=p.is_ok, fallback_ok=f.is_ok,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "memory_fb_primary_hit": self._queries_primary_hit,
            "memory_fb_fallback_hit": self._queries_fallback_hit,
            "memory_fb_writes_partial": self._writes_partial,
            **self._primary.get_metrics(),
            **self._fallback.get_metrics(),
        }

    def fallback_snapshot(self) -> list[MemoryEntry]:
        """Expose recent fallback entries without leaking the wrapped service."""
        snapshot = getattr(self._fallback, "snapshot", None)
        if not callable(snapshot):
            return []
        try:
            value = snapshot()
            return list(value) if isinstance(value, (list, tuple)) else []
        except Exception:
            return []

    # ---------- MemoryService ----------

    async def write(self, entry: MemoryEntry) -> None:
        """Fan-out cả 2 tier. Primary fail → vẫn ghi fallback (N7 partial).

        Chỉ raise khi CẢ 2 fail (write không mất khả năng lưu context tối thiểu).
        """
        primary_err: Exception | None = None
        try:
            await self._primary.write(entry)
        except Exception as e:
            primary_err = e
            self._log.warning(
                "memory_primary_write_failed_fallback_only",
                error=str(e), entry_id=entry.entry_id,
            )
        try:
            await self._fallback.write(entry)
        except Exception as e:
            if primary_err is not None:
                raise  # cả 2 fail → propagate
            self._log.warning(
                "memory_fallback_write_failed",
                error=str(e), entry_id=entry.entry_id,
            )
            # primary OK, fallback fail → không raise (primary đã persist, đủ)
            return
        if primary_err is not None:
            self._writes_partial += 1

    async def query(
        self,
        query_text: str,
        top_k: int = 3,
        tier: MemoryTier | None = None,
        viewer_id: str | None = None,
    ) -> list[MemoryEntry]:
        """Semantic first, empty → working. Semantic timeout đã trả [] ở 7.D."""
        try:
            results = await self._primary.query(query_text, top_k, tier, viewer_id)
        except Exception as e:
            self._log.warning("memory_primary_query_error_fallback", error=str(e))
            results = []
        if results:
            self._queries_primary_hit += 1
            return results
        # empty (timeout hoặc thực sự không có) → fallback
        fallback = await self._fallback.query(query_text, top_k, tier, viewer_id)
        if fallback:
            self._queries_fallback_hit += 1
        return fallback

    async def forget(self, entry_id: str) -> None:
        # forget cả 2 tier, best-effort
        try:
            await self._primary.forget(entry_id)
        except Exception as e:
            self._log.warning("memory_primary_forget_failed", error=str(e))
        try:
            await self._fallback.forget(entry_id)
        except Exception as e:
            self._log.warning("memory_fallback_forget_failed", error=str(e))

    async def export_viewer(self, viewer_id: str) -> list[MemoryEntry]:
        primary = await self._primary.export_viewer(viewer_id)
        fallback = await self._fallback.export_viewer(viewer_id)
        merged = {entry.entry_id: entry for entry in (*primary, *fallback)}
        return [merged[key] for key in sorted(merged)]

    async def forget_viewer(self, viewer_id: str) -> int:
        """Privacy deletion is strict: both tiers must succeed."""
        primary_count = await self._primary.forget_viewer(viewer_id)
        fallback_count = await self._fallback.forget_viewer(viewer_id)
        return max(primary_count, fallback_count)
