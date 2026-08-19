"""Typed Chat, System and read-only OBS adapters for canonical perception."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import EventProvenance, PerceptionEvent, perception_event_from_input
from interfaces.external_executor import OBSSceneState, OBSSceneTransportService
from interfaces.input import EventSource, InputEvent
from interfaces.perception import PerceptionAdapterService, PerceptionIngressService
from services.agent.types import AgentEventKind, GroundedEvent
from services.perception.ingress import PerceptionIngressConfig, SystemGroundedRoute


def _canonical_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _valid_input(event: object) -> bool:
    return (
        isinstance(event, InputEvent)
        and _canonical_text(event.event_id)
        and isinstance(event.source, EventSource)
        and _aware(event.timestamp)
        and isinstance(event.content, str)
        and isinstance(event.metadata, dict)
    )


class _LocalPerceptionAdapter(PerceptionAdapterService):
    """Idempotent lifecycle shared by callback-only local adapters."""

    service_id = "perception_local_adapter"

    def __init__(
        self,
        ingress: PerceptionIngressService,
        *,
        enabled: bool,
        metrics: Any = None,
    ) -> None:
        if not isinstance(ingress, PerceptionIngressService):
            raise ValueError("ingress must implement PerceptionIngressService")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._ingress = ingress
        self._enabled = enabled
        self._metrics = metrics
        self._running = False
        self._outcomes: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._enabled = enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "feature_disabled")
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        prefix = self.service_id.replace("_adapter", "")
        return {
            f"{prefix}_enabled": self._enabled,
            f"{prefix}_running": self._running,
            f"{prefix}_outcomes": dict(sorted(self._outcomes.items())),
        }

    def _submit(self, event: PerceptionEvent) -> bool:
        try:
            result = self._ingress.submit(event)
        except Exception:
            self._record("ingress_exception")
            return False
        if result is True:
            self._record("submitted")
            return True
        if result is False:
            self._record("ingress_rejected")
            return False
        self._record("ingress_invalid_result")
        return False

    def _ready(self) -> bool:
        if not self._running:
            self._record("stopped")
            return False
        if not self._enabled:
            self._record("feature_disabled")
            return False
        return True

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        recorder = getattr(self._metrics, "record_perception_event", None)
        if callable(recorder):
            try:
                recorder(outcome, self.service_id)
            except Exception:
                pass


class ChatCompatibilityAdapter(_LocalPerceptionAdapter):
    """Map existing chat InputEvent values without touching World or Director."""

    service_id = "chat_perception_adapter"

    def __init__(
        self,
        config: PerceptionIngressConfig,
        ingress: PerceptionIngressService,
        *,
        enabled: bool = True,
        metrics: Any = None,
    ) -> None:
        if not isinstance(config, PerceptionIngressConfig):
            raise ValueError("config must be PerceptionIngressConfig")
        super().__init__(ingress, enabled=enabled, metrics=metrics)
        self._config = config

    def observe_input(self, event: InputEvent) -> bool:
        source = getattr(getattr(event, "source", None), "value", None)
        if source not in self._config.chat_sources:
            return False
        if not self._ready():
            return False
        if not _valid_input(event):
            self._record("invalid_input")
            return False
        try:
            canonical = perception_event_from_input(
                event,
                max_payload_items=self._config.max_payload_items,
                max_payload_chars=self._config.max_payload_chars,
                producer=self._config.chat_producer,
                event_type="input.received",
            )
        except (AttributeError, TypeError, ValueError):
            self._record("invalid_input")
            return False
        return self._submit(canonical)


class SystemPerceptionAdapter(_LocalPerceptionAdapter):
    """Map allowlisted system inputs and grounded runtime observations."""

    service_id = "system_perception_adapter"

    def __init__(
        self,
        config: PerceptionIngressConfig,
        ingress: PerceptionIngressService,
        *,
        enabled: bool = True,
        metrics: Any = None,
    ) -> None:
        if not isinstance(config, PerceptionIngressConfig):
            raise ValueError("config must be PerceptionIngressConfig")
        super().__init__(ingress, enabled=enabled, metrics=metrics)
        self._config = config
        self._grounded_routes = {
            (route.producer, route.event_source): route
            for route in config.system_grounded_routes
        }

    def observe_input(self, event: InputEvent) -> bool:
        source = getattr(getattr(event, "source", None), "value", None)
        if source not in self._config.system_input_sources:
            return False
        if not self._ready():
            return False
        if not _valid_input(event):
            self._record("invalid_input")
            return False
        try:
            canonical = perception_event_from_input(
                event,
                max_payload_items=self._config.max_payload_items,
                max_payload_chars=self._config.max_payload_chars,
                producer=self._config.system_producer,
                event_type="system.signal",
            )
        except (AttributeError, TypeError, ValueError):
            self._record("invalid_input")
            return False
        return self._submit(canonical)

    def observe_grounded(self, event: GroundedEvent, state: Any = None) -> bool:
        del state
        if not isinstance(event, GroundedEvent) or event.kind is not AgentEventKind.ENVIRONMENT_OBSERVED:
            return False
        if not self._ready():
            return False
        producer = getattr(getattr(event, "provenance", None), "producer", None)
        event_source = getattr(getattr(event, "source", None), "value", None)
        route = self._grounded_routes.get((producer, event_source))
        if route is None or not _canonical_text(event.event_id) or not _aware(event.timestamp):
            self._record("grounded_route_rejected")
            return False
        payload = self._world_payload(event, route)
        if payload is None:
            self._record("grounded_payload_rejected")
            return False
        try:
            canonical = PerceptionEvent(
                schema_version=1,
                event_id=event.event_id,
                source=route.canonical_source,
                event_type="world.observation",
                timestamp=event.timestamp,
                payload=payload,
                provenance=EventProvenance(
                    producer=self._config.system_producer,
                    source_event_id=event.event_id,
                    session_id=event.provenance.session_id,
                    platform=event.provenance.platform,
                ),
                confidence=event.confidence,
                dedup_key=f"system:{event.event_id}",
            )
        except (AttributeError, TypeError, ValueError):
            self._record("grounded_payload_rejected")
            return False
        return self._submit(canonical)

    @staticmethod
    def _world_payload(
        event: GroundedEvent, route: SystemGroundedRoute,
    ) -> Mapping[str, Any] | None:
        if not isinstance(event.payload, Mapping):
            return None
        if route.payload_mode == "snapshot":
            return {
                "path": route.world_path,
                "value": event.payload,
                "evidence_refs": (f"system:{event.event_id}",),
            }
        allowed = {"state_path", "value", "evidence_refs"}
        if set(event.payload) - allowed or set(event.payload) < {"state_path", "value"}:
            return None
        if event.payload.get("state_path") != route.world_path:
            return None
        refs = event.payload.get("evidence_refs", ())
        if not isinstance(refs, (tuple, list)):
            return None
        return {"path": route.world_path, "value": event.payload["value"], "evidence_refs": refs}


class OBSPerceptionAdapter(PerceptionAdapterService):
    """Poll authoritative OBS scene state and submit changes through ingress only."""

    service_id = "obs_perception_adapter"

    def __init__(
        self,
        config: PerceptionIngressConfig,
        ingress: PerceptionIngressService,
        transport: OBSSceneTransportService,
        *,
        enabled: bool = False,
        metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, PerceptionIngressConfig):
            raise ValueError("config must be PerceptionIngressConfig")
        if not isinstance(ingress, PerceptionIngressService):
            raise ValueError("ingress must implement PerceptionIngressService")
        if not isinstance(transport, OBSSceneTransportService):
            raise ValueError("transport must implement OBSSceneTransportService")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._config = config
        self._ingress = ingress
        self._transport = transport
        self._enabled = enabled
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_scene: str | None = None
        self._last_query_ok = False
        self._sequence = 0
        self._outcomes: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        if self._enabled == enabled:
            return
        self._enabled = enabled
        if not enabled:
            await self._cancel_task()
            self._last_scene = None
            self._last_query_ok = False
        elif self._running:
            self._start_task()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._enabled:
            self._start_task()

    async def stop(self) -> None:
        self._running = False
        await self._cancel_task()
        self._last_scene = None
        self._last_query_ok = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "feature_disabled")
        if self._task is None or self._task.done():
            return HealthStatus.unhealthy(self.service_id, "poll_task_stopped")
        if not self._last_query_ok:
            return HealthStatus.degraded(self.service_id, "no_successful_query")
        return HealthStatus.healthy(self.service_id)

    async def poll_once(self) -> bool:
        if not self._running:
            self._record("stopped")
            return False
        if not self._enabled:
            self._record("feature_disabled")
            return False
        try:
            state = await asyncio.wait_for(
                self._transport.get_current_program_scene(),
                timeout=self._config.obs_query_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._last_query_ok = False
            self._record("poll_timeout")
            return False
        except Exception:
            self._last_query_ok = False
            self._record("poll_failure")
            return False
        if not isinstance(state, OBSSceneState):
            self._last_query_ok = False
            self._record("invalid_state")
            return False
        self._last_query_ok = True
        if state.scene_name == self._last_scene:
            self._record("unchanged")
            return False
        try:
            timestamp = self._utc_now()
        except (TypeError, ValueError):
            self._record("clock_invalid")
            return False
        self._sequence += 1
        try:
            event = PerceptionEvent(
                schema_version=1,
                event_id=f"obs:scene:{self._sequence}",
                source=self._config.obs_source,
                event_type="world.observation",
                timestamp=timestamp,
                payload={
                    "path": self._config.obs_world_path,
                    "value": state.scene_name,
                    "evidence_refs": (state.evidence_ref,),
                },
                provenance=EventProvenance(
                    producer=self._config.obs_producer,
                    source_event_id=state.evidence_ref,
                    platform="obs_websocket",
                ),
                dedup_key=f"obs:scene:{self._sequence}",
            )
        except (TypeError, ValueError):
            self._record("invalid_state")
            return False
        try:
            accepted = self._ingress.submit(event)
        except Exception:
            self._record("ingress_exception")
            return False
        if accepted is not True:
            self._record("ingress_rejected" if accepted is False else "ingress_invalid_result")
            return False
        self._last_scene = state.scene_name
        self._record("submitted")
        return True

    def get_metrics(self) -> dict[str, Any]:
        return {
            "obs_perception_enabled": self._enabled,
            "obs_perception_running": self._running,
            "obs_perception_poll_task_active": self._task is not None and not self._task.done(),
            "obs_perception_has_observation": self._last_scene is not None,
            "obs_perception_outcomes": dict(sorted(self._outcomes.items())),
        }

    def _start_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop(), name="obs_perception_poll")

    async def _cancel_task(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _poll_loop(self) -> None:
        while self._running and self._enabled:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._last_query_ok = False
                self._record("poll_internal_error")
            await asyncio.sleep(self._config.obs_poll_interval_s)

    def _utc_now(self) -> datetime:
        value = self._clock()
        if not _aware(value):
            raise ValueError("OBS perception clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        recorder = getattr(self._metrics, "record_perception_event", None)
        if callable(recorder):
            try:
                recorder(outcome, self.service_id)
            except Exception:
                pass
