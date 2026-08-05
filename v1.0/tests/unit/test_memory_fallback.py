"""Test MemoryFallbackManager — Phase 7.E chain semantic → working."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from interfaces.memory import MemoryEntry, MemoryService, MemoryTier
from services.memory.memory_fallback import MemoryFallbackManager
from services.memory.working_memory import WorkingMemoryService


class FakePrimary(MemoryService):
    """Giả SemanticMemoryService — control query result + raise behavior."""

    service_id = "primary"

    def __init__(
        self,
        query_returns: list[MemoryEntry] | None = None,
        query_raises: Exception | None = None,
        write_raises: Exception | None = None,
    ) -> None:
        self.query_returns = query_returns or []
        self.query_raises = query_raises
        self.write_raises = write_raises
        self.writes: list[MemoryEntry] = []
        self.query_calls = 0
        self.forget_calls: list[str] = []

    async def start(self): pass
    async def stop(self): pass
    async def health_check(self):
        from interfaces.base import HealthStatus
        return HealthStatus.healthy(self.service_id)
    def get_metrics(self): return {"primary_writes": len(self.writes)}

    async def write(self, entry):
        if self.write_raises: raise self.write_raises
        self.writes.append(entry)
    async def query(self, text, top_k=3, tier=None, viewer_id=None):
        self.query_calls += 1
        if self.query_raises: raise self.query_raises
        return list(self.query_returns)
    async def forget(self, entry_id):
        self.forget_calls.append(entry_id)


def make_entry(id_: str, tier: MemoryTier = MemoryTier.PERSISTENT) -> MemoryEntry:
    return MemoryEntry(
        entry_id=id_, content=f"c-{id_}", timestamp=datetime.now(), tier=tier,
    )


@pytest.fixture
def working():
    return WorkingMemoryService(maxlen=10)


class TestLifecycle:
    async def test_start_forwards_both(self, working) -> None:
        p = FakePrimary()
        fb = MemoryFallbackManager(primary=p, fallback=working)
        await fb.start()  # không raise
        h = await fb.health_check()
        assert h.is_ok is True


class TestWriteFanOut:
    async def test_write_forwards_both(self, working) -> None:
        p = FakePrimary()
        fb = MemoryFallbackManager(primary=p, fallback=working)
        e = make_entry("m1")
        await fb.write(e)
        assert p.writes == [e]
        assert [x.entry_id for x in working.snapshot()] == ["m1"]

    async def test_primary_write_fail_still_writes_fallback(self, working) -> None:
        p = FakePrimary(write_raises=RuntimeError("db down"))
        fb = MemoryFallbackManager(primary=p, fallback=working)
        e = make_entry("m1")
        # KHÔNG raise (partial success)
        await fb.write(e)
        assert p.writes == []
        assert len(working.snapshot()) == 1
        assert fb.get_metrics()["memory_fb_writes_partial"] == 1

    async def test_both_write_fail_raises(self, working) -> None:
        class BrokenFallback(WorkingMemoryService):
            async def write(self, entry): raise RuntimeError("mem down")
        p = FakePrimary(write_raises=RuntimeError("db down"))
        fb = MemoryFallbackManager(primary=p, fallback=BrokenFallback(maxlen=5))
        with pytest.raises(RuntimeError):
            await fb.write(make_entry("m1"))


class TestQueryFallback:
    async def test_primary_hit_returns_primary(self, working) -> None:
        p = FakePrimary(query_returns=[make_entry("p1"), make_entry("p2")])
        await working.write(make_entry("w1"))  # fallback có sẵn
        fb = MemoryFallbackManager(primary=p, fallback=working)
        results = await fb.query("q")
        assert [e.entry_id for e in results] == ["p1", "p2"]
        assert fb.get_metrics()["memory_fb_primary_hit"] == 1
        assert fb.get_metrics()["memory_fb_fallback_hit"] == 0

    async def test_primary_empty_falls_back_to_working(self, working) -> None:
        p = FakePrimary(query_returns=[])
        await working.write(make_entry("w1"))
        await working.write(make_entry("w2"))
        fb = MemoryFallbackManager(primary=p, fallback=working)
        results = await fb.query("q", top_k=5)
        # working trả recent first
        assert [e.entry_id for e in results] == ["w2", "w1"]
        assert fb.get_metrics()["memory_fb_fallback_hit"] == 1

    async def test_primary_error_falls_back(self, working) -> None:
        p = FakePrimary(query_raises=RuntimeError("timeout mask"))
        await working.write(make_entry("w1"))
        fb = MemoryFallbackManager(primary=p, fallback=working)
        results = await fb.query("q")
        assert [e.entry_id for e in results] == ["w1"]

    async def test_both_empty_returns_empty(self, working) -> None:
        fb = MemoryFallbackManager(primary=FakePrimary(), fallback=working)
        assert await fb.query("q") == []
        assert fb.get_metrics()["memory_fb_primary_hit"] == 0
        assert fb.get_metrics()["memory_fb_fallback_hit"] == 0

    async def test_viewer_id_forwarded_to_both_tiers(self, working) -> None:
        p = FakePrimary(query_returns=[])
        # working có 2 entries khác viewer
        from datetime import datetime as _dt
        from interfaces.memory import MemoryEntry
        e_a = MemoryEntry(entry_id="a", content="c-a", timestamp=_dt.now(),
                          tier=MemoryTier.WORKING, metadata={"viewer_id": "v_a"})
        e_b = MemoryEntry(entry_id="b", content="c-b", timestamp=_dt.now(),
                          tier=MemoryTier.WORKING, metadata={"viewer_id": "v_b"})
        await working.write(e_a); await working.write(e_b)
        fb = MemoryFallbackManager(primary=p, fallback=working)
        results = await fb.query("q", top_k=5, viewer_id="v_a")
        assert [e.entry_id for e in results] == ["a"]


class TestForget:
    async def test_forget_calls_both(self, working) -> None:
        p = FakePrimary()
        await working.write(make_entry("m1"))
        fb = MemoryFallbackManager(primary=p, fallback=working)
        await fb.forget("m1")
        assert p.forget_calls == ["m1"]
        assert [e.entry_id for e in working.snapshot()] == []
