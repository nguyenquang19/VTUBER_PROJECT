"""Interfaces for privacy-safe viewer relationships and grounded narrative (M7)."""
from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING

from interfaces.base import Service

if TYPE_CHECKING:
    from services.relationship.types import NarrativeItem, RelationshipNote, RelationshipSnapshot, RunningGag, ViewerProfile


class RelationshipService(Service):
    """Own pseudonymous viewer profiles and evidence-backed social context."""

    @abstractmethod
    def observe_interaction(
        self, *, raw_viewer_id: str | None, event_id: str, occurred_at: datetime,
        emotion_category: str | None = None,
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

    @abstractmethod
    def update_profile(
        self, viewer_id: str, *, preferences: list[str], boundaries: list[str],
        tone: str | None, evidence_refs: list[str], reason: str,
    ) -> "ViewerProfile | None":
        """Apply operator-confirmed attributes backed by grounded event references."""

    @abstractmethod
    def create_note(
        self, viewer_id: str, *, summary: str, evidence_refs: list[str], reason: str,
    ) -> "RelationshipNote | None":
        """Create a pending evidence-backed note for operator review."""

    @abstractmethod
    def review_note(self, note_id: str, *, approve: bool, reason: str) -> bool:
        """Approve or reject one note with an audit record."""

    @abstractmethod
    def delete_note(self, note_id: str, *, reason: str) -> bool:
        """Delete one note with an audit record."""

    @abstractmethod
    def create_narrative(
        self, *, summary: str, event_refs: list[str], reason: str,
        viewer_id: str | None = None,
    ) -> "NarrativeItem | None":
        """Create an active narrative only from existing grounded events."""

    @abstractmethod
    def resolve_narrative(self, narrative_id: str, *, reason: str) -> bool:
        """Resolve one narrative item with operator audit."""

    @abstractmethod
    def render_context(self, raw_viewer_id: str | None = None) -> str:
        """Render bounded approved relationship/narrative prompt context."""

    @abstractmethod
    def create_running_gag(
        self, viewer_id: str, *, summary: str, event_refs: list[str], reason: str,
    ) -> "RunningGag | None":
        """Create a pending gag only after enough grounded positive interactions."""

    @abstractmethod
    def review_running_gag(self, gag_id: str, *, approve: bool, reason: str) -> bool:
        """Approve or reject one running gag with operator audit."""

    @abstractmethod
    async def export_viewer(self, viewer_id: str) -> dict:
        """Export sanitized relationship and memory records for one pseudonym."""

    @abstractmethod
    async def delete_viewer(self, viewer_id: str, *, reason: str) -> dict | None:
        """Delete every viewer-scoped relationship and memory record."""
