"""Test WorkingMemoryService — Phase 7.E (deque 20, in-memory, no DB)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from interfaces.memory import MemoryEntry, MemoryTier
from services.memory.working_memory import WorkingMemoryService

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_entry(idx: int, tier: MemoryTier = MemoryTier.WORKING) -> MemoryEntry:
    return MemoryEntry(
        entry_id=f"m{idx}",
        content=f"content {idx}",
        timestamp=datetime.now(timezone.utc) + timedelta(seconds=idx),
        tier=tier,
    )


class TestLifecycle:
    async def test_start_health(self) -> None:
        svc = WorkingMemoryService(maxlen=5)
        await svc.start()
        h = await svc.health_check()
        assert h.is_ok is True

    async def test_stop_clears(self) -> None:
        svc = WorkingMemoryService(maxlen=5)
        await svc.write(make_entry(1))
        await svc.stop()
        assert svc.snapshot() == []


class TestWrite:
    async def test_append(self) -> None:
        svc = WorkingMemoryService(maxlen=5)
        await svc.write(make_entry(1))
        await svc.write(make_entry(2))
        snap = svc.snapshot()
        assert [e.entry_id for e in snap] == ["m1", "m2"]
        assert svc.get_metrics()["working_size"] == 2

    async def test_evict_oldest_when_full(self) -> None:
        svc = WorkingMemoryService(maxlen=3)
        for i in range(5):
            await svc.write(make_entry(i))
        snap = svc.snapshot()
        # deque giữ 3 mới nhất
        assert [e.entry_id for e in snap] == ["m2", "m3", "m4"]
        assert svc.get_metrics()["working_evictions_total"] == 2

    async def test_duplicate_is_idempotent_and_collision_is_rejected(self) -> None:
        svc = WorkingMemoryService(maxlen=3)
        entry = make_entry(1)
        await svc.write(entry)
        await svc.write(entry)
        assert len(svc.snapshot()) == 1
        assert svc.get_metrics()["working_duplicates_total"] == 1
        collision = MemoryEntry(
            entry_id=entry.entry_id, content="different",
            timestamp=entry.timestamp,
        )
        with pytest.raises(ValueError, match="collision"):
            await svc.write(collision)


class TestQuery:
    async def test_returns_most_recent_first(self) -> None:
        svc = WorkingMemoryService(maxlen=10)
        for i in range(5):
            await svc.write(make_entry(i))
        results = await svc.query("query text ignored", top_k=3)
        assert [e.entry_id for e in results] == ["m4", "m3", "m2"]

    async def test_top_k_larger_than_buffer(self) -> None:
        svc = WorkingMemoryService(maxlen=10)
        await svc.write(make_entry(0))
        await svc.write(make_entry(1))
        results = await svc.query("q", top_k=10)
        assert len(results) == 2

    async def test_filter_by_tier(self) -> None:
        svc = WorkingMemoryService(maxlen=10)
        await svc.write(make_entry(0, tier=MemoryTier.WORKING))
        await svc.write(make_entry(1, tier=MemoryTier.SESSION))
        await svc.write(make_entry(2, tier=MemoryTier.WORKING))
        results = await svc.query("q", top_k=5, tier=MemoryTier.WORKING)
        assert [e.entry_id for e in results] == ["m2", "m0"]

    async def test_empty_returns_empty(self) -> None:
        svc = WorkingMemoryService(maxlen=5)
        assert await svc.query("q") == []

    async def test_filter_by_viewer_id(self) -> None:
        svc = WorkingMemoryService(maxlen=10)
        e0 = make_entry(0)
        e1 = MemoryEntry(entry_id="m1", content="content 1", timestamp=datetime.now(timezone.utc), metadata={"viewer_id": "v_a"})
        e2 = MemoryEntry(entry_id="m2", content="content 2", timestamp=datetime.now(timezone.utc), metadata={"viewer_id": "v_b"})
        e3 = MemoryEntry(entry_id="m3", content="content 3", timestamp=datetime.now(timezone.utc), metadata={"viewer_id": "v_a"})
        for e in (e0, e1, e2, e3):
            await svc.write(e)
        results = await svc.query("q", top_k=5, viewer_id="v_a")
        assert [e.entry_id for e in results] == ["m3", "m1"]


class TestForget:
    async def test_forget_removes(self) -> None:
        svc = WorkingMemoryService(maxlen=5)
        for i in range(3):
            await svc.write(make_entry(i))
        await svc.forget("m1")
        snap_ids = [e.entry_id for e in svc.snapshot()]
        assert "m1" not in snap_ids
        assert snap_ids == ["m0", "m2"]

    async def test_forget_nonexistent_noop(self) -> None:
        svc = WorkingMemoryService(maxlen=5)
        await svc.write(make_entry(0))
        await svc.forget("nope")
        assert len(svc.snapshot()) == 1


class TestFromLoader:
    def test_default_maxlen(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        svc = WorkingMemoryService.from_loader(loader)
        assert svc.maxlen == 20  # default khi chưa có key trong system.yaml
