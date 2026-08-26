from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.config_loader import ConfigLoader
from orchestrator.fallback_manager import FallbackManager
from orchestrator.features import FeatureManager, FeatureStatus
from services.llm.canned_response import CannedResponder
from services.llm.llm_turn import LLMTurnRunner
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager
from services.cognition.agent_context_projection import AgentContextRenderer, ContextRenderConfig
from interfaces.state import (
    AgentEventKind,
    AgentEventSource,
    AgentStateSnapshot,
    EventProvenance,
    GroundedEvent,
    TopicState,
)

NOW = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _event(index: int, kind: AgentEventKind, text: str) -> GroundedEvent:
    return GroundedEvent(
        event_id=f"e{index}",
        kind=kind,
        source=AgentEventSource.CHAT if "chat" in kind.value else AgentEventSource.LLM,
        timestamp=NOW - timedelta(seconds=10 - index),
        confidence=1.0,
        payload={"text": text},
        provenance=EventProvenance("test_producer", source_event_id=f"source-{index}"),
    )


def _renderer(min_items: int = 3, max_items: int = 6) -> AgentContextRenderer:
    return AgentContextRenderer(ContextRenderConfig(
        min_items=min_items,
        max_items=max_items,
        item_max_chars=100,
        relevance_window_s=60,
    ), clock=lambda: NOW)


def test_renders_only_three_to_six_grounded_items_with_provenance() -> None:
    events = tuple(
        _event(index, AgentEventKind.CHAT_RECEIVED, f"cà phê grounded {index}")
        for index in range(8)
    )
    snapshot = AgentStateSnapshot(
        current_topic=TopicState("cà phê", "e0", NOW, 1.0),
        recent_events=events,
    )
    rendered = _renderer().render(snapshot, "cà phê nào")
    assert rendered is not None
    items = [line for line in rendered.splitlines() if line.startswith("- ")]
    assert len(items) == 6
    assert all("producer=test_producer" in line and "source_id=" in line for line in items)
    assert "invented" not in rendered


def test_returns_none_instead_of_padding_when_fewer_than_minimum() -> None:
    snapshot = AgentStateSnapshot(recent_events=(
        _event(1, AgentEventKind.CHAT_RECEIVED, "một fact"),
        _event(2, AgentEventKind.SPEECH_FINAL, "hai fact"),
    ))
    assert _renderer().render(snapshot, "fact") is None


def test_excludes_items_outside_relevance_window() -> None:
    old = GroundedEvent(
        event_id="old",
        kind=AgentEventKind.CHAT_RECEIVED,
        source=AgentEventSource.CHAT,
        timestamp=NOW - timedelta(seconds=61),
        confidence=1.0,
        payload={"text": "old"},
        provenance=EventProvenance("test"),
    )
    fresh = tuple(_event(i, AgentEventKind.CHAT_RECEIVED, f"fresh {i}") for i in range(3))
    rendered = _renderer().render(AgentStateSnapshot(recent_events=(old, *fresh)), "fresh")
    assert rendered is not None
    assert "source_id=old" not in rendered


def test_item_text_is_bounded_by_config() -> None:
    events = tuple(
        _event(i, AgentEventKind.SPEECH_FINAL, "x" * 500) for i in range(3)
    )
    rendered = _renderer().render(AgentStateSnapshot(recent_events=events))
    assert rendered is not None
    assert max(len(line.split(": ", 1)[-1]) for line in rendered.splitlines()[1:]) <= 100


def test_prompt_manager_injects_grounded_context_as_system_message() -> None:
    manager = PromptManager(PromptCache("persona"))
    request = manager.build_request(
        "r1", "follow-up", grounded_context="[Grounded working context]\n- fact",
    )
    assert [message.role for message in request.messages] == ["system", "system", "user"]
    assert request.messages[1].content.startswith("[Grounded working context]")


async def test_agent_context_and_selector_default_on_while_runner_remains_switchable() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    manager = FeatureManager.from_config(loader)
    assert await manager.get_status("agent_context") is FeatureStatus.ENABLED
    assert await manager.get_status("context_selector") is FeatureStatus.ENABLED

    class State:
        def snapshot(self) -> AgentStateSnapshot:
            return AgentStateSnapshot()

    class Renderer:
        def render(self, snapshot: AgentStateSnapshot, query: str) -> str:
            return "grounded"

    runner = LLMTurnRunner(
        svc=object(),
        prompt_manager=PromptManager(PromptCache("persona")),
        fallback=FallbackManager(),
        canned=CannedResponder({"default": ["fallback"]}),
        agent_state=State(),
    )
    assert runner.agent_context_enabled is False
    assert runner._render_agent_context("query") is None
    runner.set_agent_context_renderer(Renderer())
    assert runner.agent_context_enabled is True
    assert runner._render_agent_context("query") == "grounded"

    class Selector:
        async def select(self, snapshot: AgentStateSnapshot, query: str, viewer_id: str | None = None) -> str:
            return f"selected:{query}:{viewer_id}"

    runner.set_conversation_context_renderer(Selector())
    assert await runner._select_agent_context("query", viewer_id="viewer-1") == "selected:query:viewer-1"
