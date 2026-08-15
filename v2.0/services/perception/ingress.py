"""Bounded canonical perception ingress; it never calls Director or an SDK."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import PerceptionEvent, perception_event_from_input
from interfaces.input import EventSource, InputEvent
from interfaces.perception import PerceptionIngressService
from services.world.world_model import perception_event_from_grounded_observation


@dataclass(frozen=True)
class PerceptionIngressConfig:
    max_payload_items: int
    max_payload_chars: int
    max_recent_events: int
    allowed_input_sources: tuple[str, ...]

    @classmethod
    def from_loader(cls, loader: Any) -> "PerceptionIngressConfig":
        raw = loader.get("agent_state", "perception", {}) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("perception config must be a mapping")
        sources = tuple(str(value).strip() for value in raw.get("allowed_input_sources", ()))
        config = cls(
            max_payload_items=int(raw.get("max_payload_items", 0)),
            max_payload_chars=int(raw.get("max_payload_chars", 0)),
            max_recent_events=int(raw.get("max_recent_events", 0)),
            allowed_input_sources=sources,
        )
        if min(config.max_payload_items, config.max_payload_chars, config.max_recent_events) <= 0:
            raise ValueError("perception bounds must be positive")
        if not sources or any(not source for source in sources) or len(set(sources)) != len(sources):
            raise ValueError("perception.allowed_input_sources must be unique and non-empty")
        return config


class PerceptionIngress(PerceptionIngressService):
    """Single receive boundary for current input and grounded-environment adapters."""

    service_id = "perception_ingress"

    def __init__(
        self,
        config: PerceptionIngressConfig,
        *,
        world_model: Any = None,
        metrics: Any = None,
        enabled: bool = True,
    ) -> None:
        self._config = config
        self._world_model = world_model
        self._metrics = metrics
        self._enabled = bool(enabled)
        self._running = False
        self._events: deque[PerceptionEvent] = deque(maxlen=config.max_recent_events)
        self._outcomes: dict[tuple[str, str], int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, world_model: Any = None, metrics: Any = None, enabled: bool = True,
    ) -> "PerceptionIngress":
        return cls(PerceptionIngressConfig.from_loader(loader), world_model=world_model, metrics=metrics, enabled=enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, enabled=self._enabled, retained=len(self._events))

    def observe_input(self, event: InputEvent) -> PerceptionEvent | None:
        if not self._enabled:
            self._record("disabled", "feature_disabled")
            return None
        source = getattr(getattr(event, "source", None), "value", "")
        if source not in self._config.allowed_input_sources:
            self._record("rejected", "source_not_allowed")
            return None
        try:
            perception = perception_event_from_input(
                event,
                max_payload_items=self._config.max_payload_items,
                max_payload_chars=self._config.max_payload_chars,
                producer=self.service_id,
            )
        except (AttributeError, TypeError, ValueError):
            self._record("rejected", "invalid_input")
            return None
        self._events.append(perception)
        self._record("accepted", source)
        return perception

    def observe_grounded(self, event: Any, state: Any = None) -> bool:
        """World shadow receives only the existing structured environment bridge."""
        del state
        if not self._enabled:
            self._record("disabled", "feature_disabled")
            return False
        perception = perception_event_from_grounded_observation(event)
        if perception is None:
            self._record("rejected", "not_grounded_environment")
            return False
        if self._world_model is None:
            self._record("rejected", "world_unavailable")
            return False
        accepted = bool(self._world_model.apply_event(perception))
        self._record("accepted" if accepted else "rejected", "world_observation")
        return accepted

    def recent_events(self) -> tuple[PerceptionEvent, ...]:
        return tuple(self._events)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "perception_enabled": self._enabled,
            "perception_recent_events": len(self._events),
            "perception_events": {
                f"{outcome}:{source}": count
                for (outcome, source), count in sorted(self._outcomes.items())
            },
        }

    def _record(self, outcome: str, source: str) -> None:
        key = (str(outcome), str(source))
        self._outcomes[key] = self._outcomes.get(key, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_perception_event"):
            self._metrics.record_perception_event(*key)
        if self._metrics is not None and hasattr(self._metrics, "set_perception_recent_events"):
            self._metrics.set_perception_recent_events(len(self._events))
