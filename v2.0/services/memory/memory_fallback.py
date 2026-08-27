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

import contextlib
from dataclasses import replace
from typing import Any

from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry, MemoryService, MemoryTier
from orchestrator.logger import get_logger
from services.data.sanitize import hash_viewer_id
from services.memory.config import validate_memory_query


class MemoryFallbackManager(MemoryService):
    service_id = "memory_fallback"

    def __init__(
        self,
        primary: MemoryService | None,  # SemanticMemoryService; None = flag-off
        fallback: MemoryService,    # WorkingMemoryService (7.E)
        max_query_top_k: int = 20,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        if isinstance(max_query_top_k, bool) or not isinstance(max_query_top_k, int) or max_query_top_k <= 0:
            raise ValueError("memory max_query_top_k must be a positive integer")
        self._max_query_top_k = max_query_top_k
        self._log = get_logger("memory_fallback")
        self._primary_enabled = primary is not None
        self._primary_available = primary is not None

        self._queries_primary_hit = 0
        self._queries_fallback_hit = 0
        self._queries_fallback_miss = 0
        self._queries_fallback_total = 0
        self._writes_partial = 0     # semantic fail nhưng working thành công
        self._query_rejected = 0
        self._primary_start_failures = 0

    async def start(self) -> None:
        await self._fallback.start()
        if self._primary is None:
            return
        try:
            await self._primary.start()
        except Exception as exc:
            self._primary_start_failures += 1
            self._primary_available = False
            with contextlib.suppress(Exception):
                await self._primary.stop()
            self._log.warning(
                "memory_primary_start_failed_working_only",
                error=type(exc).__name__,
            )
        else:
            self._primary_available = True

    async def stop(self) -> None:
        if self._primary is not None:
            with contextlib.suppress(Exception):
                await self._primary.stop()
        self._primary_available = False
        await self._fallback.stop()

    async def health_check(self) -> HealthStatus:
        f = await self._fallback.health_check()
        if not f.is_ok:
            return HealthStatus.unhealthy(self.service_id, "working memory fail")
        primary_ok = False
        if self._primary is not None and self._primary_available:
            try:
                primary_ok = (await self._primary.health_check()).is_ok
            except Exception:
                primary_ok = False
        return HealthStatus.healthy(
            self.service_id,
            primary_enabled=self._primary_enabled,
            primary_ok=primary_ok,
            fallback_ok=True,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "memory_fb_primary_hit": self._queries_primary_hit,
            "memory_fb_fallback_hit": self._queries_fallback_hit,
            "memory_fb_fallback_miss": self._queries_fallback_miss,
            "memory_fb_fallback_total": self._queries_fallback_total,
            "memory_fb_writes_partial": self._writes_partial,
            "memory_fb_query_rejected": self._query_rejected,
            "memory_fb_primary_enabled": self._primary_enabled,
            "memory_fb_primary_available": self._primary_available,
            "memory_fb_primary_start_failures": self._primary_start_failures,
            **(self._primary.get_metrics() if self._primary is not None else {}),
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
        entry = _entry_with_pseudonymous_viewer(entry)
        primary_err: Exception | None = None
        if self._primary is not None and self._primary_available:
            try:
                await self._primary.write(entry)
            except Exception as e:
                primary_err = e
                self._log.warning(
                    "memory_primary_write_failed_fallback_only",
                    error=type(e).__name__, entry_id=entry.entry_id,
                )
        elif self._primary_enabled:
            primary_err = RuntimeError("semantic memory unavailable")
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
            query_text, top_k, tier, viewer_id = validate_memory_query(
                query_text, top_k, tier, viewer_id, max_top_k=self._max_query_top_k,
            )
        except ValueError:
            self._query_rejected += 1
            raise
        viewer_id = _pseudonymous_viewer_id(viewer_id)
        results: list[MemoryEntry] = []
        if self._primary is not None and self._primary_available:
            try:
                results = await self._primary.query(query_text, top_k, tier, viewer_id)
            except Exception as e:
                self._log.warning(
                    "memory_primary_query_error_fallback", error=type(e).__name__,
                )
        if results:
            self._queries_primary_hit += 1
            return results
        # empty (timeout hoặc thực sự không có) → fallback
        self._queries_fallback_total += 1
        fallback = await self._fallback.query(query_text, top_k, tier, viewer_id)
        if fallback:
            self._queries_fallback_hit += 1
        else:
            self._queries_fallback_miss += 1
        return fallback

    async def forget(self, entry_id: str) -> None:
        # forget cả 2 tier, best-effort
        if self._primary is not None:
            try:
                await self._primary.forget(entry_id)
            except Exception as e:
                self._log.warning("memory_primary_forget_failed", error=type(e).__name__)
        try:
            await self._fallback.forget(entry_id)
        except Exception as e:
            self._log.warning("memory_fallback_forget_failed", error=str(e))

    async def export_viewer(self, viewer_id: str) -> list[MemoryEntry]:
        viewer_id = _required_pseudonymous_viewer_id(viewer_id)
        primary = (
            await self._primary.export_viewer(viewer_id)
            if self._primary is not None else []
        )
        fallback = await self._fallback.export_viewer(viewer_id)
        merged = {entry.entry_id: entry for entry in (*primary, *fallback)}
        return [merged[key] for key in sorted(merged)]

    async def forget_viewer(self, viewer_id: str) -> int:
        """Privacy deletion is strict: both tiers must succeed."""
        viewer_id = _required_pseudonymous_viewer_id(viewer_id)
        primary_count = (
            await self._primary.forget_viewer(viewer_id)
            if self._primary is not None else 0
        )
        fallback_count = await self._fallback.forget_viewer(viewer_id)
        return max(primary_count, fallback_count)


def _entry_with_pseudonymous_viewer(entry: MemoryEntry) -> MemoryEntry:
    if not isinstance(entry, MemoryEntry):
        raise ValueError("memory write requires MemoryEntry")
    viewer_id = entry.metadata.get("viewer_id")
    if viewer_id is None:
        return entry
    pseudonym = _required_pseudonymous_viewer_id(viewer_id)
    if pseudonym == viewer_id:
        return entry
    metadata = dict(entry.metadata)
    metadata["viewer_id"] = pseudonym
    return replace(entry, metadata=metadata)


def _pseudonymous_viewer_id(viewer_id: str | None) -> str | None:
    if viewer_id is None:
        return None
    return _required_pseudonymous_viewer_id(viewer_id)


def _required_pseudonymous_viewer_id(viewer_id: Any) -> str:
    if not isinstance(viewer_id, str) or not viewer_id.strip():
        raise ValueError("memory viewer_id must be a non-empty string")
    clean = viewer_id.strip()
    if clean.startswith("v_"):
        return clean
    pseudonym = hash_viewer_id(clean)
    if pseudonym is None:
        raise ValueError("memory viewer_id could not be pseudonymized")
    return pseudonym
