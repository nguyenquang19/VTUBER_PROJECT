from __future__ import annotations

from datetime import datetime, timedelta, timezone

from interfaces.compatibility import SelfSnapshot, StateValue, WorldSnapshot
from interfaces.memory import MemoryEntry, MemoryTier
from services.agent.conversation_context import ConversationContextComposer, ConversationContextConfig
from services.agent.types import AgentStateSnapshot

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class Memory:
    def __init__(self, entries: list[MemoryEntry] | None = None, *, fails: bool = False) -> None:
        self.entries = entries or []
        self.fails = fails
        self.calls = 0

    async def query(self, query: str, *, top_k: int, viewer_id: str | None) -> list[MemoryEntry]:
        self.calls += 1
        if self.fails:
            raise TimeoutError("semantic memory unavailable")
        return self.entries[:top_k]


def _world() -> WorldSnapshot:
    value = StateValue(
        value="fresh live track", source="runtime", confidence=0.9, updated_at=NOW,
        evidence_refs=("observation-1",), expires_at=NOW + timedelta(seconds=60), authority=60,
    )
    return WorldSnapshot(snapshot_id="world-1", created_at=NOW, stream={"track": value})


def _self() -> SelfSnapshot:
    return SelfSnapshot(
        snapshot_id="self-1", created_at=NOW, speaking=False, busy=False, degraded=False,
        current_action_id=None, current_intention_id=None, active_goal_id=None,
        focused_thread_id=None, current_topic="music", attention_target=None,
        avatar_state={}, recent_action_ids=(),
    )


def _composer(memory: Memory, *, enabled: bool = True) -> ConversationContextComposer:
    return ConversationContextComposer(
        ConversationContextConfig(
            max_chars=400, evidence_items=3, item_max_chars=120, selector_max_chars=3000,
            memory_items=3, world_items=4, capability_items=3,
        ),
        world_snapshot_provider=_world,
        self_snapshot_provider=_self,
        capability_snapshot_provider=lambda: {
            "capabilities": [{
                "capability": {"capability_id": "WAIT", "action_type": "WAIT"},
                "availability": {"available": True, "evidence_refs": ["capability:WAIT"]},
            }],
        },
        memory_provider=lambda: memory,
        selector_enabled=enabled,
    )


async def test_selector_world_truth_overrides_conflicting_memory_and_preserves_failure() -> None:
    memory = Memory([
        MemoryEntry(
            entry_id="old-track", content="old remembered track", timestamp=NOW,
            tier=MemoryTier.PERSISTENT, metadata={"world_path": "stream.track", "confidence": 0.3},
        ),
        MemoryEntry(
            entry_id="failed-action", content="tried to change scene", timestamp=NOW,
            metadata={"action_status": "failed", "provenance": "action_result"},
        ),
    ])
    composer = _composer(memory)
    context = await composer.select(AgentStateSnapshot(), "track")
    assert "Current world [stream.track" in context
    assert "fresh live track" in context
    assert "old remembered track" not in context
    assert "Past memory [failed-action" in context
    assert "outcome=failed" in context
    assert "Available capability [WAIT" in context
    metrics = composer.get_metrics()
    assert metrics["conversation_context_selector_world_override_total"] == 1


async def test_selector_memory_error_fails_open_to_grounded_world() -> None:
    memory = Memory(fails=True)
    composer = _composer(memory)
    context = await composer.select(AgentStateSnapshot(), "track")
    assert "Current world [stream.track" in context
    assert "Past memory" not in context
    assert composer.get_metrics()["conversation_context_selector_memory_errors_total"] == 1


async def test_selector_disabled_preserves_legacy_continuity_and_does_not_query_memory() -> None:
    memory = Memory([MemoryEntry(entry_id="m1", content="unused", timestamp=NOW)])
    context = await _composer(memory, enabled=False).select(AgentStateSnapshot(), "track")
    assert "Current world" not in context
    assert "Past memory" not in context
    assert memory.calls == 0