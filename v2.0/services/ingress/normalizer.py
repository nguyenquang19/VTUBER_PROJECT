"""Pure adapters from legacy input/event shapes to the canonical ingress contract."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from interfaces.compatibility import (
    EventProvenance as PerceptionProvenance,
    PerceptionEvent,
)
from interfaces.events import (
    AgentEventKind,
    AgentEventSource,
    CanonicalEvent,
    CanonicalEventRoute,
    EventProvenance,
    GroundedEvent,
)
from interfaces.input import InputEvent


@dataclass(frozen=True)
class CanonicalNormalizerConfig:
    max_payload_items: int
    max_payload_chars: int

    def __post_init__(self) -> None:
        for name in ("max_payload_items", "max_payload_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"state.canonical_event.{name} must be a positive integer")

    @classmethod
    def from_loader(cls, loader: Any) -> "CanonicalNormalizerConfig":
        raw = loader.get("state", "canonical_event", None)
        if not isinstance(raw, Mapping):
            raise ValueError("state.canonical_event must be a mapping")
        return cls(
            max_payload_items=raw.get("max_payload_items"),
            max_payload_chars=raw.get("max_payload_chars"),
        )


class CanonicalEventNormalizer:
    """Validate, bound and map source events without mutating domain state."""

    def __init__(self, config: CanonicalNormalizerConfig) -> None:
        if not isinstance(config, CanonicalNormalizerConfig):
            raise ValueError("config must be CanonicalNormalizerConfig")
        self._config = config

    @classmethod
    def from_loader(cls, loader: Any) -> "CanonicalEventNormalizer":
        return cls(CanonicalNormalizerConfig.from_loader(loader))

    def from_grounded(self, event: GroundedEvent) -> CanonicalEvent:
        if not isinstance(event, GroundedEvent):
            raise ValueError("grounded event is invalid")
        payload = self._bounded(event.payload)
        return CanonicalEvent(
            schema_version=1,
            event_id=event.event_id,
            route=CanonicalEventRoute.AGENT,
            source=event.source.value,
            event_type=event.kind.value,
            timestamp=event.timestamp,
            confidence=event.confidence,
            payload=payload,
            provenance=event.provenance,
            dedup_key=event.event_id,
        )

    def from_perception(self, event: PerceptionEvent) -> CanonicalEvent:
        if not isinstance(event, PerceptionEvent):
            raise ValueError("perception event is invalid")
        payload = self._bounded(event.payload)
        return CanonicalEvent(
            schema_version=1,
            event_id=event.event_id,
            route=(
                CanonicalEventRoute.WORLD
                if event.event_type == "world.observation"
                else CanonicalEventRoute.OBSERVATION
            ),
            source=event.source,
            event_type=event.event_type,
            timestamp=event.timestamp,
            confidence=event.confidence,
            payload=payload,
            provenance=EventProvenance(
                producer=event.provenance.producer,
                source_event_id=event.provenance.source_event_id,
                session_id=event.provenance.session_id,
                platform=event.provenance.platform,
            ),
            dedup_key=event.dedup_key or event.event_id,
        )

    def from_input(self, event: InputEvent) -> CanonicalEvent:
        if not isinstance(event, InputEvent):
            raise ValueError("input event is invalid")
        metadata = _sanitize_input_metadata(event.metadata)
        payload = self._bounded({"content": event.content, "metadata": metadata})
        return CanonicalEvent(
            schema_version=1,
            event_id=event.event_id,
            route=CanonicalEventRoute.OBSERVATION,
            source=event.source.value,
            event_type="input_observation",
            timestamp=event.timestamp,
            confidence=1.0,
            payload=payload,
            provenance=EventProvenance(
                producer="canonical_input_adapter",
                source_event_id=event.event_id,
                platform=event.source.value,
            ),
            dedup_key=event.event_id,
        )

    @staticmethod
    def to_grounded(event: CanonicalEvent) -> GroundedEvent:
        if event.route is not CanonicalEventRoute.AGENT:
            raise ValueError("canonical event is not routed to agent state")
        return GroundedEvent(
            event_id=event.event_id,
            kind=AgentEventKind(event.event_type),
            source=AgentEventSource(event.source),
            timestamp=event.timestamp,
            confidence=event.confidence,
            payload=dict(event.payload),
            provenance=event.provenance,
        )

    @staticmethod
    def to_perception(event: CanonicalEvent) -> PerceptionEvent:
        if event.route not in {CanonicalEventRoute.WORLD, CanonicalEventRoute.OBSERVATION}:
            raise ValueError("canonical event is not a perception route")
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

    def _bounded(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("canonical source payload must be a mapping")
        if _item_count(payload) > self._config.max_payload_items:
            raise ValueError("canonical source payload exceeds item bound")
        try:
            encoded = json.dumps(
                _json_value(payload), ensure_ascii=False, separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("canonical source payload is not JSON-safe") from exc
        if len(encoded) > self._config.max_payload_chars:
            raise ValueError("canonical source payload exceeds character bound")
        return payload


def _sanitize_input_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    blocked = {
        "viewer_id", "user_id", "user_name", "author_id", "author_name",
        "display_name", "chain_of_thought", "cot",
    }
    secret_markers = (
        "token", "secret", "password", "credential", "authorization", "api_key",
    )
    for raw_key, item in value.items():
        key = str(raw_key).strip()
        normalized = key.casefold().replace("-", "_")
        if (
            not key
            or normalized in blocked
            or any(marker in normalized for marker in secret_markers)
        ):
            continue
        result[key] = item
    return result


def _item_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return len(value) + sum(_item_count(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return len(value) + sum(_item_count(item) for item in value)
    return 0


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(type(value).__name__)
