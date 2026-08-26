"""Canonical state ownership and compatibility exports."""

from services.state.authoritative import AuthoritativeStateConfig, AuthoritativeStateReducer
from services.state.continuity import ContinuityCommitter, ContinuityConfig

__all__ = [
    "AuthoritativeStateConfig", "AuthoritativeStateReducer",
    "ContinuityCommitter", "ContinuityConfig",
]
