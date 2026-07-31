"""TTSPipeline — text → sentence split → TTS chain → AudioPlayer (Phase 4 4.E).

Ghép 4.B/4.C/4.D + FallbackManager (0.D) thành pipeline hoàn chỉnh:

  text
    → split_vn → list[sentence]
    → cho từng câu: FallbackManager.execute("tts", TTSRequest)
         Level 0: ViXttsService.synthesize_stream (primary)
         Level 1: SubtitleFallbackService.synthesize_stream (subtitle overlay)
       forward AudioChunk → AudioPlayer.enqueue (no-overlap)
    → đo TTFA end-to-end (từ speak() gọi tới AudioChunk đầu tiên non-empty)

N7 fail-safe: primary lỗi → fallback qua chain; sentence này lỗi hết chain → skip,
tiếp câu sau (không giết cả turn). Cancel qua flag + audio_player.cancel_current.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from interfaces.tts import AudioChunk, TTSRequest
from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import get_logger
from services.tts.sentence_splitter import split_vn

_CHAIN_ID = "tts"


class TTSPipeline:
    def __init__(
        self,
        primary,                    # TTSService (ViXttsService)
        subtitle,                   # TTSService (SubtitleFallbackService)
        player,                     # AudioPlayer
        fallback: FallbackManager,
        timeout_primary_s: float = 3.0,
        timeout_subtitle_s: float = 0.5,
        metrics: Any = None,
    ) -> None:
        self._primary = primary
        self._subtitle = subtitle
        self._player = player
        self._fb = fallback
        self._metrics = metrics
        self._log = get_logger("tts_pipeline")

        self._cancelled: set[str] = set()

        self._requests_total = 0
        self._sentences_total = 0
        self._last_ttfa_ms: float | None = None
        self._last_level_max = 0     # tầng fallback cao nhất đã dùng trong turn cuối
        # State cho đo TTFA đúng chỗ: mark khi chunk audio ĐẦU TIÊN được enqueue
        self._speak_t0: float | None = None
        self._speak_first_marked: bool = False
        self._fb.register_chain(
            _CHAIN_ID,
            [self._synth_primary, self._synth_subtitle],
            [timeout_primary_s, timeout_subtitle_s],
        )

    @classmethod
    def from_loader(cls, loader, primary, subtitle, player, fallback, metrics=None) -> "TTSPipeline":
        return cls(
            primary, subtitle, player, fallback,
            timeout_primary_s=float(loader.get("models", "tts.timeout_primary_s", 3.0)),
            timeout_subtitle_s=float(loader.get("models", "tts.timeout_subtitle_s", 0.5)),
            metrics=metrics,
        )

    # ---------- fallback level handlers ----------
    # Handler nhận request, tự stream + push chunk vào player, trả level marker.

    async def _synth_primary(self, request: TTSRequest) -> int:
        await self._stream_to_player(self._primary, request)
        return 0

    async def _synth_subtitle(self, request: TTSRequest) -> int:
        await self._stream_to_player(self._subtitle, request)
        return 1

    async def _stream_to_player(self, svc, request: TTSRequest) -> AudioChunk | None:
        first_chunk: AudioChunk | None = None
        async for chunk in svc.synthesize_stream(request):
            if request.request_id in self._cancelled:
                await svc.cancel(request.request_id)
                break
            if chunk.audio_bytes and first_chunk is None:
                first_chunk = chunk
            await self._player.enqueue(chunk)
            # Mark TTFA khi chunk audio ĐẦU TIÊN được enqueue (ngay lúc user
            # sắp nghe âm đầu), KHÔNG phải khi cả câu synth xong.
            if chunk.audio_bytes and not self._speak_first_marked and self._speak_t0 is not None:
                self._speak_first_marked = True
                self._last_ttfa_ms = (time.perf_counter() - self._speak_t0) * 1000
        return first_chunk

    # ---------- public ----------

    async def speak(self, request_id: str, text: str) -> None:
        """Nói `text`. Trả về khi ĐÃ ENQUEUE hết chunk (không đợi phát xong).

        TTFA đo từ lúc gọi speak() tới khi chunk audio đầu tiên non-empty được
        enqueue vào player. Metric ghi vào self._last_ttfa_ms.
        """
        self._cancelled.discard(request_id)
        self._requests_total += 1
        sentences = split_vn(text)
        if not sentences:
            return

        # Reset state cho lượt speak này. TTFA sẽ được mark trong _stream_to_player
        # ngay khi chunk audio đầu tiên được enqueue (chứ KHÔNG phải khi cả câu xong).
        self._speak_t0 = time.perf_counter()
        self._speak_first_marked = False
        self._last_ttfa_ms = None
        max_level = 0

        for idx, sent in enumerate(sentences):
            if request_id in self._cancelled:
                break
            req = TTSRequest(request_id=f"{request_id}#{idx}", text=sent)
            try:
                result = await self._fb.execute(_CHAIN_ID, req)
                max_level = max(max_level, result.level_used)
            except Exception as e:
                self._log.warning("tts_sentence_failed", request_id=request_id, idx=idx, error=str(e))
                continue
            self._sentences_total += 1

        self._last_level_max = max_level
        self._record_metrics(max_level)

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)
        await self._primary.cancel(request_id)
        # audio player: cancel mọi sub-request khớp prefix
        for i in range(64):  # tối đa 64 câu — quá đủ
            await self._player.cancel_current(f"{request_id}#{i}")

    def get_metrics(self) -> dict[str, Any]:
        return {
            "tts_pipeline_requests_total": self._requests_total,
            "tts_pipeline_sentences_total": self._sentences_total,
            "tts_pipeline_last_ttfa_ms": self._last_ttfa_ms,
            "tts_pipeline_last_level_max": self._last_level_max,
        }

    def _record_metrics(self, level_used: int) -> None:
        if self._metrics is None:
            return
        rec = getattr(self._metrics, "record_tts_turn", None)
        if callable(rec):
            rec(ttfa_ms=self._last_ttfa_ms, level_used=level_used)
