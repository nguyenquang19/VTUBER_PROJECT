"""Phase 10 canonical perception ingress tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from interfaces.input import EventSource, InputEvent
from services.agent.types import AgentEventKind, AgentEventSource, EventProvenance, GroundedEvent
from services.perception.ingress import PerceptionIngress, PerceptionIngressConfig


class _World:
    def __init__(self) -> None:
        self.events = []

    def apply_event(self, event) -> bool:
        self.events.append(event)
        return True


def _config(**overrides: object) -> PerceptionIngressConfig:
    data: dict[str, object] = {
        "max_payload_items": 8,
        "max_payload_chars": 256,
        "max_recent_events": 2,
        "allowed_input_sources": ("chat_youtube", "chat_discord", "system_timer"),
    }
    data.update(overrides)
    return PerceptionIngressConfig(**data)  # type: ignore[arg-type]


def _input(source: EventSource = EventSource.CHAT_YOUTUBE, **metadata: object) -> InputEvent:
    return InputEvent(
        event_id="input:1", timestamp=datetime.now(timezone.utc), source=source,
        user_id="private-user", user_name="Private Name", content="xin chao", metadata=metadata,
    )


def _grounded(kind: AgentEventKind = AgentEventKind.ENVIRONMENT_OBSERVED) -> GroundedEvent:
    return GroundedEvent(
        event_id="grounded:1", kind=kind, source=AgentEventSource.ENVIRONMENT,
        timestamp=datetime.now(timezone.utc), confidence=0.9,
        payload={"state_path": "stream.live", "value": True, "evidence_refs": ["obs:1"]},
        provenance=EventProvenance(producer="test"),
    )


def test_input_is_sanitized_retained_and_never_calls_world() -> None:
    world = _World()
    ingress = PerceptionIngress(_config(), world_model=world)

    event = ingress.observe_input(_input(token="secret", public="ok"))

    assert event is not None
    assert event.payload["content"] == "xin chao"
    assert event.payload["metadata"] == {"public": "ok"}
    assert event.provenance.source_event_id == "input:1"
    assert ingress.recent_events() == (event,)
    assert world.events == []


def test_source_rejection_and_feature_disable_are_safe() -> None:
    ingress = PerceptionIngress(_config())
    assert ingress.observe_input(_input(EventSource.CHAT_TWITCH)) is None
    ingress.set_enabled(False)
    assert ingress.observe_input(_input()) is None
    metrics = ingress.get_metrics()["perception_events"]
    assert metrics["rejected:source_not_allowed"] == 1
    assert metrics["disabled:feature_disabled"] == 1


def test_only_structured_environment_observation_can_reach_world() -> None:
    world = _World()
    ingress = PerceptionIngress(_config(), world_model=world)

    assert not ingress.observe_grounded(_grounded(AgentEventKind.CHAT_RECEIVED))
    assert ingress.observe_grounded(_grounded())
    assert len(world.events) == 1
    assert world.events[0].event_type == "world.observation"


def test_lifecycle_and_history_bound_are_deterministic() -> None:
    ingress = PerceptionIngress(_config(max_recent_events=1))
    assert asyncio.run(ingress.health_check()).service_id == "perception_ingress"
    asyncio.run(ingress.start())
    ingress.observe_input(_input())
    ingress.observe_input(_input(public="different"))
    assert len(ingress.recent_events()) == 1
    assert asyncio.run(ingress.health_check()).is_ok
