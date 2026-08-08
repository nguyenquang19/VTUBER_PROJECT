"""Immutable relationship value objects (M7)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from enum import Enum


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
class RelationshipSnapshot:
    profiles: tuple[ViewerProfile, ...] = field(default_factory=tuple)
    notes: tuple[RelationshipNote, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles": [item.to_dict() for item in self.profiles],
            "notes": [item.to_dict() for item in self.notes],
        }
