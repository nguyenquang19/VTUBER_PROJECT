"""Integration DoD Phase 7 (ARCHITECTURE 11.8).

DoD:
- [ ] Retrieve <150ms P95
- [ ] Fallback về working memory khi timeout
- [ ] Manual inject 10 memories → callback > 80%
- [ ] Multi-viewer: 5 viewer qua sessions, không cross-leakage

Test dùng bge-m3 thật (marker 'memory_live') vì DoD đo latency P95 thực tế.
Test không tải bge-m3 (dùng FakeEmbedder) cover logic path, verify DoD-adjacent.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

import pytest

from interfaces.memory import MemoryEntry, MemoryTier
from orchestrator.migration_runner import MigrationRunner
from services.memory.memory_fallback import MemoryFallbackManager
from services.memory.semantic_memory import SemanticMemoryService, new_entry_id
from services.memory.sqlite_vec_store import SqliteVecStore
from services.memory.working_memory import WorkingMemoryService

REPO_ROOT = Path(__file__).resolve().parents[2]

DIM = 1024


# ---------- Fake embedder cho test logic (không tải bge-m3) ----------


class FakeEmbedder:
    """Encode deterministic dựa keyword — verify semantic recall theo tag đơn giản."""

    _KEYWORDS = {
        "cà phê": 0.10, "sinh nhật": 0.20, "mưa": 0.30, "học": 0.40,
        "chó": 0.50, "mèo": 0.60, "biển": 0.70, "núi": 0.80,
        "phim": 0.85, "sách": 0.90, "âm nhạc": 0.95,
    }

    def __init__(self) -> None:
        self._loaded = True
        self.embed_calls = 0

    def is_loaded(self): return self._loaded
    def load(self): self._loaded = True
    def clear_cache(self): pass
    def get_metrics(self): return {"embedder_calls_total": self.embed_calls}

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        text_lower = text.lower()
        # Score = keyword đầu tiên tìm thấy; nếu không có, hash fallback
        for kw, seed in self._KEYWORDS.items():
            if kw in text_lower:
                return [seed] * DIM
        return [(hash(text_lower) % 1000) / 1000.0] * DIM


@pytest.fixture
async def semantic(tmp_path):
    """SemanticMemoryService trên DB thật với FakeEmbedder."""
    db = tmp_path / "mai.db"
    MigrationRunner(db, REPO_ROOT / "migrations", tmp_path / "backups").initialize()
    store = SqliteVecStore(db_path=db)
    emb = FakeEmbedder()
    svc = SemanticMemoryService(store=store, embedder=emb, query_timeout_s=0.15)
    await svc.start()
    yield svc
    await svc.stop()


@pytest.fixture
async def chain(tmp_path):
    """Full chain: MemoryFallbackManager(semantic + working)."""
    db = tmp_path / "mai.db"
    MigrationRunner(db, REPO_ROOT / "migrations", tmp_path / "backups").initialize()
    store = SqliteVecStore(db_path=db)
    emb = FakeEmbedder()
    semantic = SemanticMemoryService(store=store, embedder=emb, query_timeout_s=0.15)
    working = WorkingMemoryService(maxlen=20)
    fb = MemoryFallbackManager(primary=semantic, fallback=working)
    await fb.start()
    yield fb, semantic, working
    await fb.stop()


def make_entry(content: str, tier: MemoryTier = MemoryTier.PERSISTENT,
               viewer_id: str | None = None, importance: float = 0.5) -> MemoryEntry:
    meta = {"viewer_id": viewer_id} if viewer_id else {}
    return MemoryEntry(
        entry_id=new_entry_id(),
        content=content,
        timestamp=datetime.now(),
        tier=tier,
        importance=importance,
        metadata=meta,
    )


# ---------- DoD 1: Retrieve < 150ms P95 ----------


class TestRetrieveLatency:
    async def test_p95_under_150ms(self, semantic: SemanticMemoryService) -> None:
        """DoD 1: query P95 < 150ms trên 100 entry (dùng FakeEmbedder, chỉ đo
        DB+vec+asyncio overhead). Real bge-m3 test riêng ở marker memory_live."""
        # Seed 100 entry
        for i in range(100):
            kw = list(FakeEmbedder._KEYWORDS.keys())[i % len(FakeEmbedder._KEYWORDS)]
            await semantic.write(make_entry(f"{kw} entry số {i}"))
        # Query 50 lần, đo latency
        latencies: list[float] = []
        for i in range(50):
            kw = list(FakeEmbedder._KEYWORDS.keys())[i % len(FakeEmbedder._KEYWORDS)]
            t0 = time.perf_counter()
            _ = await semantic.query(kw, top_k=3)
            latencies.append((time.perf_counter() - t0) * 1000)
        latencies.sort()
        p95 = latencies[int(0.95 * len(latencies)) - 1]
        p50 = latencies[len(latencies) // 2]
        # DoD: <150ms P95. FakeEmbedder gần như instant → thực tế P95 ~vài ms.
        assert p95 < 150, f"P95 {p95:.1f}ms > 150ms (P50={p50:.1f}ms)"
        # metric timeout không tăng
        assert semantic.get_metrics()["memory_timeouts_total"] == 0


# ---------- DoD 2: Fallback khi timeout ----------


class TestFallbackWhenTimeout:
    async def test_semantic_timeout_returns_working(self, tmp_path) -> None:
        """DoD 2: nếu semantic query > 150ms → working memory bù."""
        db = tmp_path / "mai.db"
        MigrationRunner(db, REPO_ROOT / "migrations", tmp_path / "backups").initialize()

        # SlowEmbedder ép semantic vượt timeout 150ms
        class SlowEmbedder(FakeEmbedder):
            def embed(self, text):
                time.sleep(0.2)  # 200ms > 150ms
                return super().embed(text)

        store = SqliteVecStore(db_path=db)
        semantic = SemanticMemoryService(
            store=store, embedder=SlowEmbedder(), query_timeout_s=0.15
        )
        working = WorkingMemoryService(maxlen=20)
        fb = MemoryFallbackManager(primary=semantic, fallback=working)
        await fb.start()

        # Working có sẵn 3 entry recent
        for i in range(3):
            await working.write(make_entry(f"recent {i}"))

        results = await fb.query("bất kỳ", top_k=3)
        # Semantic timeout → [] → fallback kick in → working trả 3
        assert len(results) == 3
        assert semantic.get_metrics()["memory_timeouts_total"] == 1
        assert fb.get_metrics()["memory_fb_fallback_hit"] == 1
        await fb.stop()


# ---------- DoD 3: Manual inject 10 → callback > 80% ----------


class TestManualInjectCallback:
    async def test_inject_10_callback_rate(self, semantic: SemanticMemoryService) -> None:
        """DoD 3: manual inject 10 facts → query bằng keyword → hit ≥ 8/10."""
        facts = [
            ("Mai thích cà phê sữa đá", "cà phê"),
            ("Sinh nhật Mai là 15 tháng 8", "sinh nhật"),
            ("Mai không thích mưa", "mưa"),
            ("Mai đang học đàn piano", "học"),
            ("Mai nuôi 1 con chó tên Bơ", "chó"),
            ("Mai ghét mèo lông dài", "mèo"),
            ("Mai muốn đi biển Đà Nẵng", "biển"),
            ("Mai leo núi Fansipan tuần trước", "núi"),
            ("Mai xem phim kinh dị mọi tối", "phim"),
            ("Mai đọc sách trước khi ngủ", "sách"),
        ]
        # Inject (manual → tier=PERSISTENT importance cao)
        for content, _ in facts:
            await semantic.write(make_entry(content, importance=0.9))

        # Query mỗi keyword → verify entry đúng nằm trong top_k=3
        hits = 0
        for content, keyword in facts:
            results = await semantic.query(keyword, top_k=3)
            if any(content in r.content for r in results):
                hits += 1
        rate = hits / len(facts)
        # DoD: >80%. FakeEmbedder keyword-based → gần như 100%.
        assert rate >= 0.8, f"callback rate {rate:.0%} < 80% ({hits}/{len(facts)})"


# ---------- DoD 4: Multi-viewer isolation ----------


class TestMultiViewer:
    async def test_5_viewer_no_cross_leakage(self, chain) -> None:
        """DoD 4: 5 viewer, mỗi người 3 entry riêng. Query filter viewer_id →
        chỉ trả đúng entry của viewer đó (không leak)."""
        fb, semantic, working = chain
        viewers = ["v_alice", "v_bob", "v_carol", "v_dan", "v_eve"]
        keywords = ["cà phê", "sinh nhật", "mưa"]

        # Mỗi viewer write 3 entry cùng keyword (nhưng viewer_id khác)
        for v in viewers:
            for kw in keywords:
                await fb.write(make_entry(
                    f"{v} thích {kw} lắm",
                    tier=MemoryTier.PERSISTENT,
                    viewer_id=v,
                ))

        # Query "cà phê" với filter viewer=v_alice → chỉ entry của Alice
        for v in viewers:
            results = await fb.query("cà phê", top_k=10, viewer_id=v)
            assert len(results) >= 1, f"viewer {v} không có kết quả"
            for r in results:
                assert r.metadata.get("viewer_id") == v, (
                    f"leak: viewer {v} nhận entry của {r.metadata.get('viewer_id')}"
                )

    async def test_no_viewer_filter_returns_all(self, chain) -> None:
        """Không truyền viewer_id → không filter (backward compat)."""
        fb, _, _ = chain
        for v in ["v_a", "v_b", "v_c"]:
            await fb.write(make_entry("cà phê là ngon", viewer_id=v))
        results = await fb.query("cà phê", top_k=10)
        viewer_ids = {r.metadata.get("viewer_id") for r in results}
        assert viewer_ids == {"v_a", "v_b", "v_c"}
