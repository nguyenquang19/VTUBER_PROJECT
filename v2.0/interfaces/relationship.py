"""Interfaces for privacy-safe viewer relationships and grounded narrative (M7)."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone
from enum import Enum
import math
from typing import Any

from interfaces.base import Service


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ViewerProfile:
    viewer_id: str
    interaction_count: int
    first_seen: datetime
    last_seen: datetime
    expires_at: datetime
    confirmed_preferences: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    tone: str | None = None

    def __post_init__(self) -> None:
        if not self.viewer_id.startswith("v_"):
            raise ValueError("viewer profile requires a pseudonymous id")
        if self.interaction_count < 1:
            raise ValueError("interaction_count must be positive")
        object.__setattr__(self, "first_seen", _utc(self.first_seen))
        object.__setattr__(self, "last_seen", _utc(self.last_seen))
        object.__setattr__(self, "expires_at", _utc(self.expires_at))
        object.__setattr__(self, "confirmed_preferences", tuple(self.confirmed_preferences))
        object.__setattr__(self, "boundaries", tuple(self.boundaries))

    def to_dict(self) -> dict[str, Any]:
        return {
            "viewer_id": self.viewer_id,
            "interaction_count": self.interaction_count,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "confirmed_preferences": list(self.confirmed_preferences),
            "boundaries": list(self.boundaries),
            "tone": self.tone,
        }


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class NarrativeStatus(str, Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"


class RelationshipHintKind(str, Enum):
    TONE = "tone"
    FACT = "fact"
    CALLBACK = "callback"


@dataclass(frozen=True)
class RelationshipContextHint:
    """Pseudonymous latent instruction; never contains stored relationship wording."""

    hint_id: str
    viewer_ref: str
    kind: RelationshipHintKind
    instruction: str
    evidence_refs: tuple[str, ...]
    observed_at: datetime
    expires_at: datetime
    salience: float

    def __post_init__(self) -> None:
        if not isinstance(self.hint_id, str) or not self.hint_id.strip():
            raise ValueError("relationship hint requires an id")
        if not isinstance(self.viewer_ref, str) or not self.viewer_ref.startswith("v_"):
            raise ValueError("relationship hint requires a pseudonymous viewer")
        if not isinstance(self.kind, RelationshipHintKind):
            raise ValueError("relationship hint kind is invalid")
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("relationship hint requires an instruction")
        refs = tuple(self.evidence_refs)
        if not refs or any(not isinstance(item, str) or not item.strip() for item in refs):
            raise ValueError("relationship hint requires evidence")
        if len(refs) != len(set(refs)):
            raise ValueError("relationship hint evidence must be unique")
        object.__setattr__(self, "evidence_refs", refs)
        observed = _utc(self.observed_at)
        expires = _utc(self.expires_at)
        if expires <= observed:
            raise ValueError("relationship hint expiry must follow observation")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        if isinstance(self.salience, bool) or not isinstance(self.salience, (int, float)):
            raise ValueError("relationship hint salience must be numeric")
        salience = float(self.salience)
        if not math.isfinite(salience) or not 0.0 <= salience <= 1.0:
            raise ValueError("relationship hint salience must be bounded")
        object.__setattr__(self, "salience", salience)


@dataclass(frozen=True)
class RunningGag:
    gag_id: str
    viewer_id: str
    summary: str
    event_refs: tuple[str, ...]
    status: ReviewStatus
    positive_count: int
    created_at: datetime
    last_referenced_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not self.viewer_id.startswith("v_") or not self.summary.strip()
            or not self.event_refs or self.positive_count < 1
        ):
            raise ValueError("running gag requires viewer, summary, and positive evidence")
        object.__setattr__(self, "event_refs", tuple(self.event_refs))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        if self.last_referenced_at is not None:
            object.__setattr__(self, "last_referenced_at", _utc(self.last_referenced_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "gag_id": self.gag_id,
            "viewer_id": self.viewer_id,
            "summary": self.summary,
            "event_refs": list(self.event_refs),
            "status": self.status.value,
            "positive_count": self.positive_count,
            "created_at": self.created_at.isoformat(),
            "last_referenced_at": (
                self.last_referenced_at.isoformat() if self.last_referenced_at else None
            ),
        }


@dataclass(frozen=True)
class RelationshipNote:
    note_id: str
    viewer_id: str
    summary: str
    evidence_refs: tuple[str, ...]
    status: ReviewStatus
    source: str
    created_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.viewer_id.startswith("v_") or not self.summary.strip() or not self.evidence_refs:
            raise ValueError("relationship note requires pseudonym, summary, and evidence")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "expires_at", _utc(self.expires_at))
        if self.reviewed_at is not None:
            object.__setattr__(self, "reviewed_at", _utc(self.reviewed_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "viewer_id": self.viewer_id,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "status": self.status.value,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
        }


@dataclass(frozen=True)
class NarrativeItem:
    narrative_id: str
    summary: str
    event_refs: tuple[str, ...]
    status: NarrativeStatus
    created_at: datetime
    expires_at: datetime
    viewer_id: str | None = None

    def __post_init__(self) -> None:
        if not self.summary.strip() or not self.event_refs:
            raise ValueError("narrative item requires summary and evidence")
        if self.viewer_id is not None and not self.viewer_id.startswith("v_"):
            raise ValueError("narrative viewer must be pseudonymous")
        object.__setattr__(self, "event_refs", tuple(self.event_refs))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "expires_at", _utc(self.expires_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "narrative_id": self.narrative_id,
            "viewer_id": self.viewer_id,
            "summary": self.summary,
            "event_refs": list(self.event_refs),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class RelationshipSnapshot:
    profiles: tuple[ViewerProfile, ...] = field(default_factory=tuple)
    notes: tuple[RelationshipNote, ...] = field(default_factory=tuple)
    narratives: tuple[NarrativeItem, ...] = field(default_factory=tuple)
    running_gags: tuple[RunningGag, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles": [item.to_dict() for item in self.profiles],
            "notes": [item.to_dict() for item in self.notes],
            "narratives": [item.to_dict() for item in self.narratives],
            "running_gags": [item.to_dict() for item in self.running_gags],
        }


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
    def context_hints(
        self, *, raw_viewer_id: str | None = None, event_ref: str | None = None,
    ) -> tuple[RelationshipContextHint, ...]:
        """Return bounded latent hints from either a public raw ID or grounded event lineage."""

    @property
    @abstractmethod
    def context_enabled(self) -> bool:
        """Whether A4 latent relationship projection is active."""

    @abstractmethod
    def set_context_enabled(self, enabled: bool) -> None:
        """Toggle A4 projection; disabled preserves the bounded M7 renderer."""

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
