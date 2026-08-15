"""SubtitleFallbackService — Level 2 của TTS chain (ARCHITECTURE 8.7.3, 4.C).

TTS primary (VieNeu-TTS) fail/timeout → rơi xuống đây. KHÔNG phát audio, chỉ đẩy text
ra như "subtitle event" để overlay hiển thị. Trả 1 AudioChunk final (empty audio)
để pipeline không kẹt.

Text đẩy qua callback `on_subtitle` (event bus/dashboard sub sau). Không log persona
nội dung ở INFO — coi như payload người dùng.
"""
from __future__ import annotations

import os
from pathlib import Path
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
        output_file: str | Path | None = None,
        require_delivery: bool = False,
    ) -> None:
        self._on_subtitle = on_subtitle
        self._event_bus = event_bus
        self._output_file = Path(output_file) if output_file else None
        self._require_delivery = bool(require_delivery)
        self._log = get_logger("tts_subtitle")
        self._subtitles_total = 0
        self._delivery_failures = 0

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        on_subtitle: SubtitleSink | None = None,
        event_bus: Any = None,
    ) -> "SubtitleFallbackService":
        return cls(
            on_subtitle=on_subtitle,
            event_bus=event_bus,
            output_file=loader.get(
                "models", "tts_fallback.output_file", "logs/live/subtitle.txt",
            ),
            require_delivery=bool(loader.get(
                "models", "tts_fallback.require_delivery", True,
            )),
        )

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        if self._on_subtitle is not None or self._event_bus is not None:
            return HealthStatus.healthy(self.service_id, sink="callback_or_event_bus")
        if self._output_file is not None:
            parent = self._output_file.parent
            existing_parent = parent if parent.exists() else next(
                (candidate for candidate in parent.parents if candidate.exists()), None,
            )
            if (
                existing_parent is not None
                and existing_parent.is_dir()
                and os.access(existing_parent, os.W_OK)
            ):
                return HealthStatus.healthy(
                    self.service_id, sink="file", output_file=str(self._output_file),
                )
        if self._require_delivery:
            return HealthStatus.unhealthy(
                self.service_id, "subtitle fallback has no writable delivery sink",
            )
        return HealthStatus.degraded(
            self.service_id, "subtitle fallback has no verified delivery sink",
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "tts_subtitle_total": self._subtitles_total,
            "tts_subtitle_delivery_failures_total": self._delivery_failures,
        }

    async def cancel(self, request_id: str) -> None:
        return None  # subtitle chỉ đẩy 1 event, không có gì để cancel

    async def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[AudioChunk]:
        self._subtitles_total += 1
        self._log.info("tts_subtitle_fallback", request_id=request.request_id, chars=len(request.text))
        delivered = False
        if self._on_subtitle is not None:
            try:
                self._on_subtitle(request.request_id, request.text)
                delivered = True
            except Exception as e:  # subtitle sink lỗi không được giết pipeline
                self._log.warning("subtitle_sink_failed", error=str(e))
        if self._output_file is not None:
            try:
                self._write_overlay(request.text)
                delivered = True
            except Exception as e:
                self._log.warning("subtitle_file_failed", error=str(e))
        if self._event_bus is not None:
            try:
                self._event_bus.publish(
                    "tts_subtitle", {"request_id": request.request_id, "text": request.text}
                )
                delivered = True
            except Exception:  # pragma: no cover
                pass
        if self._require_delivery and not delivered:
            self._delivery_failures += 1
            raise RuntimeError("subtitle fallback has no working delivery sink")
        # Chunk final rỗng — pipeline biết đã kết thúc
        yield AudioChunk(
            request_id=request.request_id,
            chunk_index=0,
            audio_bytes=b"",
            is_final=True,
            duration_ms=0,
        )

    def _write_overlay(self, text: str) -> None:
        if self._output_file is None:
            return
        target = self._output_file
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(str(text), encoding="utf-8")
        os.replace(temporary, target)
