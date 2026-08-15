"""Animation and embodiment contracts."""
from __future__ import annotations

from abc import abstractmethod
from enum import Enum

from pydantic import BaseModel, Field

from interfaces.base import Service
from interfaces.tts import AudioChunk


class MoodState(BaseModel):
    """Mood block LLM trả về. Thang 0-10 mỗi chiều."""

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
    command_type: str
    mood: MoodState | None = None
    duration_ms: int = 0
    intensity: float = 0.5
    gesture_id: str | None = None


class EmbodimentLevel(str, Enum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


class AnimationService(Service):
    @abstractmethod
    async def express(self, command: AnimationCommand) -> None:
        """Apply a cosmetic mood/gesture command to the avatar."""

    @abstractmethod
    async def trigger_intentional_gesture(self, gesture_id: str) -> bool:
        """Trigger one allowlisted intentional gesture and return VTS acknowledgement."""

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
    async def finish_intentional(self, action_id: str, succeeded: bool) -> None:
        """Release a HIGH gesture lease and record its authoritative outcome."""

    @abstractmethod
    def snapshot(self) -> dict[str, object]:
        """Return bounded operator-safe policy state and records."""