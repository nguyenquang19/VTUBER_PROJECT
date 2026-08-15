"""Immutable goal value objects for the M2 agenda policy."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
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
        if not self.goal_id.strip():
            raise ValueError("goal_id must not be empty")
        if not self.reason.strip():
            raise ValueError("goal reason must not be empty")
        if not self.success_conditions:
            raise ValueError("goal needs at least one success condition")
        created = _utc(self.created_at)
        expires = _utc(self.expires_at)
        if expires <= created:
            raise ValueError("goal expires_at must be after created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "success_conditions", tuple(self.success_conditions))
        steps = tuple(str(step).strip() for step in self.steps if str(step).strip()) or (self.reason.strip(),)
        if len(steps) > 3:
            raise ValueError("goal supports at most three short-intention steps")
        object.__setattr__(self, "steps", steps)
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
class GoalSnapshot:
    active: Goal | None = None
    candidates: tuple[Goal, ...] = ()
    suspended: tuple[Goal, ...] = ()
    recent_terminal: tuple[Goal, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "suspended", tuple(self.suspended))
        object.__setattr__(self, "recent_terminal", tuple(self.recent_terminal))

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active.to_dict() if self.active else None,
            "candidates": [goal.to_dict() for goal in self.candidates],
            "suspended": [goal.to_dict() for goal in self.suspended],
            "recent_terminal": [goal.to_dict() for goal in self.recent_terminal],
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return tuple(_freeze(item) for item in value)
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
