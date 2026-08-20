"""Animation and embodiment contracts."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, overload

from pydantic import BaseModel, ConfigDict, Field, field_validator

from interfaces.base import Service
from interfaces.tts import AudioChunk


class MoodState(BaseModel):
    """Mood block LLM trả về. Thang 0-10 mỗi chiều."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    vui: int = Field(0, ge=0, le=10)
    buon: int = Field(0, ge=0, le=10)
    buc: int = Field(0, ge=0, le=10)
    bon_chon: int = Field(0, ge=0, le=10)
    nguong: int = Field(0, ge=0, le=10)

    def dominant(self) -> str:
        scores = self.model_dump()
        best = max(scores, key=lambda key: scores[key])
        return best if scores[best] > 0 else "neutral"


class AnimationCommand(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    command_type: str
    mood: MoodState | None = None
    duration_ms: int = Field(0, ge=0)
    intensity: float = Field(0.5, ge=0.0, le=1.0)
    gesture_id: str | None = None

    @field_validator("command_type", "gesture_id")
    @classmethod
    def validate_labels(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value != value.strip():
            raise ValueError("animation command labels must be trimmed and non-empty")
        return value


class EmbodimentLevel(str, Enum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


class IntentionalGestureOutcome(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    EXPIRED = "expired"


@overload
def _strict_label(value: str, field_name: str) -> str: ...


@overload
def _strict_label(value: None, field_name: str) -> None: ...


def _strict_label(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a trimmed non-empty string")
    return value


@dataclass(frozen=True)
class EmbodimentRecord:
    sequence: int
    level: EmbodimentLevel
    outcome: str
    delivery_id: str | None = None
    action_id: str | None = None
    gesture_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    verification_source: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence <= 0:
            raise ValueError("sequence must be a positive integer")
        if not isinstance(self.level, EmbodimentLevel):
            raise ValueError("level must be EmbodimentLevel")
        object.__setattr__(self, "outcome", _strict_label(self.outcome, "outcome"))
        for name in ("delivery_id", "action_id", "gesture_id", "verification_source"):
            object.__setattr__(self, name, _strict_label(getattr(self, name), name))
        if not isinstance(self.evidence_refs, tuple):
            raise ValueError("evidence_refs must be a tuple")
        refs = tuple(_strict_label(item, "evidence_ref") for item in self.evidence_refs)
        object.__setattr__(self, "evidence_refs", refs)

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "level": self.level.value,
            "outcome": self.outcome,
            "delivery_id": self.delivery_id,
            "action_id": self.action_id,
            "gesture_id": self.gesture_id,
            "evidence_refs": list(self.evidence_refs),
            "verification_source": self.verification_source,
        }


@dataclass(frozen=True)
class EmbodimentSnapshot:
    running: bool
    enabled: bool
    active_level: EmbodimentLevel | None
    active_action_id: str | None
    active_gesture_id: str | None
    counts: Mapping[str, int]
    recent: tuple[EmbodimentRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.running, bool) or not isinstance(self.enabled, bool):
            raise ValueError("running and enabled must be boolean")
        if self.active_level is not None and not isinstance(self.active_level, EmbodimentLevel):
            raise ValueError("active_level must be EmbodimentLevel or None")
        object.__setattr__(
            self, "active_action_id", _strict_label(self.active_action_id, "active_action_id"),
        )
        object.__setattr__(
            self, "active_gesture_id", _strict_label(self.active_gesture_id, "active_gesture_id"),
        )
        if not isinstance(self.counts, Mapping):
            raise ValueError("counts must be a mapping")
        frozen_counts: dict[str, int] = {}
        for key, value in self.counts.items():
            label = _strict_label(key, "count key")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("count values must be non-negative integers")
            frozen_counts[label] = value
        object.__setattr__(self, "counts", MappingProxyType(frozen_counts))
        if not isinstance(self.recent, tuple) or any(
            not isinstance(item, EmbodimentRecord) for item in self.recent
        ):
            raise ValueError("recent must be a tuple of EmbodimentRecord")

    def to_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "enabled": self.enabled,
            "active_level": self.active_level.value if self.active_level is not None else None,
            "active_action_id": self.active_action_id,
            "active_gesture_id": self.active_gesture_id,
            "counts": dict(self.counts),
            "recent": [item.to_dict() for item in self.recent],
        }


class AnimationService(Service):
    @abstractmethod
    async def express(self, command: AnimationCommand) -> None:
        """Apply a cosmetic mood/gesture command to the avatar."""

    @abstractmethod
    async def trigger_intentional_gesture(self, gesture_id: str) -> bool:
        """Trigger one allowlisted intentional gesture and return VTS acknowledgement."""

    @abstractmethod
    def is_intentional_gesture_allowed(self, gesture_id: str) -> bool:
        """Return whether a strict gesture ID is present in the operator allowlist."""

    @abstractmethod
    async def sync_with_audio(self, audio_chunk: AudioChunk) -> None:
        """Sync automatic lip-sync with audio output."""


class EmbodimentPolicyService(Service):
    """Coordinate cosmetic MID expression and exclusive HIGH action leases."""

    @abstractmethod
    async def apply_mid(self, delivery_id: str, mood: MoodState) -> bool:
        """Apply one post-delivery MID expression without altering business state."""

    @abstractmethod
    async def begin_intentional(
        self, action_id: str, gesture_id: str, evidence_refs: tuple[str, ...],
    ) -> bool:
        """Reserve the policy lease for a grounded HIGH gesture."""

    @abstractmethod
    async def finish_intentional(
        self,
        action_id: str,
        outcome: IntentionalGestureOutcome,
        verification_source: str | None = None,
    ) -> bool:
        """Release a HIGH gesture lease and record its authoritative outcome."""

    @abstractmethod
    def snapshot(self) -> EmbodimentSnapshot:
        """Return bounded operator-safe policy state and records."""
