"""Canonical import path for the deterministic World reducer."""

from services.world.world_model import (
    WorldModelConfig,
    WorldModelShadow,
    perception_event_from_grounded_observation,
)

__all__ = [
    "WorldModelConfig", "WorldModelShadow", "perception_event_from_grounded_observation",
]
