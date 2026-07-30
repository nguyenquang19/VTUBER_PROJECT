"""TTS interface (ARCHITECTURE 7.6).

Implementation `ViXttsService` ở Phase 4 (chốt Pre-flight Day 2).
Fallback là subtitle overlay, không phải TTS engine thứ 2 (spec 8.7.3).
"""
from __future__ import annotations

from abc import abstractmethod
from typing import AsyncIterator

from pydantic import BaseModel

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


class TTSService(Service):
    @abstractmethod
    def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[AudioChunk]:
        """Sinh audio theo chunk, in-order, không overlap giữa request."""

    @abstractmethod
    async def cancel(self, request_id: str) -> None:
        """Huỷ synthesize đang chạy (dùng khi interrupt)."""
