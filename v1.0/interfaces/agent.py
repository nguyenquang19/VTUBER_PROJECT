"""Interfaces for grounded agent working state (Master Plan M1)."""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from interfaces.base import Service

if TYPE_CHECKING:
    from services.agent.types import AgentStateSnapshot, GroundedEvent
    from services.agent.goal_types import Goal, GoalSnapshot
    from services.agent.goal_proposal import GoalProposal
    from services.agent.types import OpenThread, ThreadEvidence, ThreadKind
    from services.agent.types import SessionRecap
    from services.agent.thread_extraction import ThreadExtraction


class EventLedgerService(Service):
    @abstractmethod
    def append(self, event: "GroundedEvent") -> bool:
        """Append one grounded event. Return False when rejected or duplicated."""

    @abstractmethod
    def recent(
        self, limit: int | None = None, *, now: datetime | None = None,
    ) -> tuple["GroundedEvent", ...]:
        """Return a bounded immutable view ordered from oldest to newest."""


class AgentStateService(Service):
    @abstractmethod
    def record(self, event: "GroundedEvent") -> bool:
        """Record an accepted grounded event and reduce working state."""

    @abstractmethod
    def snapshot(self) -> "AgentStateSnapshot":
        """Return an immutable state snapshot detached from mutable internals."""

    @abstractmethod
    def set_active_goal_ref(self, goal_id: str | None) -> None:
        """Update only the reference owned by GoalManager."""

    @abstractmethod
    def add_event_listener(
        self, listener: Callable[["GroundedEvent", "AgentStateSnapshot"], None],
    ) -> None:
        """Observe accepted events after reduction; listener failures are isolated."""


class GoalManagerService(Service):
    @abstractmethod
    def submit(self, goal: "Goal") -> bool:
        """Validate and submit a grounded goal candidate."""

    @abstractmethod
    def complete(self, goal_id: str, *, reason: str = "success") -> bool:
        """Complete one goal and activate the next eligible goal."""

    @abstractmethod
    def cancel(self, goal_id: str, *, reason: str) -> bool:
        """Cancel one goal and activate the next eligible goal."""

    @abstractmethod
    def snapshot(self) -> "GoalSnapshot":
        """Return an immutable, pruned goal snapshot."""

    @abstractmethod
    def pin_operator(
        self, *, reason: str, success_condition: str, parent_thread_id: str | None = None,
    ) -> "Goal | None":
        """Create the only goal kind that dashboard is allowed to originate."""

    @abstractmethod
    def operator_complete(self, goal_id: str, *, reason: str) -> bool:
        """Complete any goal as an audited operator override."""

    @abstractmethod
    def operator_cancel(self, goal_id: str, *, reason: str) -> bool:
        """Cancel any goal as an audited operator override."""

    @abstractmethod
    def handle_event(self, event: "GroundedEvent", state: "AgentStateSnapshot") -> None:
        """Apply grounded event completion/refresh rules and candidate policy."""

    @abstractmethod
    def accept_proposal(
        self, proposal: "GoalProposal", state: "AgentStateSnapshot",
    ) -> bool:
        """Validate LLM proposal evidence before submitting it as a candidate."""


class GoalProposalService(Service):
    @abstractmethod
    async def propose(self, state: "AgentStateSnapshot") -> "GoalProposal | None":
        """Return one strict-schema proposal without activating it."""

    @abstractmethod
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable future proposal calls."""


class OpenThreadManagerService(Service):
    @abstractmethod
    def create(
        self,
        *,
        kind: "ThreadKind",
        topic: str,
        summary: str,
        evidence: "ThreadEvidence",
        thread_id: str | None = None,
    ) -> "OpenThread | None":
        """Create one grounded thread, or reject invalid/duplicate evidence."""

    @abstractmethod
    def update(
        self, thread_id: str, *, summary: str, evidence: "ThreadEvidence",
    ) -> bool:
        """Update a live thread with new grounded evidence."""

    @abstractmethod
    def resolve(self, thread_id: str, *, reason: str) -> bool:
        """Resolve and remove one open thread."""

    @abstractmethod
    def expire(self) -> int:
        """Expire stale threads using the injected clock."""

    @abstractmethod
    def snapshot(self) -> tuple["OpenThread", ...]:
        """Return an immutable bounded view of open threads."""


class ThreadExtractionService(Service):
    @abstractmethod
    async def propose(
        self, event: "GroundedEvent", state: "AgentStateSnapshot",
    ) -> "ThreadExtraction | None":
        """Optionally extract one post-hoc thread operation from grounded evidence."""

    @abstractmethod
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable future extraction calls."""


class SessionRecapService(Service):
    @abstractmethod
    def handle_event(self, event: "GroundedEvent") -> None:
        """Update the bounded recap from one accepted grounded event."""

    @abstractmethod
    def snapshot(self) -> "SessionRecap":
        """Return an immutable recap containing no full transcript."""


class ConversationContextService(Service):
    @abstractmethod
    def render(self, state: "AgentStateSnapshot", query: str = "") -> str:
        """Render bounded grounded continuity context for one LLM turn."""
