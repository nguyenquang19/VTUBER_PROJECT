"""Canonical ingress preserves legacy state semantics behind one writer boundary."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from interfaces.compatibility import EventProvenance as PerceptionProvenance
from interfaces.compatibility import PerceptionEvent
from interfaces.events import (
    AgentEventKind,
    AgentEventSource,
    CanonicalEvent,
    CanonicalEventRoute,
    EventProvenance,
    GroundedEvent,
)
from interfaces.input import EventSource, InputEvent
from orchestrator.config_loader import ConfigLoader
from services.ingress.adapters import (
    CanonicalAgentStateAdapter,
    CanonicalEventIngress,
    CanonicalPerceptionIngressAdapter,
    CanonicalWorldModelAdapter,
)
from services.ingress.normalizer import CanonicalEventNormalizer
from services.state.agent import AgentState
from services.state.authoritative import AuthoritativeStateConfig, AuthoritativeStateReducer
from services.state.event_ledger import EventLedger
from services.state.world import WorldModelShadow
from services.perception.ingress import PerceptionIngress


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)


def _loader() -> ConfigLoader:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return loader


def _stores(loader: ConfigLoader) -> tuple[AgentState, WorldModelShadow]:
    ledger = EventLedger.from_loader(loader, clock=lambda: NOW)
    agent = AgentState.from_loader(loader, ledger, clock=lambda: NOW)
    world = WorldModelShadow.from_loader(loader, enabled=True, clock=lambda: NOW)
    return agent, world


def _grounded(event_id: str = "agent:chat:e1") -> GroundedEvent:
    return GroundedEvent(
        event_id=event_id,
        kind=AgentEventKind.CHAT_RECEIVED,
        source=AgentEventSource.YOUTUBE,
        timestamp=NOW,
        confidence=1.0,
        payload={"text": "Chào Mai", "viewer_alias": "Lan"},
        provenance=EventProvenance(
            producer="chat_router", source_event_id="e1", platform="youtube",
        ),
    )


def _world_event(event_id: str = "world:e1") -> PerceptionEvent:
    return PerceptionEvent(
        schema_version=1,
        event_id=event_id,
        source="runtime",
        event_type="world.observation",
        timestamp=NOW,
        payload={"path": "stream.runtime", "value": {"ready": True}},
        provenance=PerceptionProvenance(
            producer="system_perception_adapter", source_event_id="e1",
        ),
        confidence=1.0,
        dedup_key=f"system:{event_id}",
    )


def _canonical_stack(loader: ConfigLoader):
    agent, world = _stores(loader)
    normalizer = CanonicalEventNormalizer.from_loader(loader)
    state = AuthoritativeStateReducer(
        AuthoritativeStateConfig.from_loader(loader),
        agent_state=agent,
        world_model=world,
        clock=lambda: NOW,
    )
    ingress = CanonicalEventIngress(state)
    return (
        CanonicalAgentStateAdapter(agent, normalizer, ingress),
        CanonicalWorldModelAdapter(world, normalizer, ingress),
        state,
    )


def test_canonical_event_is_immutable_and_rejects_raw_identity() -> None:
    event = CanonicalEvent(
        schema_version=1,
        event_id="e1",
        route=CanonicalEventRoute.AGENT,
        source="youtube",
        event_type="chat_received",
        timestamp=NOW,
        confidence=1,
        payload={"text": "xin chào", "nested": {"ok": True}},
        provenance=EventProvenance(producer="test"),
        dedup_key="e1",
    )
    assert event.to_dict()["payload"] == {"text": "xin chào", "nested": {"ok": True}}
    with pytest.raises(TypeError):
        event.payload["text"] = "changed"  # type: ignore[index]
    with pytest.raises(ValueError, match="sensitive"):
        CanonicalEvent(
            schema_version=1,
            event_id="e2",
            route=CanonicalEventRoute.OBSERVATION,
            source="chat_youtube",
            event_type="input_observation",
            timestamp=NOW,
            confidence=1.0,
            payload={"user_id": "raw-id"},
            provenance=EventProvenance(producer="test"),
            dedup_key="e2",
        )


def test_input_normalizer_drops_raw_identity_before_boundary() -> None:
    canonical = CanonicalEventNormalizer.from_loader(_loader()).from_input(InputEvent(
        event_id="input-1",
        timestamp=NOW,
        source=EventSource.CHAT_YOUTUBE,
        user_id="raw-id",
        user_name="Private Name",
        content="hello",
        metadata={"viewer_id": "raw-id", "is_owner": True},
    ))
    assert canonical.payload == {"content": "hello", "metadata": {"is_owner": True}}
    assert "raw-id" not in str(canonical.to_dict())
    assert "Private Name" not in str(canonical.to_dict())


def test_canonical_adapters_match_direct_agent_and_world_reducers() -> None:
    loader = _loader()
    direct_agent, direct_world = _stores(loader)
    canonical_agent, canonical_world, state = _canonical_stack(loader)
    grounded = _grounded()
    observed = _world_event()

    assert direct_agent.record(grounded)
    assert direct_world.apply_event(observed)
    assert canonical_agent.record(grounded)
    assert canonical_world.apply_event(observed)

    assert canonical_agent.snapshot().to_dict() == direct_agent.snapshot().to_dict()
    assert canonical_world.snapshot().to_dict() == direct_world.snapshot().to_dict()
    snapshot = state.snapshot()
    assert snapshot.revision == 2
    assert snapshot.last_event_id == observed.event_id
    assert snapshot.agent.to_dict() == direct_agent.snapshot().to_dict()
    assert snapshot.world.to_dict() == direct_world.snapshot().to_dict()


def test_deterministic_state_replay_matches_for_all_grounded_event_classes() -> None:
    loader = _loader()
    direct_agent, _direct_world = _stores(loader)
    canonical_agent, _canonical_world, _state = _canonical_stack(loader)
    rows = (
        (AgentEventKind.CHAT_RECEIVED, {"text": "Mai ơi"}),
        (AgentEventKind.DONATION_RECEIVED, {"text": "tặng Mai", "amount_vnd": 50000}),
        (AgentEventKind.ENVIRONMENT_OBSERVED, {"source_services": ["youtube"]}),
        (AgentEventKind.SPEECH_FINAL, {"text": "Cảm ơn cậu nhé"}),
        (AgentEventKind.GOAL_AUDIT, {"goal_id": "goal-1", "outcome": "observed"}),
    )
    for index, (kind, payload) in enumerate(rows):
        event = GroundedEvent(
            event_id=f"replay-{index}",
            kind=kind,
            source=AgentEventSource.RUNTIME,
            timestamp=NOW + timedelta(milliseconds=index),
            confidence=1.0,
            payload=payload,
            provenance=EventProvenance(producer="deterministic_replay"),
        )
        assert direct_agent.record(event)
        assert canonical_agent.record(event)
        assert canonical_agent.snapshot().to_dict() == direct_agent.snapshot().to_dict()


def test_authoritative_dedup_runs_once_per_route_and_key() -> None:
    canonical_agent, canonical_world, state = _canonical_stack(_loader())
    assert canonical_agent.record(_grounded())
    assert not canonical_agent.record(_grounded())
    assert canonical_world.apply_event(_world_event(event_id="same-key"))
    assert not canonical_world.apply_event(_world_event(event_id="same-key"))
    assert state.snapshot().revision == 2
    assert state.get_metrics()["authoritative_state_events"] == {
        "accepted": 2,
        "duplicate": 2,
    }


@pytest.mark.asyncio
async def test_perception_source_uses_same_canonical_ingress_without_world_mutation() -> None:
    loader = _loader()
    agent, world = _stores(loader)
    normalizer = CanonicalEventNormalizer.from_loader(loader)
    state = AuthoritativeStateReducer(
        AuthoritativeStateConfig.from_loader(loader),
        agent_state=agent,
        world_model=world,
        clock=lambda: NOW,
    )
    ingress = CanonicalEventIngress(state)
    perception_store = PerceptionIngress.from_loader(
        loader, world_model=world, enabled=True, clock=lambda: NOW,
    )
    state.bind_perception_ingress(perception_store)
    perception = CanonicalPerceptionIngressAdapter(perception_store, normalizer, ingress)
    await perception.start()
    event = PerceptionEvent(
        schema_version=1,
        event_id="chat-observation-1",
        source="chat_youtube",
        event_type="input.received",
        timestamp=NOW,
        payload={"content": "hello", "metadata": {}},
        provenance=PerceptionProvenance(
            producer="chat_perception_adapter",
            source_event_id="input-1",
        ),
        confidence=1.0,
        dedup_key="chat:input-1",
    )
    assert perception.submit(event)
    assert not perception.submit(event)
    assert perception.recent_events() == (event,)
    assert state.snapshot().revision == 1
    assert world.snapshot().to_dict()["stream"] == {}
    await perception.stop()


def test_state_config_is_canonical_and_legacy_reads_are_exact_aliases() -> None:
    loader = _loader()
    assert loader.get("agent_state", "agent_state") == loader.get("state", "agent_state")
    assert loader.get("agent_state", "world_model") == loader.get("state", "world_model")
    assert loader.get("relationships", "relationships") == loader.get(
        "state", "relationships",
    )
    assert loader.path_for("state") == ROOT / "config" / "state.yaml"


def test_live_composition_uses_canonical_state_paths() -> None:
    source = (ROOT / "orchestrator" / "stream_runtime.py").read_text(encoding="utf-8")
    assert "from services.state.agent import AgentState" in source
    assert "from services.state.world import WorldModelShadow" in source
    assert "CanonicalAgentStateAdapter(" in source
    assert "CanonicalWorldModelAdapter(" in source
    assert "CanonicalPerceptionIngressAdapter(" in source
    assert "authoritative_state=authoritative_state" in source
    assert "m.update(self._authoritative_state.get_metrics())" in source


def test_live_and_replay_code_do_not_import_legacy_state_implementations() -> None:
    forbidden = {
        "services.agent.agent_state",
        "services.agent.event_ledger",
        "services.world.world_model",
        "services.self_model.projection",
    }
    violations: list[str] = []
    for source_root in (ROOT / "orchestrator", ROOT / "scripts"):
        for path in source_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden:
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}",
                            )
    assert violations == []
