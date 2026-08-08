"""Immutable grounded-event and agent-state value objects (Master Plan M1.1)."""
from __future__ import annotations

from dataclasses import dataclass, field
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


class StreamPhase(str, Enum):
    OPENING = "opening"
    MAIN = "main"
    CHAT = "chat"
    CLOSING = "closing"


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
class TopicState:
    summary: str
    source_event_id: str
    updated_at: datetime
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "updated_at", _as_utc(self.updated_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "source_event_id": self.source_event_id,
            "updated_at": self.updated_at.isoformat(),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class OpenThread:
    thread_id: str
    topic: str
    summary: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _as_utc(self.created_at))
        object.__setattr__(self, "updated_at", _as_utc(self.updated_at))
        object.__setattr__(self, "expires_at", _as_utc(self.expires_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "topic": self.topic,
            "summary": self.summary,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class AgentStateSnapshot:
    current_topic: TopicState | None = None
    open_threads: tuple[OpenThread, ...] = ()
    active_goal_ref: str | None = None
    recent_events: tuple[GroundedEvent, ...] = ()
    environment_summary: Mapping[str, Any] | None = None
    stream_phase: StreamPhase = StreamPhase.OPENING
    last_spoken_summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_threads", tuple(self.open_threads))
        object.__setattr__(self, "recent_events", tuple(self.recent_events))
        if self.environment_summary is not None:
            object.__setattr__(
                self, "environment_summary", _freeze_mapping(self.environment_summary),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_topic": self.current_topic.to_dict() if self.current_topic else None,
            "open_threads": [thread.to_dict() for thread in self.open_threads],
            "active_goal_ref": self.active_goal_ref,
            "recent_events": [event.to_dict() for event in self.recent_events],
            "environment_summary": (
                _thaw(self.environment_summary) if self.environment_summary is not None else None
            ),
            "stream_phase": self.stream_phase.value,
            "last_spoken_summary": self.last_spoken_summary,
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
