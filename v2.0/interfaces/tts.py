"""TTS interface (ARCHITECTURE 7.6).

Implementation production là `VieNeuTtsService`; local baseline TTFA sau voice cache khoảng 308 ms.
Fallback là subtitle overlay, không phải TTS engine thứ 2 (spec 8.7.3).
"""
from __future__ import annotations

from abc import abstractmethod
from enum import Enum
from typing import AsyncIterator

from pydantic import BaseModel, ConfigDict, Field

from interfaces.base import Service


class TTSRequest(BaseModel):
    request_id: str
    text: str
    voice_id: str = "mai_default"
    emotion: str | None = None
    intensity: float = 0.5
    speed: float = 1.0


class AudioChunk(BaseModel):
    request_id: str
    chunk_index: int
    audio_bytes: bytes
    is_final: bool
    duration_ms: int


class TTSDeliveryMode(str, Enum):
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    MIXED = "mixed"
    NONE = "none"
    CANCELLED = "cancelled"


class TTSDeliveryResult(BaseModel):
    """Delivery-boundary result used before committing a Director action."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    delivered: bool = False
    mode: TTSDeliveryMode = TTSDeliveryMode.NONE
    sentences_total: int = Field(0, ge=0)
    sentences_delivered: int = Field(0, ge=0)
    audio_sentences: int = Field(0, ge=0)
    subtitle_sentences: int = Field(0, ge=0)
    failed_sentences: int = Field(0, ge=0)
    cancelled: bool = False


class TTSService(Service):
    @abstractmethod
    def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[AudioChunk]:
        """Sinh audio theo chunk, in-order, không overlap giữa request."""

    @abstractmethod
    async def cancel(self, request_id: str) -> None:
        """Huỷ synthesize đang chạy (dùng khi interrupt)."""
