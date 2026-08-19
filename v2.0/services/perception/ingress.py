"""Strict, bounded canonical perception ingress with no decision ownership."""
from __future__ import annotations

import json
import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import PerceptionEvent
from interfaces.perception import PerceptionIngressService


_CHAT_EVENT_TYPE = "input.received"
_SYSTEM_EVENT_TYPE = "system.signal"
_WORLD_EVENT_TYPE = "world.observation"
_PAYLOAD_MODES = frozenset({"snapshot", "structured"})


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a canonical non-empty string")
    return value


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and bool(value.strip()) and value == value.strip() else None


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive int")
    return value


def _positive_number(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be a finite positive number")
    return float(value)


def _non_negative_number(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return float(value)


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{field_name} must be a sequence")
    result = tuple(_text(item, field_name) for item in value)
    if not result or len(set(result)) != len(result):
        raise ValueError(f"{field_name} must be unique and non-empty")
    return result


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _exact_keys(raw: Mapping[str, Any], allowed: frozenset[str], field_name: str) -> None:
    if set(raw) != allowed:
        raise ValueError(f"{field_name} keys must be exactly {sorted(allowed)}")


@dataclass(frozen=True)
class SystemGroundedRoute:
    producer: str
    event_source: str
    canonical_source: str
    world_path: str
    payload_mode: str

    def __post_init__(self) -> None:
        for field_name in ("producer", "event_source", "canonical_source", "world_path"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        if self.payload_mode not in _PAYLOAD_MODES:
            raise ValueError("payload_mode must be snapshot or structured")
        if "." not in self.world_path:
            raise ValueError("world_path must contain a domain and key")


@dataclass(frozen=True)
class PerceptionIngressConfig:
    max_payload_items: int
    max_payload_chars: int
    max_recent_events: int
    max_event_age_s: float
    max_future_skew_s: float
    dedup_ttl_s: float
    max_dedup_keys: int
    chat_producer: str
    chat_sources: tuple[str, ...]
    system_producer: str
    system_input_sources: tuple[str, ...]
    system_grounded_routes: tuple[SystemGroundedRoute, ...]
    obs_producer: str
    obs_source: str
    obs_world_path: str
    obs_poll_interval_s: float
    obs_query_timeout_s: float

    def __post_init__(self) -> None:
        for name in ("max_payload_items", "max_payload_chars", "max_recent_events", "max_dedup_keys"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))
        for name in ("max_event_age_s", "dedup_ttl_s", "obs_poll_interval_s", "obs_query_timeout_s"):
            object.__setattr__(self, name, _positive_number(getattr(self, name), name))
        object.__setattr__(
            self, "max_future_skew_s",
            _non_negative_number(self.max_future_skew_s, "max_future_skew_s"),
        )
        for name in ("chat_producer", "system_producer", "obs_producer", "obs_source", "obs_world_path"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "chat_sources", _string_tuple(self.chat_sources, "chat_sources"))
        object.__setattr__(
            self, "system_input_sources",
            _string_tuple(self.system_input_sources, "system_input_sources"),
        )
        if set(self.chat_sources) & set(self.system_input_sources):
            raise ValueError("chat and system input sources must not overlap")
        if not isinstance(self.system_grounded_routes, tuple) or not self.system_grounded_routes:
            raise ValueError("system_grounded_routes must be a non-empty tuple")
        if not all(isinstance(route, SystemGroundedRoute) for route in self.system_grounded_routes):
            raise ValueError("system_grounded_routes must contain typed routes")
        route_keys = tuple((route.producer, route.event_source) for route in self.system_grounded_routes)
        if len(set(route_keys)) != len(route_keys):
            raise ValueError("system grounded producer/source routes must be unique")
        if "." not in self.obs_world_path:
            raise ValueError("obs_world_path must contain a domain and key")
        producers = (self.chat_producer, self.system_producer, self.obs_producer)
        if len(set(producers)) != len(producers):
            raise ValueError("perception adapter producers must be unique")

    @classmethod
    def from_loader(cls, loader: Any) -> "PerceptionIngressConfig":
        raw = _mapping(loader.get("agent_state", "perception", None), "perception")
        _exact_keys(raw, frozenset({
            "max_payload_items", "max_payload_chars", "max_recent_events", "max_event_age_s",
            "max_future_skew_s", "dedup_ttl_s", "max_dedup_keys", "chat", "system", "obs",
        }), "perception")
        chat = _mapping(raw.get("chat"), "perception.chat")
        _exact_keys(chat, frozenset({"producer", "sources"}), "perception.chat")
        system = _mapping(raw.get("system"), "perception.system")
        _exact_keys(
            system, frozenset({"producer", "input_sources", "grounded_routes"}),
            "perception.system",
        )
        obs = _mapping(raw.get("obs"), "perception.obs")
        _exact_keys(
            obs,
            frozenset({"producer", "source", "world_path", "poll_interval_s", "query_timeout_s"}),
            "perception.obs",
        )
        routes_raw = system.get("grounded_routes")
        if not isinstance(routes_raw, (tuple, list)) or not routes_raw:
            raise ValueError("perception.system.grounded_routes must be a non-empty sequence")
        routes: list[SystemGroundedRoute] = []
        route_fields = frozenset({
            "producer", "event_source", "canonical_source", "world_path", "payload_mode",
        })
        for index, item in enumerate(routes_raw):
            route = _mapping(item, f"perception.system.grounded_routes[{index}]")
            _exact_keys(route, route_fields, f"perception.system.grounded_routes[{index}]")
            routes.append(SystemGroundedRoute(**route))
        chat_sources = chat.get("sources")
        system_sources = system.get("input_sources")
        parsed_chat_sources = _string_tuple(chat_sources, "perception.chat.sources")
        parsed_system_sources = _string_tuple(
            system_sources, "perception.system.input_sources",
        )
        return cls(
            max_payload_items=raw.get("max_payload_items"),
            max_payload_chars=raw.get("max_payload_chars"),
            max_recent_events=raw.get("max_recent_events"),
            max_event_age_s=raw.get("max_event_age_s"),
            max_future_skew_s=raw.get("max_future_skew_s"),
            dedup_ttl_s=raw.get("dedup_ttl_s"),
            max_dedup_keys=raw.get("max_dedup_keys"),
            chat_producer=chat.get("producer"),
            chat_sources=parsed_chat_sources,
            system_producer=system.get("producer"),
            system_input_sources=parsed_system_sources,
            system_grounded_routes=tuple(routes),
            obs_producer=obs.get("producer"),
            obs_source=obs.get("source"),
            obs_world_path=obs.get("world_path"),
            obs_poll_interval_s=obs.get("poll_interval_s"),
            obs_query_timeout_s=obs.get("query_timeout_s"),
        )


class PerceptionIngress(PerceptionIngressService):
    """The only admission and optional World projection boundary."""

    service_id = "perception_ingress"

    def __init__(
        self,
        config: PerceptionIngressConfig,
        *,
        world_model: Any = None,
        metrics: Any = None,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, PerceptionIngressConfig):
            raise ValueError("config must be PerceptionIngressConfig")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._config = config
        self._world_model = world_model
        self._metrics = metrics
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False
        self._events: deque[PerceptionEvent] = deque(maxlen=config.max_recent_events)
        self._dedup: OrderedDict[str, datetime] = OrderedDict()
        self._outcomes: dict[tuple[str, str, str], int] = {}
        self._evicted_total = 0

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        world_model: Any = None,
        metrics: Any = None,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> "PerceptionIngress":
        return cls(
            PerceptionIngressConfig.from_loader(loader), world_model=world_model,
            metrics=metrics, enabled=enabled, clock=clock,
        )

    @property
    def config(self) -> PerceptionIngressConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        if self._enabled and not enabled:
            self._clear_caches()
        self._enabled = enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._clear_caches()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, enabled=self._enabled, retained=len(self._events), dedup=len(self._dedup),
        )

    def submit(self, event: PerceptionEvent) -> bool:
        source = self._metric_source(event)
        if not self._running:
            self._record("rejected", source, "stopped")
            return False
        if not self._enabled:
            self._record("rejected", source, "feature_disabled")
            return False
        if not isinstance(event, PerceptionEvent):
            self._record("rejected", "unknown", "invalid_type")
            return False
        reason = self._route_error(event)
        if reason is not None:
            self._record("rejected", source, reason)
            return False
        if self._payload_item_count(event.payload) > self._config.max_payload_items:
            self._record("rejected", source, "payload_items")
            return False
        try:
            encoded = json.dumps(event.to_dict()["payload"], ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            self._record("rejected", source, "payload_encoding")
            return False
        if len(encoded) > self._config.max_payload_chars:
            self._record("rejected", source, "payload_chars")
            return False
        try:
            now = self._utc_now()
        except (TypeError, ValueError):
            self._record("rejected", source, "clock_invalid")
            return False
        if event.timestamp < now - timedelta(seconds=self._config.max_event_age_s):
            self._record("rejected", source, "stale")
            return False
        if event.timestamp > now + timedelta(seconds=self._config.max_future_skew_s):
            self._record("rejected", source, "future")
            return False
        self._evict_dedup(now)
        dedup_key = event.dedup_key or event.event_id
        if dedup_key in self._dedup:
            self._record("duplicate", source, "dedup")
            return False

        evicted = len(self._events) == self._events.maxlen
        self._events.append(event)
        if evicted:
            self._evicted_total += 1
            self._record("evicted", source, "recent_capacity")
        self._dedup[dedup_key] = now + timedelta(seconds=self._config.dedup_ttl_s)
        while len(self._dedup) > self._config.max_dedup_keys:
            self._dedup.popitem(last=False)
            self._evicted_total += 1
            self._record("evicted", source, "dedup_capacity")
        self._record("accepted", source, "admitted")
        self._sync_recent_gauge()
        if event.event_type == _WORLD_EVENT_TYPE:
            self._project_world(event, source)
        return True

    def recent_events(self) -> tuple[PerceptionEvent, ...]:
        return tuple(self._events)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "perception_enabled": self._enabled,
            "perception_running": self._running,
            "perception_recent_events": len(self._events),
            "perception_dedup_keys": len(self._dedup),
            "perception_evicted_total": self._evicted_total,
            "perception_events": {
                f"{outcome}:{source}:{reason}": count
                for (outcome, source, reason), count in sorted(self._outcomes.items())
            },
        }

    def _route_error(self, event: PerceptionEvent) -> str | None:
        if event.schema_version != 1:
            return "schema_version"
        producer = event.provenance.producer
        if producer == self._config.chat_producer:
            if event.source not in self._config.chat_sources or event.event_type != _CHAT_EVENT_TYPE:
                return "route_not_allowed"
            return None if set(event.payload) == {"content", "metadata"} else "payload_schema"
        if producer == self._config.system_producer:
            if event.event_type == _SYSTEM_EVENT_TYPE:
                if event.source not in self._config.system_input_sources:
                    return "route_not_allowed"
                return None if set(event.payload) == {"content", "metadata"} else "payload_schema"
            if event.event_type != _WORLD_EVENT_TYPE:
                return "route_not_allowed"
            payload_error = self._world_payload_error(event)
            if payload_error is not None:
                return payload_error
            path = event.payload.get("path")
            allowed = any(
                event.source == route.canonical_source and path == route.world_path
                for route in self._config.system_grounded_routes
            )
            return None if allowed else "world_path_not_allowed"
        if producer == self._config.obs_producer:
            payload_error = self._world_payload_error(event)
            if payload_error is not None:
                return payload_error
            if (
                event.source == self._config.obs_source
                and event.event_type == _WORLD_EVENT_TYPE
                and event.payload.get("path") == self._config.obs_world_path
                and _text_or_none(event.payload.get("value")) is not None
            ):
                return None
            return "route_not_allowed"
        return "producer_not_allowed"

    @staticmethod
    def _world_payload_error(event: PerceptionEvent) -> str | None:
        allowed = {"path", "value", "evidence_refs"}
        if set(event.payload) - allowed or set(event.payload) < {"path", "value"}:
            return "payload_schema"
        path = event.payload.get("path")
        if not isinstance(path, str) or not path.strip() or path != path.strip():
            return "world_path_not_allowed"
        refs = event.payload.get("evidence_refs", ())
        if not isinstance(refs, tuple) or any(
            not isinstance(ref, str) or not ref.strip() or ref != ref.strip() for ref in refs
        ):
            return "payload_schema"
        return None

    def _project_world(self, event: PerceptionEvent, source: str) -> None:
        if self._world_model is None:
            self._record("projection_rejected", source, "world_unavailable")
            return
        try:
            result = self._world_model.apply_event(event)
        except Exception:
            self._record("projection_error", source, "world_exception")
            return
        if result is True:
            self._record("projection_accepted", source, "world_applied")
        elif result is False:
            self._record("projection_rejected", source, "world_rejected")
        else:
            self._record("projection_error", source, "world_invalid_result")

    def _metric_source(self, event: object) -> str:
        source = getattr(event, "source", None)
        allowed = {
            *self._config.chat_sources, *self._config.system_input_sources,
            self._config.obs_source,
            *(route.canonical_source for route in self._config.system_grounded_routes),
        }
        return source if isinstance(source, str) and source in allowed else "unknown"

    def _utc_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("perception clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _evict_dedup(self, now: datetime) -> None:
        expired = [key for key, expires_at in self._dedup.items() if expires_at <= now]
        for key in expired:
            del self._dedup[key]

    def _clear_caches(self) -> None:
        self._events.clear()
        self._dedup.clear()
        self._sync_recent_gauge()

    def _sync_recent_gauge(self) -> None:
        recorder = getattr(self._metrics, "set_perception_recent_events", None)
        if callable(recorder):
            try:
                recorder(len(self._events))
            except Exception:
                pass

    def _record(self, outcome: str, source: str, reason: str) -> None:
        key = (outcome, source, reason)
        self._outcomes[key] = self._outcomes.get(key, 0) + 1
        recorder = getattr(self._metrics, "record_perception_event", None)
        if callable(recorder):
            try:
                recorder(f"{outcome}_{reason}", source)
            except Exception:
                pass

    @staticmethod
    def _payload_item_count(value: Any) -> int:
        if isinstance(value, Mapping):
            return len(value) + sum(PerceptionIngress._payload_item_count(item) for item in value.values())
        if isinstance(value, tuple):
            return len(value) + sum(PerceptionIngress._payload_item_count(item) for item in value)
        return 0
