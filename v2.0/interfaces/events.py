"""Canonical grounded-event contracts shared across runtime subsystems."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

from interfaces.base import Service


class AgentEventKind(str, Enum):
    CHAT_RECEIVED = "chat_received"
    DONATION_RECEIVED = "donation_received"
    EMOTION_APPLIED = "emotion_applied"
    DIRECTOR_ACTION = "director_action"
    SELF_TALK_COMPLETED = "self_talk_completed"
    SPEECH_FINAL = "speech_final"
    ENVIRONMENT_OBSERVED = "environment_observed"
    SPEECH_COMPLETED = "speech_completed"
    GOAL_AUDIT = "goal_audit"


class AgentEventSource(str, Enum):
    YOUTUBE = "youtube"
    DISCORD = "discord"
    CHAT = "chat"
    EMOTION = "emotion"
    DIRECTOR = "director"
    AUTONOMY = "autonomy"
    LLM = "llm"
    RUNTIME = "runtime"
    ENVIRONMENT = "environment"
    GOAL_MANAGER = "goal_manager"
    OPERATOR = "operator"


class CanonicalEventRoute(str, Enum):
    AGENT = "agent"
    WORLD = "world"
    OBSERVATION = "observation"


@dataclass(frozen=True)
class EventProvenance:
    producer: str
    source_event_id: str | None = None
    session_id: str | None = None
    platform: str | None = None

    def __post_init__(self) -> None:
        if not self.producer.strip():
            raise ValueError("provenance producer must not be empty")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "producer": self.producer,
            "source_event_id": self.source_event_id,
            "session_id": self.session_id,
            "platform": self.platform,
        }


@dataclass(frozen=True)
class GroundedEvent:
    event_id: str
    kind: AgentEventKind
    source: AgentEventSource
    timestamp: datetime
    confidence: float
    payload: Mapping[str, Any]
    provenance: EventProvenance

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        object.__setattr__(self, "timestamp", _as_utc(self.timestamp))
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "source": self.source.value,
            "timestamp": self.timestamp.isoformat(),
            "confidence": self.confidence,
            "payload": _thaw(self.payload),
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class CanonicalEvent:
    """Strict state-ingress event; one route receives one dedup decision."""

    schema_version: int
    event_id: str
    route: CanonicalEventRoute
    source: str
    event_type: str
    timestamp: datetime
    confidence: float
    payload: Mapping[str, Any]
    provenance: EventProvenance
    dedup_key: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise ValueError("canonical event schema_version must be 1")
        for name in ("event_id", "source", "event_type", "dedup_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"canonical event {name} must be a clean non-empty string")
        if not isinstance(self.route, CanonicalEventRoute):
            raise ValueError("canonical event route is invalid")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ValueError("canonical event timestamp must be timezone-aware")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("canonical event confidence must be finite within [0, 1]")
        if not isinstance(self.provenance, EventProvenance):
            raise ValueError("canonical event provenance is invalid")
        _reject_canonical_sensitive_keys(self.payload)
        object.__setattr__(self, "timestamp", _as_utc(self.timestamp))
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "payload", _strict_freeze_mapping(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "route": self.route.value,
            "source": self.source,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "confidence": self.confidence,
            "payload": _thaw(self.payload),
            "provenance": self.provenance.to_dict(),
            "dedup_key": self.dedup_key,
        }


class CanonicalEventIngressService(Service):
    @abstractmethod
    def submit(self, event: CanonicalEvent) -> bool:
        """Validate and route one canonical event through the authoritative reducer."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _strict_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _strict_freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_strict_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"canonical event payload type is unsupported: {type(value).__name__}")


def _strict_freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("canonical event payload must be a mapping")
    frozen: dict[str, Any] = {}
    for raw_key, item in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("canonical event payload keys must be non-empty strings")
        key = raw_key.strip()
        if key in frozen:
            raise ValueError("canonical event payload keys must be unique")
        frozen[key] = _strict_freeze(item)
    return MappingProxyType(frozen)


def _reject_canonical_sensitive_keys(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("canonical event payload must be a mapping")
    for raw_key, item in value.items():
        key = str(raw_key).casefold().replace("-", "_")
        identity_keys = {
            "viewer_id", "user_id", "user_name", "author_id", "author_name",
            "display_name", "chain_of_thought", "cot",
        }
        secret_markers = (
            "token", "secret", "password", "credential", "authorization", "api_key",
        )
        if key in identity_keys or any(marker in key for marker in secret_markers):
            raise ValueError("canonical event payload contains a sensitive key")
        if isinstance(item, Mapping):
            _reject_canonical_sensitive_keys(item)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                if isinstance(nested, Mapping):
                    _reject_canonical_sensitive_keys(nested)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


__all__ = [
    "AgentEventKind", "AgentEventSource", "CanonicalEvent", "CanonicalEventIngressService",
    "CanonicalEventRoute", "EventProvenance", "GroundedEvent",
]
