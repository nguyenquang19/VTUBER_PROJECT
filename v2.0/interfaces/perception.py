"""Canonical boundaries for Phase 10 perception adapters."""
from __future__ import annotations

from abc import abstractmethod

from interfaces.base import Service
from interfaces.compatibility import PerceptionEvent


class PerceptionIngressService(Service):
    """Accept observations without making decisions or causing side effects."""

    @abstractmethod
    def submit(self, event: PerceptionEvent) -> bool:
        """Admit one typed event; only this boundary may project it to World."""

    @abstractmethod
    def recent_events(self) -> tuple[PerceptionEvent, ...]:
        """Return the bounded, read-only canonical observation history."""

    @abstractmethod
    def set_enabled(self, enabled: bool) -> None:
        """Toggle collection while preserving all decision-path isolation."""


class PerceptionAdapterService(Service):
    """Lifecycle contract for a source adapter that only submits to ingress."""

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Return whether the adapter may accept or poll new observations."""

    @abstractmethod
    async def set_enabled(self, enabled: bool) -> None:
        """Toggle the adapter and clear source-local observation caches when disabled."""
