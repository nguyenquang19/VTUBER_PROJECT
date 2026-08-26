"""S5 compatibility re-exports for canonical local execution."""
from services.execution.local import (
    ActionAdapterConfig,
    AvatarGestureAuthority,
    AvatarGestureExecutor,
    AvatarGestureVerifier,
    LocalActionAdapterBoundary,
    SpeechDeliveryAuthority,
    SpeechDeliveryExecutor,
    SpeechDeliveryVerifier,
)

__all__ = [
    "ActionAdapterConfig", "AvatarGestureAuthority", "AvatarGestureExecutor",
    "AvatarGestureVerifier", "LocalActionAdapterBoundary", "SpeechDeliveryAuthority",
    "SpeechDeliveryExecutor", "SpeechDeliveryVerifier",
]
