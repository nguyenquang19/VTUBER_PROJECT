"""Deterministic, bounded World Model reducer used only in Phase 2 shadow mode."""
from __future__ import annotations

import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import EventProvenance, PerceptionEvent, StateValue, WorldSnapshot
from interfaces.world import WorldModelService


_EVENT_TYPE = "world.observation"
_WORLD_DOMAINS = frozenset({"stream", "social", "call", "media", "physical", "game"})


@dataclass(frozen=True)
class WorldModelConfig:
    allowed_domains: tuple[str, ...]
    default_ttl_s: float
    source_authority: Mapping[str, int]
    max_state_entries: int
    max_evidence_refs: int
    max_payload_items: int
    max_payload_chars: int
    dedup_ttl_s: float
    max_dedup_keys: int

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_domains, tuple):
            raise ValueError("world_model.allowed_domains must be a tuple")
        domains = tuple(
            _required_text(value, field_name="world_model.allowed_domains")
            for value in self.allowed_domains
        )
        if not domains or len(set(domains)) != len(domains):
            raise ValueError("world_model.allowed_domains must be unique and non-empty")
        if set(domains) - _WORLD_DOMAINS:
            raise ValueError("world_model.allowed_domains contains an unsupported domain")
        if not isinstance(self.source_authority, Mapping) or not self.source_authority:
            raise ValueError("world_model.source_authority must be a non-empty mapping")
        authority: dict[str, int] = {}
        for raw_key, raw_value in self.source_authority.items():
            key = _required_text(raw_key, field_name="world_model.source_authority key")
            authority[key] = _non_negative_int(
                raw_value, field_name=f"world_model.source_authority.{key}",
            )
        if len(authority) != len(self.source_authority):
            raise ValueError("world_model.source_authority keys must be unique after normalization")

        object.__setattr__(self, "allowed_domains", domains)
        object.__setattr__(self, "default_ttl_s", _positive_number(
            self.default_ttl_s, field_name="world_model.default_ttl_s",
        ))
        object.__setattr__(self, "source_authority", MappingProxyType(authority))
        for name in (
            "max_state_entries", "max_evidence_refs", "max_payload_items",
            "max_payload_chars", "max_dedup_keys",
        ):
            object.__setattr__(self, name, _positive_int(
                getattr(self, name), field_name=f"world_model.{name}",
            ))
        object.__setattr__(self, "dedup_ttl_s", _positive_number(
            self.dedup_ttl_s, field_name="world_model.dedup_ttl_s",
        ))

    @classmethod
    def from_loader(cls, loader: Any) -> "WorldModelConfig":
        raw = loader.get("agent_state", "world_model", {}) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("world_model config must be a mapping")
        domains_raw = raw.get("allowed_domains", ())
        if not isinstance(domains_raw, (tuple, list)):
            raise ValueError("world_model.allowed_domains must be a sequence")
        authority_raw = raw.get("source_authority", {})
        if not isinstance(authority_raw, Mapping):
            raise ValueError("world_model.source_authority must be a mapping")
        return cls(
            allowed_domains=tuple(domains_raw),
            default_ttl_s=raw.get("default_ttl_s", 0),
            source_authority=authority_raw,
            max_state_entries=raw.get("max_state_entries", 0),
            max_evidence_refs=raw.get("max_evidence_refs", 0),
            max_payload_items=raw.get("max_payload_items", 0),
            max_payload_chars=raw.get("max_payload_chars", 0),
            dedup_ttl_s=raw.get("dedup_ttl_s", 0),
            max_dedup_keys=raw.get("max_dedup_keys", 0),
        )


class WorldModelShadow(WorldModelService):
    """In-memory shadow reducer with explicit inputs and no decision consumers."""

    service_id = "world_model_shadow"

    def __init__(
        self,
        config: WorldModelConfig,
        *,
        metrics: Any = None,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        self._enabled = bool(enabled)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._state: dict[str, dict[str, StateValue]] = {
            domain: {} for domain in self._config.allowed_domains
        }
        self._dedup: OrderedDict[str, datetime] = OrderedDict()
        self._revision = 0
        self._running = False
        self._events: dict[tuple[str, str], int] = {}
        self._evicted_total = 0
        self._sync_entry_gauge()

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        metrics: Any = None,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> "WorldModelShadow":
        return cls(WorldModelConfig.from_loader(loader), metrics=metrics, enabled=enabled, clock=clock)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._enabled and not enabled:
            self._state = {domain: {} for domain in self._config.allowed_domains}
            self._dedup.clear()
            self._revision += 1
            self._sync_entry_gauge()
        self._enabled = enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if self._enabled:
            self.evict_stale(_utc(self._clock()))
        return HealthStatus.healthy(self.service_id, enabled=self._enabled, entries=self._entry_count())

    def apply_event(self, event: PerceptionEvent) -> bool:
        now = _utc(self._clock())
        if not self._enabled:
            self._record("disabled", "feature_disabled")
            return False
        self.evict_stale(now)
        try:
            domain, key, value, evidence_refs, authority = self._validated(event)
        except (TypeError, ValueError):
            self._record("rejected", "invalid")
            return False

        expires_at = event.timestamp + timedelta(seconds=self._config.default_ttl_s)
        if expires_at <= now:
            self._record("rejected", "stale")
            return False

        dedup_key = event.dedup_key or event.event_id
        if dedup_key in self._dedup:
            self._record("duplicate", "dedup")
            return False

        bucket = self._state[domain]
        existing = bucket.get(key)
        if existing is not None and _is_stale(existing, now):
            del bucket[key]
            existing = None
            self._revision += 1
        if existing is not None:
            if authority < existing.authority:
                self._record("rejected", "lower_authority")
                return False
            if authority == existing.authority and event.timestamp <= existing.updated_at:
                self._record("rejected", "not_newer")
                return False
        elif self._entry_count() >= self._config.max_state_entries:
            self._record("rejected", "capacity")
            return False

        bucket[key] = StateValue(
            value=value,
            source=event.source,
            confidence=event.confidence,
            updated_at=event.timestamp,
            evidence_refs=evidence_refs,
            expires_at=expires_at,
            authority=authority,
        )
        self._dedup[dedup_key] = now + timedelta(seconds=self._config.dedup_ttl_s)
        while len(self._dedup) > self._config.max_dedup_keys:
            self._dedup.popitem(last=False)
        self._revision += 1
        self._sync_entry_gauge()
        self._record("accepted", "updated" if existing is not None else "new")
        return True

    def snapshot(self) -> WorldSnapshot:
        now = _utc(self._clock())
        self.evict_stale(now)
        if not self._enabled:
            return WorldSnapshot(snapshot_id=f"world-{self._revision}", created_at=now)
        values = {
            domain: {key: bucket[key] for key in sorted(bucket)}
            for domain, bucket in self._state.items()
        }
        return WorldSnapshot(
            snapshot_id=f"world-{self._revision}",
            created_at=now,
            stream=values.get("stream", {}),
            social=values.get("social", {}),
            call=values.get("call", {}),
            media=values.get("media", {}),
            physical=values.get("physical", {}),
            game=values.get("game", {}),
        )

    def query(self, path: str) -> StateValue | None:
        now = _utc(self._clock())
        self.evict_stale(now)
        try:
            domain, key = self._split_path(path)
        except (TypeError, ValueError):
            return None
        value = self._state[domain].get(key)
        if value is None or not self._enabled:
            return None
        return value

    def evict_stale(self, now: datetime) -> int:
        now = _utc(now)
        removed = 0
        for bucket in self._state.values():
            stale = [key for key, value in bucket.items() if _is_stale(value, now)]
            for key in stale:
                del bucket[key]
            removed += len(stale)
        if removed:
            self._revision += 1
            self._evicted_total += removed
            self._sync_entry_gauge()
            if self._metrics is not None and hasattr(self._metrics, "record_world_model_eviction"):
                self._metrics.record_world_model_eviction(removed)
        self._evict_dedup(now)
        return removed

    def get_metrics(self) -> dict[str, Any]:
        if self._enabled:
            self.evict_stale(_utc(self._clock()))
        return {
            "world_model_enabled": self._enabled,
            "world_model_state_entries": self._entry_count() if self._enabled else 0,
            "world_model_evicted_total": self._evicted_total,
            "world_model_events": {
                f"{outcome}:{reason}": count
                for (outcome, reason), count in sorted(self._events.items())
            },
        }

    def _validated(self, event: PerceptionEvent) -> tuple[str, str, Any, tuple[str, ...], int]:
        if not isinstance(event, PerceptionEvent) or event.event_type != _EVENT_TYPE:
            raise ValueError("unsupported event")
        if event.source not in self._config.source_authority:
            raise ValueError("unknown source")
        if set(event.payload) - {"path", "value", "evidence_refs"} or "path" not in event.payload or "value" not in event.payload:
            raise ValueError("invalid world observation payload")
        domain, key = self._split_path(event.payload["path"])
        value = event.payload["value"]
        if _item_count(event.payload) > self._config.max_payload_items:
            raise ValueError("payload item limit")
        encoded = json.dumps(
            event.to_dict()["payload"], ensure_ascii=False, separators=(",", ":"),
        )
        if len(encoded) > self._config.max_payload_chars:
            raise ValueError("payload character limit")
        refs_raw = event.payload.get("evidence_refs", ())
        if not isinstance(refs_raw, (tuple, list)):
            raise ValueError("invalid evidence refs")
        supplied_refs = tuple(
            _required_text(ref, field_name="evidence_ref") for ref in refs_raw
        )
        source_event_id = event.provenance.source_event_id or event.event_id
        provenance_ref = f"{event.provenance.producer}:{source_event_id}"
        refs = _bounded_unique_strings(
            (provenance_ref, *supplied_refs), self._config.max_evidence_refs,
        )
        return domain, key, value, refs, self._config.source_authority[event.source]

    def _split_path(self, path: str) -> tuple[str, str]:
        clean_path = _required_text(path, field_name="world path")
        domain, separator, key = clean_path.partition(".")
        if not separator or not domain or not key or domain not in self._state:
            raise ValueError("invalid world path")
        if any(not part or part != part.strip() for part in key.split(".")):
            raise ValueError("invalid world path")
        return domain, key

    def _evict_dedup(self, now: datetime) -> None:
        expired = [key for key, expires_at in self._dedup.items() if expires_at <= now]
        for key in expired:
            del self._dedup[key]

    def _entry_count(self) -> int:
        return sum(len(bucket) for bucket in self._state.values())

    def _sync_entry_gauge(self) -> None:
        if self._metrics is not None and hasattr(self._metrics, "set_world_model_entries"):
            self._metrics.set_world_model_entries(self._entry_count() if self._enabled else 0)

    def _record(self, outcome: str, reason: str) -> None:
        key = (outcome, reason)
        self._events[key] = self._events.get(key, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_world_model_event"):
            self._metrics.record_world_model_event(outcome, reason)


def perception_event_from_grounded_observation(event: Any) -> PerceptionEvent | None:
    """Bridge only explicitly structured environment observations into shadow input."""
    kind = getattr(getattr(event, "kind", None), "value", None)
    if kind != "environment_observed":
        return None
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping) or "state_path" not in payload or "value" not in payload:
        return None
    world_payload: dict[str, Any] = {"path": payload["state_path"], "value": payload["value"]}
    if "evidence_refs" in payload:
        world_payload["evidence_refs"] = payload["evidence_refs"]
    try:
        return PerceptionEvent(
            schema_version=1,
            event_id=str(getattr(event, "event_id")),
            source=str(getattr(getattr(event, "source", None), "value", "")),
            event_type=_EVENT_TYPE,
            timestamp=getattr(event, "timestamp"),
            payload=world_payload,
            provenance=EventProvenance.from_legacy(getattr(event, "provenance")),
            confidence=float(getattr(event, "confidence")),
            dedup_key=str(getattr(event, "event_id")),
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _item_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return len(value) + sum(_item_count(item) for item in value.values())
    if isinstance(value, tuple):
        return len(value) + sum(_item_count(item) for item in value)
    return 0


def _is_stale(value: StateValue, now: datetime) -> bool:
    return value.expires_at is not None and value.expires_at <= now


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("world model time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _required_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must not be empty")
    return clean


def _positive_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return number


def _positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def _non_negative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _bounded_unique_strings(values: tuple[str, ...], limit: int) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return tuple(result)
