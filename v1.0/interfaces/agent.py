"""Interfaces for grounded agent working state (Master Plan M1)."""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING

from interfaces.base import Service

if TYPE_CHECKING:
    from services.agent.types import AgentStateSnapshot, GroundedEvent


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
