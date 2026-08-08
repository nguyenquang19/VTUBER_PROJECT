"""Immutable relationship value objects (M7)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


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


@dataclass(frozen=True)
class RelationshipSnapshot:
    profiles: tuple[ViewerProfile, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"profiles": [item.to_dict() for item in self.profiles]}

