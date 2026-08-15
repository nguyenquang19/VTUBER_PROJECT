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
    from services.agent.types import (
        ConversationMove, OpenThread, ThreadEvidence, ThreadKind, ThreadSpeaker,
        ThreadStatus, TopicMatch,
    )
    from services.agent.types import SessionRecap
    from services.agent.thread_extraction import ThreadExtraction
    from services.agent.behavior_library import BehaviorDecision


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
    def fail(self, goal_id: str, *, reason: str) -> bool:
        """Record a deterministic failed action outcome without replanning."""

    @abstractmethod
    def snapshot(self) -> "GoalSnapshot":
        """Return an immutable, pruned goal snapshot."""

    @abstractmethod
    def reconcile_threads(self, open_thread_ids: set[str] | tuple[str, ...]) -> int:
        """Cancel thread-bound goals whose parent thread is no longer open."""

    @abstractmethod
    def focus_delivered_thread(
        self,
        parent_thread_id: str | None,
        *,
        source_event_ids: set[str] | tuple[str, ...] = (),
        reason: str = "targeted_chat_delivered",
    ) -> int:
        """Focus one delivered chat parent and cancel stale soft continuations."""

    @abstractmethod
    def clear_continue_threads(self, *, reason: str) -> int:
        """Cancel pending soft continuations after a delivered public boundary."""

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
        speaker: "ThreadSpeaker | None" = None,
        status: "ThreadStatus | None" = None,
        move: "ConversationMove | None" = None,
        is_open_question: bool = False,
    ) -> "OpenThread | None":
        """Create one grounded thread, or reject invalid/duplicate evidence."""

    @abstractmethod
    def update(
        self, thread_id: str, *, summary: str, evidence: "ThreadEvidence",
        speaker: "ThreadSpeaker | None" = None, status: "ThreadStatus | None" = None,
        move: "ConversationMove | None" = None, is_open_question: bool = False,
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

    @abstractmethod
    def set_status(self, thread_id: str, status: "ThreadStatus") -> bool:
        """Move one live thread between active, waiting, and parked states."""


class TopicMatcherService(Service):
    @abstractmethod
    def match(
        self, text: str, open_threads: tuple["OpenThread", ...],
    ) -> "TopicMatch | None":
        """Return the strongest related thread, or None below the grounded threshold."""


class ConversationMovePlannerService(Service):
    @abstractmethod
    def choose(self, thread: "OpenThread") -> "ConversationMove":
        """Choose the next bounded public conversation move for one thread."""


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
    def render(
        self, state: "AgentStateSnapshot", query: str = "", viewer_id: str | None = None,
    ) -> str:
        """Render bounded grounded continuity context for one LLM turn."""

class ContextSelectorService(Service):
    @abstractmethod
    async def select(
        self, state: "AgentStateSnapshot", query: str = "", viewer_id: str | None = None,
    ) -> str:
        """Select bounded, grounded context for one LLM turn without side effects."""


class ConversationRepairService(Service):
    @abstractmethod
    def decide(self, state: "AgentStateSnapshot", query: str) -> object | None:
        """Return a deterministic repair decision when evidence is unsafe to assert."""


class BehaviorLibraryService(Service):
    @abstractmethod
    def select(
        self,
        action: str,
        mood: object,
        tone_flags: set[str] | tuple[str, ...] = (),
        *,
        proactive_source: str | None = None,
        repair_kind: str | None = None,
    ) -> "BehaviorDecision":
        """Choose one applicable guarded behavior for a grounded host action."""

    @abstractmethod
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable behavior directives for future turns."""
