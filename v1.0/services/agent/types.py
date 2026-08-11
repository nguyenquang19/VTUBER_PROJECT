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


class StreamPhase(str, Enum):
    OPENING = "opening"
    MAIN = "main"
    CHAT = "chat"
    CLOSING = "closing"


class ThreadKind(str, Enum):
    QUESTION = "question"
    PROMISE = "promise"
    STORY = "story"


class ThreadStatus(str, Enum):
    ACTIVE = "active"
    WAITING = "waiting"
    PARKED = "parked"


class ThreadSpeaker(str, Enum):
    VIEWER = "viewer"
    MAI = "mai"
    SYSTEM = "system"


class ConversationMove(str, Enum):
    CLARIFY = "clarify"
    DEEPEN = "deepen"
    COMPARE = "compare"
    CHALLENGE = "challenge"
    SYNTHESIZE = "synthesize"
    REVISE = "revise"
    INVITE = "invite"
    SUMMARIZE = "summarize"
    PARK = "park"
    RESUME = "resume"
    CLOSE = "close"


class ThreadOperation(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    RESOLVE = "resolve"


@dataclass(frozen=True)
class ThreadSignal:
    operation: ThreadOperation
    kind: ThreadKind
    topic: str
    summary: str
    evidence: "ThreadEvidence"
    target_thread_id: str | None = None
    reason: str | None = None
    speaker: ThreadSpeaker = ThreadSpeaker.SYSTEM
    status: ThreadStatus | None = None
    move: ConversationMove | None = None
    is_open_question: bool = False


@dataclass(frozen=True)
class ThreadEvidence:
    source_event_id: str
    excerpt: str
    detector: str
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.source_event_id.strip() or not self.excerpt.strip():
            raise ValueError("thread evidence needs source_event_id and excerpt")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("thread evidence confidence must be within [0, 1]")
        object.__setattr__(self, "confidence", float(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_id": self.source_event_id,
            "excerpt": self.excerpt,
            "detector": self.detector,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ThreadContribution:
    source_event_id: str
    text: str
    speaker: ThreadSpeaker

    def __post_init__(self) -> None:
        if not self.source_event_id.strip() or not self.text.strip():
            raise ValueError("thread contribution needs source_event_id and text")

    def to_dict(self) -> dict[str, str]:
        return {
            "source_event_id": self.source_event_id,
            "text": self.text,
            "speaker": self.speaker.value,
        }


@dataclass(frozen=True)
class TopicMatch:
    thread_id: str
    score: float
    shared_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.thread_id.strip():
            raise ValueError("topic match needs thread_id")
        if not 0.0 <= float(self.score) <= 1.0:
            raise ValueError("topic match score must be within [0, 1]")
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "shared_terms", tuple(self.shared_terms))


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
    kind: ThreadKind = ThreadKind.QUESTION
    evidence: tuple[ThreadEvidence, ...] = ()
    origin_event_id: str | None = None
    status: ThreadStatus = ThreadStatus.ACTIVE
    claims: tuple[ThreadContribution, ...] = ()
    viewer_contributions: tuple[ThreadContribution, ...] = ()
    open_questions: tuple[ThreadContribution, ...] = ()
    last_move: ConversationMove | None = None
    next_move: ConversationMove | None = None
    move_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _as_utc(self.created_at))
        object.__setattr__(self, "updated_at", _as_utc(self.updated_at))
        object.__setattr__(self, "expires_at", _as_utc(self.expires_at))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "viewer_contributions", tuple(self.viewer_contributions))
        object.__setattr__(self, "open_questions", tuple(self.open_questions))
        object.__setattr__(self, "move_count", max(0, int(self.move_count)))
        if not self.thread_id.strip() or not self.topic.strip() or not self.summary.strip():
            raise ValueError("open thread needs id, topic, and summary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "topic": self.topic,
            "summary": self.summary,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "kind": self.kind.value,
            "evidence": [item.to_dict() for item in self.evidence],
            "origin_event_id": self.origin_event_id,
            "status": self.status.value,
            "claims": [item.to_dict() for item in self.claims],
            "viewer_contributions": [item.to_dict() for item in self.viewer_contributions],
            "open_questions": [item.to_dict() for item in self.open_questions],
            "last_move": self.last_move.value if self.last_move else None,
            "next_move": self.next_move.value if self.next_move else None,
            "move_count": self.move_count,
        }


@dataclass(frozen=True)
class SessionRecapItem:
    source_event_id: str
    kind: AgentEventKind
    summary: str
    timestamp: datetime
    producer: str

    def __post_init__(self) -> None:
        if not self.source_event_id.strip() or not self.summary.strip():
            raise ValueError("recap item needs source event and summary")
        object.__setattr__(self, "timestamp", _as_utc(self.timestamp))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_id": self.source_event_id,
            "kind": self.kind.value,
            "summary": self.summary,
            "timestamp": self.timestamp.isoformat(),
            "producer": self.producer,
        }


@dataclass(frozen=True)
class SessionRecap:
    items: tuple[SessionRecapItem, ...] = ()
    total_chars: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "total_chars", sum(len(item.summary) for item in self.items))

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total_chars": self.total_chars,
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
    session_recap: SessionRecap | None = None

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
            "session_recap": self.session_recap.to_dict() if self.session_recap else None,
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
