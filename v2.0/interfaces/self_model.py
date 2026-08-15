"""Read-only Self Model projection contract (Phase 3)."""
from __future__ import annotations

from abc import abstractmethod

from interfaces.base import Service
from interfaces.compatibility import SelfSnapshot


class SelfModelService(Service):
    """Expose an immutable projection without owning domain state."""

    @abstractmethod
    def snapshot(self) -> SelfSnapshot:
        """Project current public source state into an immutable snapshot."""
