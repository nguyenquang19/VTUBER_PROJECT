from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from interfaces.memory import MemoryEntry, MemoryTier
from services.memory.working_memory import WorkingMemoryService
from services.relationship.manager import RelationshipLimits, RelationshipManager
from services.relationship.store import RelationshipStore


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _manager(memory=None) -> RelationshipManager:
    root = Path(__file__).resolve().parents[2]
    conn = sqlite3.connect(":memory:")
    for name in ("005_add_relationship_tables.sql", "006_add_relationship_positive_events.sql"):
        conn.executescript((root / "migrations" / name).read_text(encoding="utf-8"))
    return RelationshipManager(
        RelationshipStore(conn=conn), RelationshipLimits(), clock=lambda: NOW,
        evidence_exists=lambda value: value in {"agent:chat:e1", "agent:chat:e2"},
        memory_service=memory,
    )


async def test_export_is_pseudonymous_and_sanitizes_memory() -> None:
    memory = WorkingMemoryService()
    manager = _manager(memory)
    profile = manager.observe_interaction(
        raw_viewer_id="raw-platform-id", event_id="e1", occurred_at=NOW,
    )
    assert profile is not None
    manager.update_profile(
        profile.viewer_id, preferences=["cats"], boundaries=[], tone="gentle",
        evidence_refs=["agent:chat:e1"], reason="operator",
    )
    await memory.write(MemoryEntry(
        entry_id="m1", content="email x@example.com", timestamp=NOW,
        tier=MemoryTier.PERSISTENT, metadata={"viewer_id": profile.viewer_id},
    ))
    exported = await manager.export_viewer(profile.viewer_id)
    rendered = str(exported)
    assert "raw-platform-id" not in rendered
    assert "x@example.com" not in rendered
    assert "[PII]" in rendered
    assert exported["profile"]["viewer_id"] == profile.viewer_id


async def test_delete_removes_only_target_viewer_relationship_and_memory() -> None:
    memory = WorkingMemoryService()
    manager = _manager(memory)
    first = manager.observe_interaction(raw_viewer_id="raw-a", event_id="e1", occurred_at=NOW)
    second = manager.observe_interaction(raw_viewer_id="raw-b", event_id="e2", occurred_at=NOW)
    assert first is not None and second is not None
    for profile, entry_id in ((first, "m-a"), (second, "m-b")):
        await memory.write(MemoryEntry(
            entry_id=entry_id, content="safe", timestamp=NOW,
            metadata={"viewer_id": profile.viewer_id},
        ))
    result = await manager.delete_viewer(first.viewer_id, reason="viewer request")
    assert result is not None and result["memory"] == 1
    assert manager.get_profile(first.viewer_id) is None
    assert manager.get_profile(second.viewer_id) is not None
    assert await memory.export_viewer(first.viewer_id) == []
    assert len(await memory.export_viewer(second.viewer_id)) == 1


async def test_memory_failure_aborts_relationship_delete() -> None:
    class BrokenMemory:
        async def forget_viewer(self, viewer_id):
            raise RuntimeError("memory purge failed")

        async def export_viewer(self, viewer_id):
            return []

    manager = _manager(BrokenMemory())
    profile = manager.observe_interaction(raw_viewer_id="raw", event_id="e1", occurred_at=NOW)
    assert profile is not None
    with pytest.raises(RuntimeError, match="memory purge failed"):
        await manager.delete_viewer(profile.viewer_id, reason="viewer request")
    assert manager.get_profile(profile.viewer_id) is not None


async def test_working_memory_forget_viewer_is_idempotent() -> None:
    memory = WorkingMemoryService()
    await memory.write(MemoryEntry(
        entry_id="m1", content="a", timestamp=NOW, metadata={"viewer_id": "v_a"},
    ))
    assert await memory.forget_viewer("v_a") == 1
    assert await memory.forget_viewer("v_a") == 0

