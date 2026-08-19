"""M10.2 public composition boundaries and fallback introspection."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from interfaces.memory import MemoryEntry
from services.memory.memory_fallback import MemoryFallbackManager


REPO_ROOT = Path(__file__).resolve().parents[2]


class SnapshotFallback:
    def __init__(self, entries) -> None:
        self._entries = entries

    def snapshot(self):
        return self._entries


def test_memory_fallback_snapshot_returns_entry_list() -> None:
    entry = MemoryEntry(
        entry_id="recent-1",
        content="recent context",
        timestamp=datetime.now(timezone.utc),
    )
    manager = MemoryFallbackManager(object(), SnapshotFallback([entry]))
    assert manager.fallback_snapshot() == [entry]


def test_stream_runtime_composition_uses_public_boundaries() -> None:
    source = (REPO_ROOT / "orchestrator" / "stream_runtime.py").read_text(
        encoding="utf-8",
    )
    forbidden = (
        "._turn_lock",
        "._fallback",
        "._modifiers",
        "._runtime_ctx_fn",
        "router._process",
        "rt._stop_",
        "rt._operations_snapshot",
        "rt._shutdown_coordinator",
        "getattr(self._router, \"_sources\"",
    )
    assert [token for token in forbidden if token in source] == []
    assert "router.add_activity_listener(_on_input_activity)" in source
    assert "director_loop.set_runtime_context_provider(rt.runtime_context)" in source
    assert "async def execute_external_action" in source
    director_call = source[
        source.index("director_loop = DirectorLoop("):
        source.index("# ─── M9 operator control plane")
    ]
    assert "external_action_loop" not in director_call
    assert "external_executor_registry" not in director_call
