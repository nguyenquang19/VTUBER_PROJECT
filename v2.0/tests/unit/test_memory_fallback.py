"""Test MemoryFallbackManager — Phase 7.E chain semantic → working."""
from __future__ import annotations

from datetime import datetime, timezone
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
        start_raises: Exception | None = None,
    ) -> None:
        self.query_returns = query_returns or []
        self.query_raises = query_raises
        self.write_raises = write_raises
        self.start_raises = start_raises
        self.writes: list[MemoryEntry] = []
        self.query_calls = 0
        self.query_viewer_ids: list[str | None] = []
        self.forget_calls: list[str] = []
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self):
        self.start_calls += 1
        if self.start_raises:
            raise self.start_raises
    async def stop(self): self.stop_calls += 1
    async def health_check(self):
        from interfaces.base import HealthStatus
        return HealthStatus.healthy(self.service_id)
    def get_metrics(self): return {"primary_writes": len(self.writes)}

    async def write(self, entry):
        if self.write_raises: raise self.write_raises
        self.writes.append(entry)
    async def query(self, text, top_k=3, tier=None, viewer_id=None):
        self.query_calls += 1
        self.query_viewer_ids.append(viewer_id)
        if self.query_raises: raise self.query_raises
        return list(self.query_returns)
    async def forget(self, entry_id):
        self.forget_calls.append(entry_id)


def make_entry(id_: str, tier: MemoryTier = MemoryTier.PERSISTENT) -> MemoryEntry:
    return MemoryEntry(
        entry_id=id_, content=f"c-{id_}", timestamp=datetime.now(timezone.utc), tier=tier,
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

    async def test_primary_start_failure_degrades_to_working_only(self, working) -> None:
        p = FakePrimary(start_raises=RuntimeError("model unavailable"))
        fb = MemoryFallbackManager(primary=p, fallback=working)
        await fb.start()
        await fb.write(make_entry("w1"))
        assert [entry.entry_id for entry in await fb.query("q")] == ["w1"]
        metrics = fb.get_metrics()
        assert metrics["memory_fb_primary_available"] is False
        assert metrics["memory_fb_primary_start_failures"] == 1
        assert metrics["memory_fb_fallback_total"] == 1

    async def test_flag_off_uses_working_only(self, working) -> None:
        fb = MemoryFallbackManager(primary=None, fallback=working)
        await fb.start()
        await fb.write(make_entry("w1"))
        assert [entry.entry_id for entry in await fb.query("q")] == ["w1"]
        metrics = fb.get_metrics()
        assert metrics["memory_fb_primary_enabled"] is False
        assert metrics["memory_fb_primary_available"] is False
        assert metrics["memory_fb_fallback_hit"] == 1


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
        assert fb.get_metrics()["memory_fb_fallback_total"] == 1

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
        assert fb.get_metrics()["memory_fb_fallback_miss"] == 1

    async def test_viewer_id_forwarded_to_both_tiers(self, working) -> None:
        p = FakePrimary(query_returns=[])
        # working có 2 entries khác viewer
        from datetime import datetime as _dt, timezone as _tz
        from interfaces.memory import MemoryEntry
        e_a = MemoryEntry(entry_id="a", content="c-a", timestamp=_dt.now(_tz.utc),
                          tier=MemoryTier.WORKING, metadata={"viewer_id": "v_a"})
        e_b = MemoryEntry(entry_id="b", content="c-b", timestamp=_dt.now(_tz.utc),
                          tier=MemoryTier.WORKING, metadata={"viewer_id": "v_b"})
        await working.write(e_a); await working.write(e_b)
        fb = MemoryFallbackManager(primary=p, fallback=working)
        results = await fb.query("q", top_k=5, viewer_id="v_a")
        assert [e.entry_id for e in results] == ["a"]

    async def test_raw_viewer_round_trip_is_pseudonymized_symmetrically(
        self, working,
    ) -> None:
        raw_viewer = "youtube-channel-raw-123"
        entry = MemoryEntry(
            entry_id="round-trip", content="viewer_preference: thích mèo",
            timestamp=datetime.now(timezone.utc), tier=MemoryTier.PERSISTENT,
            metadata={"viewer_id": raw_viewer},
        )
        primary = FakePrimary()
        fb = MemoryFallbackManager(primary=primary, fallback=working)
        await fb.write(entry)
        stored = working.snapshot()[0]
        pseudonym = stored.metadata["viewer_id"]
        assert isinstance(pseudonym, str) and pseudonym.startswith("v_")
        assert pseudonym != raw_viewer
        assert primary.writes[0].metadata["viewer_id"] == pseudonym

        results = await fb.query("mèo", viewer_id=raw_viewer)
        assert [item.entry_id for item in results] == ["round-trip"]
        assert primary.query_viewer_ids == [pseudonym]

        # Passing the pseudonym again must not hash it a second time.
        results = await fb.query("mèo", viewer_id=pseudonym)
        assert [item.entry_id for item in results] == ["round-trip"]
        assert primary.query_viewer_ids[-1] == pseudonym

    async def test_export_and_forget_normalize_raw_viewer_id(self, working) -> None:
        raw_viewer = "discord-user-raw-456"
        fb = MemoryFallbackManager(primary=None, fallback=working)
        await fb.write(MemoryEntry(
            entry_id="privacy", content="verified_conversation",
            timestamp=datetime.now(timezone.utc),
            metadata={"viewer_id": raw_viewer},
        ))
        exported = await fb.export_viewer(raw_viewer)
        assert [item.entry_id for item in exported] == ["privacy"]
        assert exported[0].metadata["viewer_id"] != raw_viewer
        assert await fb.forget_viewer(raw_viewer) == 1
        assert await fb.export_viewer(raw_viewer) == []


class TestForget:
    async def test_forget_calls_both(self, working) -> None:
        p = FakePrimary()
        await working.write(make_entry("m1"))
        fb = MemoryFallbackManager(primary=p, fallback=working)
        await fb.forget("m1")
        assert p.forget_calls == ["m1"]
        assert [e.entry_id for e in working.snapshot()] == []
