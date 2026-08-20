"""Immutable goal and short-intention value objects."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping


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
        object.__setattr__(self, "goal_id", _required(self.goal_id, "goal_id"))
        if not isinstance(self.kind, GoalKind):
            raise ValueError("goal kind must be GoalKind")
        if not isinstance(self.status, GoalStatus):
            raise ValueError("goal status must be GoalStatus")
        if not isinstance(self.source, GoalSource):
            raise ValueError("goal source must be GoalSource")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("goal priority must be an integer")
        object.__setattr__(self, "reason", _required(self.reason, "goal reason"))
        conditions = _strict_strings(self.success_conditions, "success_conditions")
        if not conditions:
            raise ValueError("goal needs at least one success condition")
        created = _utc(self.created_at)
        expires = _utc(self.expires_at)
        if expires <= created:
            raise ValueError("goal expires_at must be after created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "success_conditions", conditions)
        if self.steps == ():
            steps = (self.reason,)
        else:
            steps = _strict_strings(self.steps, "steps")
        if not 1 <= len(steps) <= 3:
            raise ValueError("goal requires one to three short-intention steps")
        object.__setattr__(self, "steps", steps)
        if self.suspend_reason is not None:
            object.__setattr__(
                self, "suspend_reason", _required(self.suspend_reason, "suspend_reason"),
            )
        if self.parent_thread_id is not None:
            object.__setattr__(
                self, "parent_thread_id", _required(self.parent_thread_id, "parent_thread_id"),
            )
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

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
            "metadata": _thaw(self.metadata),
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
            object.__setattr__(self, name, _required(getattr(self, name), name))
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
        created = _utc(self.created_at)
        updated = _utc(self.updated_at)
        expires = _utc(self.expires_at)
        if updated < created:
            raise ValueError("updated_at cannot precede created_at")
        if expires <= created:
            raise ValueError("expires_at must be after created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "expires_at", expires)
        outcomes = _strict_strings(self.applied_outcome_ids, "applied_outcome_ids")
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


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("goal timestamps must be timezone-aware datetime values")
    return value.astimezone(timezone.utc)


def _required(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _strict_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    return tuple(_required(item, field_name) for item in value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("goal metadata numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("goal metadata contains an unsupported value")


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("goal metadata must be a mapping")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        clean = _required(key, "metadata key")
        if clean in frozen:
            raise ValueError("goal metadata keys must be unique")
        frozen[clean] = _freeze(item)
    return MappingProxyType(frozen)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
