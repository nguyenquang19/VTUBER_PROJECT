"""Test SemanticMemoryService — Phase 7.D.

Dùng FakeStore + FakeEmbedder (không cần SQLite/bge-m3). Test hard timeout
150ms không raise (fail-safe N7 → return []).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from interfaces.memory import MemoryEntry, MemoryTier
from services.memory.semantic_memory import (
    SemanticMemoryError,
    SemanticMemoryService,
    new_entry_id,
)
from services.memory.sqlite_vec_store import StoredEntry

REPO_ROOT = Path(__file__).resolve().parents[2]

DIM = 1024


class FakeEmbedder:
    def __init__(self, embed_delay_s: float = 0.0, raise_on_call: bool = False) -> None:
        self.embed_delay_s = embed_delay_s
        self.raise_on_call = raise_on_call
        self.embed_calls: list[str] = []
        self._loaded = True

    def is_loaded(self) -> bool: return self._loaded
    def load(self) -> None: self._loaded = True
    def clear_cache(self) -> None: pass
    def get_metrics(self) -> dict: return {"embedder_calls_total": len(self.embed_calls)}

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        if self.raise_on_call:
            raise RuntimeError("embed boom")
        if self.embed_delay_s:
            time.sleep(self.embed_delay_s)
        return [hash(text) % 100 / 100.0] * DIM


class FakeStore:
    def __init__(self, query_delay_s: float = 0.0, raise_on_query: bool = False) -> None:
        self.query_delay_s = query_delay_s
        self.raise_on_query = raise_on_query
        self.inserts: list[tuple] = []
        self.deletes: list[str] = []
        self._entries: dict[str, StoredEntry] = {}
        self.knn_results: list[StoredEntry] = []
        self.knn_calls: list[tuple] = []

    def insert(self, entry_id, content, embedding, **kw):
        self.inserts.append((entry_id, content, embedding, kw))
        self._entries[entry_id] = StoredEntry(
            entry_id=entry_id, content=content,
            timestamp=kw.get("timestamp", datetime.now(timezone.utc)),
            tier=kw.get("tier", "persistent"),
            importance=kw.get("importance", 0.5),
            tags=list(kw.get("tags", [])),
            metadata=dict(kw.get("metadata", {})),
            viewer_id=kw.get("viewer_id"),
            session_id=kw.get("session_id"),
        )

    def fetch_by_id(self, entry_id):
        return self._entries.get(entry_id)

    def query_knn(self, embedding, top_k, tier=None, viewer_id=None):
        self.knn_calls.append((embedding, top_k, tier, viewer_id))
        if self.raise_on_query:
            raise RuntimeError("knn boom")
        if self.query_delay_s:
            time.sleep(self.query_delay_s)
        return list(self.knn_results)[:top_k]

    def delete(self, entry_id): self.deletes.append(entry_id); return entry_id in self._entries
    def close(self): pass
    def count(self, tier=None): return len(self._entries)


@pytest.fixture
def svc():
    return SemanticMemoryService(
        store=FakeStore(),
        embedder=FakeEmbedder(),
        query_timeout_s=0.15,
    )


def make_entry(content: str = "hello", tier: MemoryTier = MemoryTier.PERSISTENT, **over) -> MemoryEntry:
    kw = dict(
        entry_id=new_entry_id(),
        content=content,
        timestamp=datetime.now(timezone.utc),
        tier=tier,
    )
    kw.update(over)
    return MemoryEntry(**kw)


def make_stored(entry_id: str = "m1", content: str = "c1", **over) -> StoredEntry:
    kw = dict(
        entry_id=entry_id, content=content,
        timestamp=datetime.now(timezone.utc),
        tier="persistent", importance=0.5,
        tags=[], metadata={},
        viewer_id=None, session_id=None,
        distance=0.1,
    )
    kw.update(over)
    return StoredEntry(**kw)


class TestLifecycle:
    async def test_start_health_ok(self, svc: SemanticMemoryService) -> None:
        await svc.start()
        h = await svc.health_check()
        assert h.is_ok is True

    async def test_health_unhealthy_when_embedder_not_loaded(self) -> None:
        emb = FakeEmbedder(); emb._loaded = False
        svc = SemanticMemoryService(store=FakeStore(), embedder=emb)
        h = await svc.health_check()
        assert h.is_ok is False

    async def test_start_loads_embedder(self) -> None:
        emb = FakeEmbedder(); emb._loaded = False
        svc = SemanticMemoryService(store=FakeStore(), embedder=emb)
        await svc.start()
        assert emb.is_loaded() is True


class TestWrite:
    async def test_write_embeds_and_inserts(self, svc: SemanticMemoryService) -> None:
        e = make_entry("Mai thích cà phê sữa")
        await svc.write(e)
        assert svc._embedder.embed_calls == ["Mai thích cà phê sữa"]
        assert len(svc._store.inserts) == 1
        entry_id, content, vec, kw = svc._store.inserts[0]
        assert entry_id == e.entry_id
        assert content == "Mai thích cà phê sữa"
        assert len(vec) == DIM
        assert kw["tier"] == "persistent"

    async def test_write_forwards_viewer_and_session_from_metadata(self) -> None:
        svc = SemanticMemoryService(store=FakeStore(), embedder=FakeEmbedder())
        e = make_entry(metadata={"viewer_id": "v_abc", "session_id": "s_1"})
        await svc.write(e)
        kw = svc._store.inserts[0][3]
        assert kw["viewer_id"] == "v_abc"
        assert kw["session_id"] == "s_1"

    async def test_write_metric_increments(self, svc: SemanticMemoryService) -> None:
        await svc.write(make_entry())
        await svc.write(make_entry())
        assert svc.get_metrics()["memory_writes_total"] == 2

    async def test_duplicate_is_idempotent_and_collision_is_rejected(self) -> None:
        svc = SemanticMemoryService(store=FakeStore(), embedder=FakeEmbedder())
        entry = make_entry("same")
        await svc.write(entry)
        await svc.write(entry)
        assert len(svc._store.inserts) == 1
        assert svc.get_metrics()["memory_duplicates_total"] == 1
        collision = MemoryEntry(
            entry_id=entry.entry_id, content="different", timestamp=entry.timestamp,
            tier=entry.tier,
        )
        with pytest.raises(ValueError, match="collision"):
            await svc.write(collision)

    async def test_write_error_raises(self) -> None:
        svc = SemanticMemoryService(
            store=FakeStore(), embedder=FakeEmbedder(raise_on_call=True)
        )
        with pytest.raises(SemanticMemoryError, match="write failed"):
            await svc.write(make_entry())
        assert svc.get_metrics()["memory_errors_total"] == 1


class TestQuery:
    async def test_query_returns_entries_sorted_by_store(self, svc: SemanticMemoryService) -> None:
        svc._store.knn_results = [
            make_stored("m1", "near", distance=0.05),
            make_stored("m2", "far", distance=0.5),
        ]
        results = await svc.query("gì đó", top_k=2)
        assert [r.entry_id for r in results] == ["m1", "m2"]
        # distance nhét vào metadata
        assert results[0].metadata["distance"] == 0.05

    async def test_query_passes_top_k_and_tier(self, svc: SemanticMemoryService) -> None:
        svc._store.knn_results = []
        await svc.query("q", top_k=5, tier=MemoryTier.SESSION)
        (emb, k, tier_arg, viewer_arg) = svc._store.knn_calls[0]
        assert k == 5
        assert tier_arg == "session"
        assert viewer_arg is None

    async def test_query_metric_and_latency(self, svc: SemanticMemoryService) -> None:
        svc._store.knn_results = [make_stored()]
        await svc.query("q")
        m = svc.get_metrics()
        assert m["memory_queries_total"] == 1
        assert m["memory_last_query_ms"] is not None and m["memory_last_query_ms"] >= 0
        assert m["memory_timeouts_total"] == 0

    async def test_query_timeout_returns_empty_not_raise(self) -> None:
        # store query 500ms → vượt hard timeout 150ms
        svc = SemanticMemoryService(
            store=FakeStore(query_delay_s=0.5),
            embedder=FakeEmbedder(),
            query_timeout_s=0.15,
        )
        t0 = time.perf_counter()
        results = await svc.query("q")
        elapsed = time.perf_counter() - t0
        assert results == []
        assert elapsed < 0.35  # cắt sớm gần 150ms (thêm chút overhead)
        assert svc.get_metrics()["memory_timeouts_total"] == 1

    async def test_query_store_error_returns_empty(self) -> None:
        svc = SemanticMemoryService(
            store=FakeStore(raise_on_query=True), embedder=FakeEmbedder()
        )
        results = await svc.query("q")
        assert results == []
        assert svc.get_metrics()["memory_errors_total"] == 1

    async def test_stored_viewer_session_gom_metadata(self, svc: SemanticMemoryService) -> None:
        svc._store.knn_results = [
            make_stored(viewer_id="v_x", session_id="s_y", distance=0.1),
        ]
        results = await svc.query("q")
        assert results[0].metadata["viewer_id"] == "v_x"
        assert results[0].metadata["session_id"] == "s_y"


class TestForget:
    async def test_forget_deletes(self, svc: SemanticMemoryService) -> None:
        e = make_entry()
        await svc.write(e)
        await svc.forget(e.entry_id)
        assert e.entry_id in svc._store.deletes

    async def test_forget_nonexistent_ok(self, svc: SemanticMemoryService) -> None:
        # không raise
        await svc.forget("nope")


class TestFromLoader:
    def test_from_loader_uses_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        # inject store + embedder giả để tránh mở DB thật + tải bge-m3
        svc = SemanticMemoryService.from_loader(
            loader, store=FakeStore(), embedder=FakeEmbedder(),
        )
        assert svc._timeout_s == 0.15  # default (chưa cấu hình trong system.yaml)
        assert svc._default_top_k == 3
