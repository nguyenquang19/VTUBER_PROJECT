"""Canonical ingress contract for Phase 10 perception adapters."""
from __future__ import annotations

from abc import abstractmethod
from typing import Any

from interfaces.base import Service
from interfaces.compatibility import PerceptionEvent
from interfaces.input import InputEvent


class PerceptionIngressService(Service):
    """Accept observations without making decisions or causing side effects."""

    @abstractmethod
    def observe_input(self, event: InputEvent) -> PerceptionEvent | None:
        """Map one compatibility input into the canonical perception boundary."""

    @abstractmethod
    def observe_grounded(self, event: Any, state: Any = None) -> bool:
        """Accept only a structured grounded observation for World shadow reduction."""

    @abstractmethod
    def recent_events(self) -> tuple[PerceptionEvent, ...]:
        """Return the bounded, read-only canonical observation history."""

    @abstractmethod
    def set_enabled(self, enabled: bool) -> None:
        """Toggle collection while preserving all decision-path isolation."""
