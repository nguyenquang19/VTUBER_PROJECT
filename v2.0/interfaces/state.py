"""Canonical immutable state, proposal, and crossing-decision contracts."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, field_validator

from interfaces.base import Service
from interfaces.compatibility import SelfSnapshot, WorldSnapshot
from interfaces.execution import OutcomeCommit
from interfaces.events import (
    AgentEventKind,
    AgentEventSource,
    CanonicalEvent,
    EventProvenance,
    GroundedEvent,
)
from interfaces.relationship import RelationshipSnapshot


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
class TopicState:
    summary: str
    source_event_id: str
    updated_at: datetime
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "updated_at", _state_as_utc(self.updated_at))

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
        object.__setattr__(self, "created_at", _state_as_utc(self.created_at))
        object.__setattr__(self, "updated_at", _state_as_utc(self.updated_at))
        object.__setattr__(self, "expires_at", _state_as_utc(self.expires_at))
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
        object.__setattr__(self, "timestamp", _state_as_utc(self.timestamp))

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
                self, "environment_summary", _state_freeze_mapping(self.environment_summary),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_topic": self.current_topic.to_dict() if self.current_topic else None,
            "open_threads": [thread.to_dict() for thread in self.open_threads],
            "active_goal_ref": self.active_goal_ref,
            "recent_events": [event.to_dict() for event in self.recent_events],
            "environment_summary": (
                _state_thaw(self.environment_summary)
                if self.environment_summary is not None else None
            ),
            "stream_phase": self.stream_phase.value,
            "last_spoken_summary": self.last_spoken_summary,
            "session_recap": self.session_recap.to_dict() if self.session_recap else None,
        }


@dataclass(frozen=True)
class AuthoritativeStateSnapshot:
    revision: int
    created_at: datetime
    last_event_id: str | None
    agent: AgentStateSnapshot
    world: WorldSnapshot
    self_state: SelfSnapshot | None = None
    goals: "GoalSnapshot | None" = None
    relationships: RelationshipSnapshot | None = None

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("authoritative state revision must be non-negative")
        object.__setattr__(self, "created_at", _state_as_utc(self.created_at))
        if self.last_event_id is not None and not self.last_event_id.strip():
            raise ValueError("authoritative last_event_id must not be blank")
        if not isinstance(self.agent, AgentStateSnapshot):
            raise ValueError("authoritative agent snapshot is invalid")
        if not isinstance(self.world, WorldSnapshot):
            raise ValueError("authoritative world snapshot is invalid")
        if self.self_state is not None and not isinstance(self.self_state, SelfSnapshot):
            raise ValueError("authoritative self snapshot is invalid")
        if self.goals is not None and not isinstance(self.goals, GoalSnapshot):
            raise ValueError("authoritative goal snapshot is invalid")
        if self.relationships is not None and not isinstance(
            self.relationships, RelationshipSnapshot,
        ):
            raise ValueError("authoritative relationship snapshot is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "last_event_id": self.last_event_id,
            "agent": self.agent.to_dict(),
            "world": self.world.to_dict(),
            "self": self.self_state.to_dict() if self.self_state else None,
            "goals": self.goals.to_dict() if self.goals else None,
            "relationships": self.relationships.to_dict() if self.relationships else None,
        }


class AuthoritativeStateService(Service):
    @abstractmethod
    def apply(self, event: CanonicalEvent) -> bool:
        """Apply one canonical event to exactly one domain reducer."""

    @abstractmethod
    def snapshot(self) -> AuthoritativeStateSnapshot:
        """Return the immutable aggregate view of authoritative domain owners."""


class ContinuityCommitDisposition(str, Enum):
    COMMITTED = "committed"
    DUPLICATE = "duplicate"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True)
class DeliveredTurnRecord:
    """Exact verified speech fact consumed by the next cognitive turn."""

    schema_version: int
    continuity_id: str
    outcome_ref: str
    transaction_id: str
    delivery_id: str
    session_id: str
    source_mode: str
    action_type: str
    speech_text: str
    history_input: str | None
    ref_event_ids: tuple[str, ...]
    goal_id: str | None
    intention_id: str | None
    thread_id: str | None
    conversation_move: str | None
    viewer_ref: str | None
    trigger_type: str | None
    output_ok: bool
    mood_dominant: str | None
    mood_intensity: int | None
    delivered_at: datetime
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("delivered turn schema_version must be 1")
        for name in (
            "continuity_id", "outcome_ref", "transaction_id", "delivery_id",
            "session_id", "source_mode", "action_type", "speech_text",
        ):
            object.__setattr__(
                self, name, _continuity_required(getattr(self, name), name),
            )
        for name in (
            "history_input", "goal_id", "intention_id", "thread_id",
            "conversation_move", "viewer_ref", "trigger_type", "mood_dominant",
        ):
            object.__setattr__(
                self, name, _continuity_optional(getattr(self, name), name),
            )
        object.__setattr__(
            self, "ref_event_ids", _continuity_strings(
                self.ref_event_ids, "ref_event_ids", allow_empty=True,
            ),
        )
        object.__setattr__(
            self, "evidence_refs", _continuity_strings(
                self.evidence_refs, "evidence_refs", allow_empty=False,
            ),
        )
        if not isinstance(self.output_ok, bool):
            raise ValueError("output_ok must be a bool")
        if self.mood_intensity is not None and (
            isinstance(self.mood_intensity, bool)
            or not isinstance(self.mood_intensity, int)
            or not 0 <= self.mood_intensity <= 10
        ):
            raise ValueError("mood_intensity must be an integer from 0 to 10")
        object.__setattr__(self, "delivered_at", _continuity_utc(self.delivered_at))


@dataclass(frozen=True)
class ContinuityCommitReceipt:
    schema_version: int
    continuity_id: str
    disposition: ContinuityCommitDisposition
    committed_facets: tuple[str, ...]
    failed_facets: tuple[str, ...]
    completed_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("continuity receipt schema_version must be 1")
        object.__setattr__(
            self, "continuity_id",
            _continuity_required(self.continuity_id, "continuity_id"),
        )
        if not isinstance(self.disposition, ContinuityCommitDisposition):
            raise ValueError("continuity receipt disposition is invalid")
        object.__setattr__(
            self, "committed_facets", _continuity_strings(
                self.committed_facets, "committed_facets", allow_empty=True,
            ),
        )
        object.__setattr__(
            self, "failed_facets", _continuity_strings(
                self.failed_facets, "failed_facets", allow_empty=True,
            ),
        )
        object.__setattr__(self, "completed_at", _continuity_utc(self.completed_at))


class ContinuityStateService(Service):
    @abstractmethod
    def commit_verified(
        self, outcome: OutcomeCommit, record: DeliveredTurnRecord,
    ) -> ContinuityCommitReceipt:
        """Commit one verified turn idempotently after transaction COMMITTED."""

    @abstractmethod
    def recent(self, limit: int | None = None) -> tuple[DeliveredTurnRecord, ...]:
        """Return bounded committed speech facts in commit order."""


class GoalKind(str, Enum):
    ACK_DONATION = "ack_donation"
    WAIT_FOR_CHAT_ANSWER = "wait_for_chat_answer"
    CONTINUE_THREAD = "continue_thread"
    ANSWER_FOLLOW_UP = "answer_follow_up"
    OPERATOR_PINNED = "operator_pinned"


class GoalStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GoalSource(str, Enum):
    RULE = "rule"
    OPERATOR = "operator"
    LLM_PROPOSAL = "llm_proposal"


class ShortIntentionStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class Goal:
    goal_id: str
    kind: GoalKind
    status: GoalStatus
    priority: int
    reason: str
    source: GoalSource
    created_at: datetime
    expires_at: datetime
    success_conditions: tuple[str, ...]
    steps: tuple[str, ...] = ()
    suspend_reason: str | None = None
    parent_thread_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal_id", _goal_required(self.goal_id, "goal_id"))
        if not isinstance(self.kind, GoalKind):
            raise ValueError("goal kind must be GoalKind")
        if not isinstance(self.status, GoalStatus):
            raise ValueError("goal status must be GoalStatus")
        if not isinstance(self.source, GoalSource):
            raise ValueError("goal source must be GoalSource")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("goal priority must be an integer")
        object.__setattr__(self, "reason", _goal_required(self.reason, "goal reason"))
        conditions = _goal_strict_strings(self.success_conditions, "success_conditions")
        if not conditions:
            raise ValueError("goal needs at least one success condition")
        created = _goal_utc(self.created_at)
        expires = _goal_utc(self.expires_at)
        if expires <= created:
            raise ValueError("goal expires_at must be after created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "success_conditions", conditions)
        steps = (self.reason,) if self.steps == () else _goal_strict_strings(self.steps, "steps")
        if not 1 <= len(steps) <= 3:
            raise ValueError("goal requires one to three short-intention steps")
        object.__setattr__(self, "steps", steps)
        if self.suspend_reason is not None:
            object.__setattr__(
                self, "suspend_reason", _goal_required(self.suspend_reason, "suspend_reason"),
            )
        if self.parent_thread_id is not None:
            object.__setattr__(
                self,
                "parent_thread_id",
                _goal_required(self.parent_thread_id, "parent_thread_id"),
            )
        object.__setattr__(self, "metadata", _goal_freeze_mapping(self.metadata))

    def with_status(
        self, status: GoalStatus, *, suspend_reason: str | None = None,
    ) -> "Goal":
        return replace(self, status=status, suspend_reason=suspend_reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.goal_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "priority": self.priority,
            "reason": self.reason,
            "source": self.source.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "success_conditions": list(self.success_conditions),
            "steps": list(self.steps),
            "suspend_reason": self.suspend_reason,
            "parent_thread_id": self.parent_thread_id,
            "metadata": _goal_thaw(self.metadata),
        }


@dataclass(frozen=True)
class ShortIntention:
    intention_id: str
    goal_id: str
    status: ShortIntentionStatus
    step_index: int
    step_count: int
    step: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    reason_code: str
    applied_outcome_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("intention_id", "goal_id", "step", "reason_code"):
            object.__setattr__(self, name, _goal_required(getattr(self, name), name))
        if not isinstance(self.status, ShortIntentionStatus):
            raise ValueError("intention status must be ShortIntentionStatus")
        for name in ("step_index", "step_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if not 1 <= self.step_count <= 3:
            raise ValueError("step_count must be between one and three")
        if not 0 <= self.step_index < self.step_count:
            raise ValueError("step_index is outside the intention step range")
        created = _goal_utc(self.created_at)
        updated = _goal_utc(self.updated_at)
        expires = _goal_utc(self.expires_at)
        if updated < created:
            raise ValueError("updated_at cannot precede created_at")
        if expires <= created:
            raise ValueError("expires_at must be after created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "expires_at", expires)
        outcomes = _goal_strict_strings(self.applied_outcome_ids, "applied_outcome_ids")
        if len(outcomes) > self.step_count or len(set(outcomes)) != len(outcomes):
            raise ValueError("applied_outcome_ids must be unique and bounded by step_count")
        object.__setattr__(self, "applied_outcome_ids", outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.intention_id,
            "goal_id": self.goal_id,
            "status": self.status.value,
            "step_index": self.step_index,
            "step_count": self.step_count,
            "step": self.step,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "reason_code": self.reason_code,
            "applied_outcome_ids": list(self.applied_outcome_ids),
        }


@dataclass(frozen=True)
class GoalSnapshot:
    active: Goal | None = None
    candidates: tuple[Goal, ...] = ()
    suspended: tuple[Goal, ...] = ()
    recent_terminal: tuple[Goal, ...] = ()
    current_intention: ShortIntention | None = None
    intentions: tuple[ShortIntention, ...] = ()
    recent_intentions: tuple[ShortIntention, ...] = ()

    def __post_init__(self) -> None:
        for name in ("candidates", "suspended", "recent_terminal"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not all(isinstance(item, Goal) for item in value):
                raise ValueError(f"{name} must be a tuple of Goal")
        if self.active is not None and not isinstance(self.active, Goal):
            raise ValueError("active must be Goal")
        if self.current_intention is not None and not isinstance(
            self.current_intention, ShortIntention,
        ):
            raise ValueError("current_intention must be ShortIntention")
        if not isinstance(self.recent_intentions, tuple) or not all(
            isinstance(item, ShortIntention) for item in self.recent_intentions
        ):
            raise ValueError("recent_intentions must be a tuple of ShortIntention")
        if not isinstance(self.intentions, tuple) or not all(
            isinstance(item, ShortIntention) for item in self.intentions
        ):
            raise ValueError("intentions must be a tuple of ShortIntention")
        if len({item.goal_id for item in self.intentions}) != len(self.intentions):
            raise ValueError("intentions must contain at most one entry per goal")
        if self.active is None and self.current_intention is not None:
            raise ValueError("current_intention requires an active goal")
        if self.active is not None and self.current_intention is not None:
            if self.current_intention.goal_id != self.active.goal_id:
                raise ValueError("current_intention must belong to the active goal")
            if self.current_intention.status is not ShortIntentionStatus.ACTIVE:
                raise ValueError("current_intention must be active")

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active.to_dict() if self.active else None,
            "candidates": [goal.to_dict() for goal in self.candidates],
            "suspended": [goal.to_dict() for goal in self.suspended],
            "recent_terminal": [goal.to_dict() for goal in self.recent_terminal],
            "current_intention": (
                self.current_intention.to_dict() if self.current_intention else None
            ),
            "intentions": [item.to_dict() for item in self.intentions],
            "recent_intentions": [item.to_dict() for item in self.recent_intentions],
        }


class GoalProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: GoalKind
    reason: str
    success_condition: str
    source_event_id: str
    parent_thread_id: str | None = None

    @field_validator("reason", "success_condition", "source_event_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        compact = " ".join(value.split())
        if not compact:
            raise ValueError("must not be blank")
        return compact


class ThreadExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: ThreadOperation
    kind: ThreadKind
    topic: str
    summary: str
    source_event_id: str
    evidence_excerpt: str
    target_thread_id: str | None = None
    reason: str | None = None

    @field_validator("topic", "summary", "source_event_id", "evidence_excerpt")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        compact = " ".join(value.split())
        if not compact:
            raise ValueError("must not be blank")
        return compact


class BehaviorKind(str, Enum):
    CURIOUS = "curious"
    DEFLECT = "deflect"
    TEASE = "tease"
    ACKNOWLEDGE = "acknowledge"
    REPAIR = "repair"
    INVITE = "invite"
    TRANSITION = "transition"


@dataclass(frozen=True)
class BehaviorDecision:
    kind: BehaviorKind
    directive: str
    reason: str
    applicable: bool = True


def _state_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _state_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _state_freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_state_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_state_freeze(item) for item in value), key=repr))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _state_freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _state_freeze(item) for key, item in value.items()})


def _state_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _state_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_state_thaw(item) for item in value]
    return value


def _goal_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("goal timestamps must be timezone-aware datetime values")
    return value.astimezone(timezone.utc)


def _goal_required(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _goal_strict_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    return tuple(_goal_required(item, field_name) for item in value)


def _goal_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _goal_freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_goal_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("goal metadata numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("goal metadata contains an unsupported value")


def _goal_freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("goal metadata must be a mapping")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        clean = _goal_required(key, "metadata key")
        if clean in frozen:
            raise ValueError("goal metadata keys must be unique")
        frozen[clean] = _goal_freeze(item)
    return MappingProxyType(frozen)


def _goal_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _goal_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_goal_thaw(item) for item in value]
    return value


def _continuity_required(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be canonical non-empty text")
    return value


def _continuity_optional(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _continuity_required(value, field_name)


def _continuity_strings(
    value: Any, field_name: str, *, allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    result = tuple(_continuity_required(item, field_name) for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{field_name} must not be empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must contain unique values")
    return result


def _continuity_utc(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("continuity timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "AgentEventKind", "AgentEventSource", "AgentStateSnapshot", "AuthoritativeStateService",
    "AuthoritativeStateSnapshot", "BehaviorDecision",
    "BehaviorKind", "ContinuityCommitDisposition", "ContinuityCommitReceipt",
    "ContinuityStateService", "ConversationMove", "DeliveredTurnRecord",
    "EventProvenance", "GroundedEvent",
    "Goal", "GoalKind", "GoalProposal", "GoalSnapshot", "GoalSource", "GoalStatus",
    "OpenThread", "SessionRecap", "SessionRecapItem", "ShortIntention",
    "ShortIntentionStatus", "StreamPhase", "ThreadContribution", "ThreadEvidence",
    "ThreadExtraction", "ThreadKind", "ThreadOperation", "ThreadSignal", "ThreadSpeaker",
    "ThreadStatus", "TopicMatch", "TopicState",
]
