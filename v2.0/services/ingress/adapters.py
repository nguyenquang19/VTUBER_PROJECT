"""Compatibility adapters that route legacy state writes through canonical ingress."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from interfaces.agent import AgentStateService
from interfaces.base import HealthStatus
from interfaces.events import CanonicalEvent, CanonicalEventIngressService, GroundedEvent
from interfaces.perception import PerceptionIngressService
from interfaces.state import AgentStateSnapshot
from interfaces.world import WorldModelService
from interfaces.compatibility import PerceptionEvent, StateValue, WorldSnapshot
from services.ingress.normalizer import CanonicalEventNormalizer


class CanonicalEventIngress(CanonicalEventIngressService):
    service_id = "canonical_event_ingress"

    def __init__(self, state: Any) -> None:
        if not hasattr(state, "apply"):
            raise ValueError("state must implement apply")
        self._state = state
        self._running = False
        self._accepted = 0
        self._rejected = 0

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        return (
            HealthStatus.healthy(self.service_id, accepted=self._accepted)
            if self._running else HealthStatus.stopped(self.service_id)
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "canonical_ingress_accepted_total": self._accepted,
            "canonical_ingress_rejected_total": self._rejected,
        }

    def submit(self, event: CanonicalEvent) -> bool:
        if not isinstance(event, CanonicalEvent):
            self._rejected += 1
            return False
        try:
            accepted = self._state.apply(event) is True
        except Exception:
            accepted = False
        if accepted:
            self._accepted += 1
        else:
            self._rejected += 1
        return accepted


class CanonicalAgentStateAdapter(AgentStateService):
    service_id = "agent_state"

    def __init__(
        self,
        delegate: AgentStateService,
        normalizer: CanonicalEventNormalizer,
        ingress: CanonicalEventIngressService,
    ) -> None:
        if not isinstance(delegate, AgentStateService):
            raise ValueError("delegate must implement AgentStateService")
        self._delegate = delegate
        self._normalizer = normalizer
        self._ingress = ingress

    async def start(self) -> None:
        await self._delegate.start()

    async def stop(self) -> None:
        await self._delegate.stop()

    async def health_check(self) -> HealthStatus:
        return await self._delegate.health_check()

    def get_metrics(self) -> dict[str, Any]:
        return self._delegate.get_metrics()

    def record(self, event: GroundedEvent) -> bool:
        try:
            return self._ingress.submit(self._normalizer.from_grounded(event))
        except (TypeError, ValueError):
            return False

    def snapshot(self) -> AgentStateSnapshot:
        return self._delegate.snapshot()

    def set_active_goal_ref(self, goal_id: str | None) -> None:
        self._delegate.set_active_goal_ref(goal_id)

    def add_event_listener(
        self, listener: Callable[[GroundedEvent, AgentStateSnapshot], None],
    ) -> None:
        self._delegate.add_event_listener(listener)


class CanonicalWorldModelAdapter(WorldModelService):
    service_id = "world_model_shadow"

    def __init__(
        self,
        delegate: WorldModelService,
        normalizer: CanonicalEventNormalizer,
        ingress: CanonicalEventIngressService,
    ) -> None:
        if not isinstance(delegate, WorldModelService):
            raise ValueError("delegate must implement WorldModelService")
        self._delegate = delegate
        self._normalizer = normalizer
        self._ingress = ingress

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._delegate, "enabled", True))

    def set_enabled(self, enabled: bool) -> None:
        setter = getattr(self._delegate, "set_enabled", None)
        if not callable(setter):
            raise ValueError("world delegate does not support feature toggling")
        setter(enabled)

    async def start(self) -> None:
        await self._delegate.start()

    async def stop(self) -> None:
        await self._delegate.stop()

    async def health_check(self) -> HealthStatus:
        return await self._delegate.health_check()

    def get_metrics(self) -> dict[str, Any]:
        return self._delegate.get_metrics()

    def apply_event(self, event: PerceptionEvent) -> bool:
        try:
            return self._ingress.submit(self._normalizer.from_perception(event))
        except (TypeError, ValueError):
            return False

    def snapshot(self) -> WorldSnapshot:
        return self._delegate.snapshot()

    def query(self, path: str) -> StateValue | None:
        return self._delegate.query(path)

    def evict_stale(self, now: datetime) -> int:
        return self._delegate.evict_stale(now)


class CanonicalPerceptionIngressAdapter(PerceptionIngressService):
    service_id = "perception_ingress"

    def __init__(
        self,
        delegate: PerceptionIngressService,
        normalizer: CanonicalEventNormalizer,
        ingress: CanonicalEventIngressService,
    ) -> None:
        if not isinstance(delegate, PerceptionIngressService):
            raise ValueError("delegate must implement PerceptionIngressService")
        self._delegate = delegate
        self._normalizer = normalizer
        self._ingress = ingress

    @property
    def config(self) -> Any:
        return getattr(self._delegate, "config")

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._delegate, "enabled", True))

    async def start(self) -> None:
        await self._delegate.start()

    async def stop(self) -> None:
        await self._delegate.stop()

    async def health_check(self) -> HealthStatus:
        return await self._delegate.health_check()

    def get_metrics(self) -> dict[str, Any]:
        return self._delegate.get_metrics()

    def set_enabled(self, enabled: bool) -> None:
        self._delegate.set_enabled(enabled)

    def submit(self, event: PerceptionEvent) -> bool:
        try:
            return self._ingress.submit(self._normalizer.from_perception(event))
        except (TypeError, ValueError):
            return False

    def recent_events(self) -> tuple[PerceptionEvent, ...]:
        return self._delegate.recent_events()
