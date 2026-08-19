from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from interfaces.base import HealthStatus
from interfaces.external_executor import OBSCommandAck, OBSSceneState, OBSSceneTransportService
from interfaces.input import EventSource, InputEvent
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)
from services.perception.adapters import (
    ChatCompatibilityAdapter,
    OBSPerceptionAdapter,
    SystemPerceptionAdapter,
)
from services.perception.ingress import (
    PerceptionIngress,
    PerceptionIngressConfig,
    SystemGroundedRoute,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _config(**changes: object) -> PerceptionIngressConfig:
    values: dict[str, object] = {
        "max_payload_items": 24,
        "max_payload_chars": 1024,
        "max_recent_events": 16,
        "max_event_age_s": 60.0,
        "max_future_skew_s": 2.0,
        "dedup_ttl_s": 30.0,
        "max_dedup_keys": 16,
        "chat_producer": "chat_perception_adapter",
        "chat_sources": ("chat_youtube", "chat_discord"),
        "system_producer": "system_perception_adapter",
        "system_input_sources": ("system_timer", "dashboard"),
        "system_grounded_routes": (
            SystemGroundedRoute(
                "stream_runtime", "runtime", "runtime", "stream.runtime", "snapshot",
            ),
        ),
        "obs_producer": "obs_perception_adapter",
        "obs_source": "environment",
        "obs_world_path": "stream.current_scene",
        "obs_poll_interval_s": 3600.0,
        "obs_query_timeout_s": 0.05,
    }
    values.update(changes)
    return PerceptionIngressConfig(**values)  # type: ignore[arg-type]


class FakeWorld:
    def __init__(self) -> None:
        self.events = []

    def apply_event(self, event) -> bool:
        self.events.append(event)
        return True


class FakeOBSTransport(OBSSceneTransportService):
    service_id = "fake_obs"

    def __init__(self, scene: str = "Main") -> None:
        self.scene = scene
        self.get_calls = 0
        self.set_calls: list[str] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.delay_s = 0.0
        self.result: object | None = None

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, int]:
        return {"get_calls": self.get_calls}

    async def get_current_program_scene(self) -> OBSSceneState:
        self.get_calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.result is not None:
            return self.result  # type: ignore[return-value]
        return OBSSceneState(self.scene, f"obs:get:{self.get_calls}")

    async def set_current_program_scene(self, scene_name: str) -> OBSCommandAck:
        self.set_calls.append(scene_name)
        return OBSCommandAck("set:1", True, "obs:set:1")


class ManualOBSPerceptionAdapter(OBSPerceptionAdapter):
    """Keep unit mapping scenarios independent from the background scheduler."""

    def _start_task(self) -> None:
        return None


def _input(source: EventSource, event_id: str = "input:1", **metadata: object) -> InputEvent:
    return InputEvent(
        event_id=event_id,
        timestamp=NOW,
        source=source,
        user_id="private-user",
        user_name="Private Name",
        content="xin chao",
        metadata=metadata,
    )


def _runtime_observation(event_id: str = "runtime:1", producer: str = "stream_runtime") -> GroundedEvent:
    return GroundedEvent(
        event_id=event_id,
        kind=AgentEventKind.ENVIRONMENT_OBSERVED,
        source=AgentEventSource.RUNTIME,
        timestamp=NOW,
        confidence=1.0,
        payload={"source_services": ["youtube"], "tts_enabled": True},
        provenance=EventProvenance(producer=producer, session_id="session:1"),
    )


def test_chat_adapter_sanitizes_metadata_and_never_projects_world() -> None:
    async def scenario() -> None:
        world = FakeWorld()
        ingress = PerceptionIngress(_config(), world_model=world, clock=lambda: NOW)
        adapter = ChatCompatibilityAdapter(_config(), ingress)
        await ingress.start()
        await adapter.start()

        assert adapter.observe_input(_input(EventSource.CHAT_YOUTUBE, token="secret", public="ok"))
        event = ingress.recent_events()[0]
        assert event.event_type == "input.received"
        assert event.payload["metadata"] == {"public": "ok"}
        assert "private-user" not in str(event.to_dict())
        assert world.events == []

    asyncio.run(scenario())


def test_system_input_is_signal_only_and_voice_remains_deferred() -> None:
    async def scenario() -> None:
        world = FakeWorld()
        ingress = PerceptionIngress(_config(), world_model=world, clock=lambda: NOW)
        adapter = SystemPerceptionAdapter(_config(), ingress)
        await ingress.start()
        await adapter.start()

        assert adapter.observe_input(_input(EventSource.SYSTEM_TIMER))
        assert adapter.observe_input(_input(EventSource.VOICE_OPERATOR, "voice:1")) is False
        assert ingress.recent_events()[0].event_type == "system.signal"
        assert world.events == []

    asyncio.run(scenario())


def test_system_grounded_route_maps_runtime_snapshot_and_rejects_unknown_producer() -> None:
    async def scenario() -> None:
        world = FakeWorld()
        ingress = PerceptionIngress(_config(), world_model=world, clock=lambda: NOW)
        adapter = SystemPerceptionAdapter(_config(), ingress)
        await ingress.start()
        await adapter.start()

        assert adapter.observe_grounded(_runtime_observation())
        assert adapter.observe_grounded(_runtime_observation("runtime:2", "unknown")) is False
        assert len(world.events) == 1
        event = world.events[0]
        assert event.payload["path"] == "stream.runtime"
        assert event.payload["value"]["tts_enabled"] is True
        assert event.provenance.producer == "system_perception_adapter"

    asyncio.run(scenario())


def test_local_adapter_lifecycle_and_metric_failure_are_isolated() -> None:
    class BadMetrics:
        def record_perception_event(self, outcome: str, source: str) -> None:
            raise RuntimeError("metrics failed")

    async def scenario() -> None:
        ingress = PerceptionIngress(_config(), clock=lambda: NOW)
        adapter = ChatCompatibilityAdapter(_config(), ingress, metrics=BadMetrics())
        await ingress.start()
        assert adapter.observe_input(_input(EventSource.CHAT_YOUTUBE)) is False
        await adapter.start()
        assert adapter.observe_input(_input(EventSource.CHAT_YOUTUBE)) is True
        await adapter.set_enabled(False)
        assert adapter.observe_input(_input(EventSource.CHAT_YOUTUBE, "input:2")) is False
        await adapter.stop()
        assert (await adapter.health_check()).service_id == "chat_perception_adapter"

    asyncio.run(scenario())


def test_obs_feature_off_performs_no_io_and_never_owns_transport_lifecycle() -> None:
    async def scenario() -> None:
        ingress = PerceptionIngress(_config(), clock=lambda: NOW)
        transport = FakeOBSTransport()
        adapter = OBSPerceptionAdapter(_config(), ingress, transport, enabled=False, clock=lambda: NOW)
        await ingress.start()
        await adapter.start()
        await asyncio.sleep(0)
        assert transport.get_calls == 0
        assert await adapter.poll_once() is False
        await adapter.stop()
        assert transport.start_calls == 0
        assert transport.stop_calls == 0
        assert transport.set_calls == []

    asyncio.run(scenario())


def test_obs_query_submits_only_changes_and_accepts_change_away_then_return() -> None:
    async def scenario() -> None:
        current = [NOW]
        world = FakeWorld()
        ingress = PerceptionIngress(_config(), world_model=world, clock=lambda: current[0])
        transport = FakeOBSTransport("Main")
        adapter = ManualOBSPerceptionAdapter(
            _config(), ingress, transport, enabled=True, clock=lambda: current[0],
        )
        await ingress.start()
        await adapter.start()
        try:
            assert await adapter.poll_once() is True
            assert len(world.events) == 1

            assert await adapter.poll_once() is False
            current[0] += timedelta(seconds=1)
            transport.scene = "BRB"
            assert await adapter.poll_once() is True
            current[0] += timedelta(seconds=1)
            transport.scene = "Main"
            assert await adapter.poll_once() is True

            assert [event.payload["value"] for event in world.events] == ["Main", "BRB", "Main"]
            assert transport.set_calls == []
            assert adapter.get_metrics()["obs_perception_outcomes"]["unchanged"] == 1
        finally:
            await adapter.stop()
        assert transport.stop_calls == 0

    asyncio.run(scenario())


def test_obs_timeout_and_malformed_state_are_fail_isolated() -> None:
    async def scenario() -> None:
        ingress = PerceptionIngress(_config(obs_query_timeout_s=0.005), clock=lambda: NOW)
        transport = FakeOBSTransport()
        transport.delay_s = 0.02
        adapter = ManualOBSPerceptionAdapter(
            _config(obs_query_timeout_s=0.005), ingress, transport, enabled=True, clock=lambda: NOW,
        )
        await ingress.start()
        await adapter.start()
        try:
            assert await adapter.poll_once() is False
            assert adapter.get_metrics()["obs_perception_outcomes"]["poll_timeout"] == 1
            transport.delay_s = 0
            transport.result = object()
            assert await adapter.poll_once() is False
            assert adapter.get_metrics()["obs_perception_outcomes"]["invalid_state"] == 1
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_obs_background_poll_cancellation_propagates_without_stopping_transport() -> None:
    async def scenario() -> None:
        ingress = PerceptionIngress(_config(), clock=lambda: NOW)
        transport = FakeOBSTransport()
        transport.delay_s = 60
        adapter = OBSPerceptionAdapter(_config(), ingress, transport, enabled=True, clock=lambda: NOW)
        await ingress.start()
        await adapter.start()
        await asyncio.sleep(0)
        await asyncio.wait_for(adapter.stop(), timeout=0.2)
        assert transport.start_calls == 0
        assert transport.stop_calls == 0

    asyncio.run(scenario())


def test_deterministic_adapter_replay_has_identical_history_and_world() -> None:
    async def run_once() -> tuple[list[dict], list[dict]]:
        current = [NOW]
        world = FakeWorld()
        ingress = PerceptionIngress(_config(), world_model=world, clock=lambda: current[0])
        chat = ChatCompatibilityAdapter(_config(), ingress)
        system = SystemPerceptionAdapter(_config(), ingress)
        await ingress.start()
        await chat.start()
        await system.start()
        assert chat.observe_input(_input(EventSource.CHAT_DISCORD, "chat:1", public="ok"))
        assert system.observe_input(_input(EventSource.DASHBOARD, "system:1"))
        assert system.observe_grounded(_runtime_observation())
        return (
            [event.to_dict() for event in ingress.recent_events()],
            [event.to_dict() for event in world.events],
        )

    first = asyncio.run(run_once())
    second = asyncio.run(run_once())
    assert first == second
