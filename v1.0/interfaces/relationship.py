"""Interfaces for privacy-safe viewer relationships and grounded narrative (M7)."""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING

from interfaces.base import Service

if TYPE_CHECKING:
    from services.relationship.types import RelationshipSnapshot, ViewerProfile


class RelationshipService(Service):
    """Own pseudonymous viewer profiles and evidence-backed social context."""

    @abstractmethod
    def observe_interaction(
        self, *, raw_viewer_id: str | None, event_id: str, occurred_at: datetime,
    ) -> "ViewerProfile | None":
        """Count one deduplicated grounded interaction without storing the raw ID."""

    @abstractmethod
    def get_profile(self, viewer_id: str) -> "ViewerProfile | None":
        """Return one non-expired profile by pseudonymous ID."""

    @abstractmethod
    def snapshot(self) -> "RelationshipSnapshot":
        """Return a detached, privacy-safe operator snapshot."""

    @abstractmethod
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable future relationship collection/context use."""
