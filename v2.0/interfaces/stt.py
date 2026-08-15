"""STT interface (ARCHITECTURE 7.3).

Voice input bị defer theo scope decision (xem `docs/01_SYSTEM_OVERVIEW.md`).
`NullSTTService` là stub để Phase 0-4 chạy được mà không cần STT thật;
Phase 5 sẽ thêm implementation faster-whisper cạnh nó, không breaking.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, AsyncIterator

from pydantic import BaseModel

from interfaces.base import HealthStatus, Service


class TranscriptChunk(BaseModel):
    chunk_id: str
    text: str
    is_final: bool
    emotion: str | None = None
    emotion_confidence: float | None = None
    audio_start_ms: int
    audio_end_ms: int


class STTService(Service):
    @abstractmethod
    def transcribe_stream(
        self, audio_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptChunk]:
        """Stream audio in, yield transcript chunks out."""


class NullSTTService(STTService):
    """Stub: không transcribe gì. Dùng khi feature `input_voice` off."""

    service_id = "stt_null"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id, note="stub, voice input disabled")

    def get_metrics(self) -> dict[str, Any]:
        return {"stt_chunks_total": 0}

    async def transcribe_stream(
        self, audio_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptChunk]:
        # Không yield gì — caller thấy stream rỗng, tương đương "silence".
        return
        yield  # pragma: no cover  (giữ hàm là async generator)
