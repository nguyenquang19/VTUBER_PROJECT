"""Compatibility re-exports for canonical goal contracts; remove in S8."""
from interfaces.state import (
    Goal,
    GoalKind,
    GoalSnapshot,
    GoalSource,
    GoalStatus,
    ShortIntention,
    ShortIntentionStatus,
)

__all__ = [
    "Goal", "GoalKind", "GoalSnapshot", "GoalSource", "GoalStatus", "ShortIntention",
    "ShortIntentionStatus",
]
