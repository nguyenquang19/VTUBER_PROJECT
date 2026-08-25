"""Canonical grounded-event contracts shared across runtime subsystems."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


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


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


__all__ = ["AgentEventKind", "AgentEventSource", "EventProvenance", "GroundedEvent"]
