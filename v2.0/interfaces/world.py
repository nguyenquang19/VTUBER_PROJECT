"""Public World Model shadow service contract (Phase 2)."""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from interfaces.base import Service
from interfaces.compatibility import PerceptionEvent, StateValue, WorldSnapshot


class WorldModelService(Service):
    """Bounded read-only world-state reducer; it must not drive decisions."""

    @abstractmethod
    def apply_event(self, event: PerceptionEvent) -> bool:
        """Validate and reduce one explicit world observation."""

    @abstractmethod
    def snapshot(self) -> WorldSnapshot:
        """Return an immutable snapshot that excludes stale state."""

    @abstractmethod
    def query(self, path: str) -> StateValue | None:
        """Return one fresh state value by allowlisted dotted path."""

    @abstractmethod
    def evict_stale(self, now: datetime) -> int:
        """Remove stale values and return the number evicted."""
