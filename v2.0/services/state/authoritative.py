"""Single canonical mutation boundary over the existing deterministic domain reducers."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import EventProvenance as PerceptionProvenance
from interfaces.compatibility import PerceptionEvent
from interfaces.events import CanonicalEvent, CanonicalEventRoute, GroundedEvent
from interfaces.state import AuthoritativeStateService, AuthoritativeStateSnapshot
from interfaces.events import AgentEventKind, AgentEventSource


@dataclass(frozen=True)
class AuthoritativeStateConfig:
    dedup_ttl_s: float
    max_dedup_keys: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.dedup_ttl_s, bool)
            or not isinstance(self.dedup_ttl_s, (int, float))
            or float(self.dedup_ttl_s) <= 0
        ):
            raise ValueError("state.authoritative.dedup_ttl_s must be positive")
        if (
            isinstance(self.max_dedup_keys, bool)
            or not isinstance(self.max_dedup_keys, int)
            or self.max_dedup_keys <= 0
        ):
            raise ValueError("state.authoritative.max_dedup_keys must be positive")
        object.__setattr__(self, "dedup_ttl_s", float(self.dedup_ttl_s))

    @classmethod
    def from_loader(cls, loader: Any) -> "AuthoritativeStateConfig":
        raw = loader.get("state", "authoritative", None)
        if not isinstance(raw, Mapping):
            raise ValueError("state.authoritative must be a mapping")
        return cls(
            dedup_ttl_s=raw.get("dedup_ttl_s"),
            max_dedup_keys=raw.get("max_dedup_keys"),
        )


class AuthoritativeStateReducer(AuthoritativeStateService):
    service_id = "authoritative_state"

    def __init__(
        self,
        config: AuthoritativeStateConfig,
        *,
        agent_state: Any,
        world_model: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, AuthoritativeStateConfig):
            raise ValueError("config must be AuthoritativeStateConfig")
        if not hasattr(agent_state, "record") or not hasattr(agent_state, "snapshot"):
            raise ValueError("agent_state reducer is invalid")
        if not hasattr(world_model, "apply_event") or not hasattr(world_model, "snapshot"):
            raise ValueError("world_model reducer is invalid")
        self._config = config
        self._agent_state = agent_state
        self._world_model = world_model
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._self_model: Any = None
        self._goal_manager: Any = None
        self._relationship_manager: Any = None
        self._perception_ingress: Any = None
        self._seen: OrderedDict[tuple[str, str], datetime] = OrderedDict()
        self._revision = 0
        self._last_event_id: str | None = None
        self._running = False
        self._outcomes: dict[str, int] = {}

    def bind_read_providers(
        self,
        *,
        self_model: Any = None,
        goal_manager: Any = None,
        relationship_manager: Any = None,
    ) -> None:
        self._self_model = self_model
        self._goal_manager = goal_manager
        self._relationship_manager = relationship_manager

    def bind_perception_ingress(self, ingress: Any) -> None:
        if not hasattr(ingress, "submit") or not hasattr(ingress, "recent_events"):
            raise ValueError("perception ingress is invalid")
        self._perception_ingress = ingress

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        return (
            HealthStatus.healthy(self.service_id, revision=self._revision, dedup=len(self._seen))
            if self._running else HealthStatus.stopped(self.service_id)
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "authoritative_state_revision": self._revision,
            "authoritative_state_dedup_keys": len(self._seen),
            "authoritative_state_events": dict(sorted(self._outcomes.items())),
        }

    def apply(self, event: CanonicalEvent) -> bool:
        if not isinstance(event, CanonicalEvent):
            self._record("invalid")
            return False
        now = _utc(self._clock())
        self._evict(now)
        key = (event.route.value, event.dedup_key)
        if key in self._seen:
            self._record("duplicate")
            return False
        try:
            if event.route is CanonicalEventRoute.AGENT:
                accepted = self._agent_state.record(_to_grounded(event)) is True
            elif event.route in {
                CanonicalEventRoute.WORLD,
                CanonicalEventRoute.OBSERVATION,
            }:
                perception = _to_perception(event)
                if self._perception_ingress is not None:
                    accepted = self._perception_ingress.submit(perception) is True
                elif event.route is CanonicalEventRoute.WORLD:
                    accepted = self._world_model.apply_event(perception) is True
                else:
                    self._record("perception_unavailable")
                    return False
            else:
                self._record("unsupported_route")
                return False
        except Exception:
            self._record("reducer_error")
            return False
        if not accepted:
            self._record("domain_rejected")
            return False
        self._seen[key] = now + timedelta(seconds=self._config.dedup_ttl_s)
        while len(self._seen) > self._config.max_dedup_keys:
            self._seen.popitem(last=False)
            self._record("dedup_evicted")
        self._revision += 1
        self._last_event_id = event.event_id
        self._record("accepted")
        return True

    def snapshot(self) -> AuthoritativeStateSnapshot:
        return AuthoritativeStateSnapshot(
            revision=self._revision,
            created_at=_utc(self._clock()),
            last_event_id=self._last_event_id,
            agent=self._agent_state.snapshot(),
            world=self._world_model.snapshot(),
            self_state=_snapshot_or_none(self._self_model),
            goals=_snapshot_or_none(self._goal_manager),
            relationships=_snapshot_or_none(self._relationship_manager),
        )

    def _evict(self, now: datetime) -> None:
        expired = [key for key, expires_at in self._seen.items() if expires_at <= now]
        for key in expired:
            del self._seen[key]

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1


def _to_grounded(event: CanonicalEvent) -> GroundedEvent:
    return GroundedEvent(
        event_id=event.event_id,
        kind=AgentEventKind(event.event_type),
        source=AgentEventSource(event.source),
        timestamp=event.timestamp,
        confidence=event.confidence,
        payload=dict(event.payload),
        provenance=event.provenance,
    )


def _to_perception(event: CanonicalEvent) -> PerceptionEvent:
    return PerceptionEvent(
        schema_version=event.schema_version,
        event_id=event.event_id,
        source=event.source,
        event_type=event.event_type,
        timestamp=event.timestamp,
        payload=dict(event.payload),
        provenance=PerceptionProvenance(
            producer=event.provenance.producer,
            source_event_id=event.provenance.source_event_id,
            session_id=event.provenance.session_id,
            platform=event.provenance.platform,
        ),
        confidence=event.confidence,
        dedup_key=event.dedup_key,
    )


def _snapshot_or_none(provider: Any) -> Any:
    if provider is None or not hasattr(provider, "snapshot"):
        return None
    try:
        return provider.snapshot()
    except Exception:
        return None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("authoritative state clock must be timezone-aware")
    return value.astimezone(timezone.utc)
