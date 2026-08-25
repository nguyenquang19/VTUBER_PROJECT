"""Compatibility re-exports for canonical relationship contracts; remove in S8."""
from interfaces.relationship import (
    NarrativeItem,
    NarrativeStatus,
    RelationshipNote,
    RelationshipSnapshot,
    ReviewStatus,
    RunningGag,
    ViewerProfile,
)

__all__ = [
    "NarrativeItem", "NarrativeStatus", "RelationshipNote", "RelationshipSnapshot",
    "ReviewStatus", "RunningGag", "ViewerProfile",
]
