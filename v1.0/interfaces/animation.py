"""Animation interface (ARCHITECTURE 7.7).

5 mood khớp persona (vui / buồn / bực / bồn chồn / ngượng).
Implementation VTube Studio ở Phase 6.
"""
from __future__ import annotations

from abc import abstractmethod

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
        """Mood mạnh nhất. Tie → theo thứ tự khai báo. Tất cả 0 → 'neutral'."""
        scores = self.model_dump()
        best = max(scores, key=lambda k: scores[k])
        return best if scores[best] > 0 else "neutral"


class AnimationCommand(BaseModel):
    command_type: str  # "express" | "gesture" | "idle"
    mood: MoodState | None = None
    duration_ms: int = 0
    intensity: float = 0.5


class AnimationService(Service):
    @abstractmethod
    async def express(self, command: AnimationCommand) -> None:
        """Áp expression/gesture lên model."""

    @abstractmethod
    async def sync_with_audio(self, audio_chunk: AudioChunk) -> None:
        """Sync animation (lip-sync/nhấn) với audio đang phát."""
