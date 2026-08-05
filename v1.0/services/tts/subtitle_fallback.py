"""SubtitleFallbackService — Level 2 của TTS chain (ARCHITECTURE 8.7.3, 4.C).

TTS primary (VieNeu-TTS) fail/timeout → rơi xuống đây. KHÔNG phát audio, chỉ đẩy text
ra như "subtitle event" để overlay hiển thị. Trả 1 AudioChunk final (empty audio)
để pipeline không kẹt.

Text đẩy qua callback `on_subtitle` (event bus/dashboard sub sau). Không log persona
nội dung ở INFO — coi như payload người dùng.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Callable

from interfaces.base import HealthStatus
from interfaces.tts import AudioChunk, TTSRequest, TTSService
from orchestrator.logger import get_logger

SubtitleSink = Callable[[str, str], None]  # (request_id, text)


class SubtitleFallbackService(TTSService):
    service_id = "tts_subtitle"

    def __init__(
        self,
        on_subtitle: SubtitleSink | None = None,
        event_bus: Any = None,
    ) -> None:
        self._on_subtitle = on_subtitle
        self._event_bus = event_bus
        self._log = get_logger("tts_subtitle")
        self._subtitles_total = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {"tts_subtitle_total": self._subtitles_total}

    async def cancel(self, request_id: str) -> None:
        return None  # subtitle chỉ đẩy 1 event, không có gì để cancel

    async def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[AudioChunk]:
        self._subtitles_total += 1
        self._log.info("tts_subtitle_fallback", request_id=request.request_id, chars=len(request.text))
        if self._on_subtitle is not None:
            try:
                self._on_subtitle(request.request_id, request.text)
            except Exception as e:  # subtitle sink lỗi không được giết pipeline
                self._log.warning("subtitle_sink_failed", error=str(e))
        if self._event_bus is not None:
            try:
                self._event_bus.publish(
                    "tts_subtitle", {"request_id": request.request_id, "text": request.text}
                )
            except Exception:  # pragma: no cover
                pass
        # Chunk final rỗng — pipeline biết đã kết thúc
        yield AudioChunk(
            request_id=request.request_id,
            chunk_index=0,
            audio_bytes=b"",
            is_final=True,
            duration_ms=0,
        )
