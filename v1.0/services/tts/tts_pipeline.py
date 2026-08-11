"""TTSPipeline — text → sentence split → TTS chain → AudioPlayer (Phase 4 4.E).

Ghép 4.B/4.C/4.D + FallbackManager (0.D) thành pipeline hoàn chỉnh:

  text
    → split_vn → list[sentence]
    → cho từng câu: FallbackManager.execute("tts", TTSRequest)
         Level 0: VieNeuTtsService.synthesize_stream (primary)
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

from interfaces.tts import (
    AudioChunk,
    TTSDeliveryMode,
    TTSDeliveryResult,
    TTSRequest,
)
from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import get_logger
from services.tts.sentence_splitter import split_vn

_CHAIN_ID = "tts"


class TTSPipeline:
    def __init__(
        self,
        primary,                    # TTSService | None (VieNeuTtsService)
        subtitle,                   # TTSService | None (SubtitleFallbackService)
        player,                     # AudioPlayer | None
        fallback: FallbackManager,
        # 15s = đủ cho câu Mai NÓI dài nhất (viXTTS ~0.5×audio ⇒ câu ~10-12s audio
        # synth mất ~5s; buffer 3× vì có lúc GPU nghẽn LLM streaming đồng thời).
        # Trước đây 3s → câu dài rơi xuống subtitle oan uổng.
        timeout_primary_s: float = 15.0,
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
        self._active_request_id: str | None = None

        self._requests_total = 0
        self._sentences_total = 0
        self._last_ttfa_ms: float | None = None
        self._last_level_max = 0     # tầng fallback cao nhất đã dùng trong turn cuối
        self._last_delivery = TTSDeliveryResult(request_id="")
        self._delivery_counts: dict[str, int] = {}
        # State cho đo TTFA đúng chỗ: mark khi chunk audio ĐẦU TIÊN được enqueue
        self._speak_t0: float | None = None
        self._speak_first_marked: bool = False
        # Serialize speak() calls — viXTTS model KHÔNG thread-safe cho concurrent
        # inference. Lock lazy tạo trong event loop (asyncio.Lock() cần loop từ 3.10-).
        self._speak_lock: asyncio.Lock | None = None
        self._fb.register_chain(
            _CHAIN_ID,
            [self._synth_primary, self._synth_subtitle],
            [timeout_primary_s, timeout_subtitle_s],
        )

    @classmethod
    def from_loader(cls, loader, primary, subtitle, player, fallback, metrics=None) -> "TTSPipeline":
        return cls(
            primary, subtitle, player, fallback,
            timeout_primary_s=float(loader.get("models", "tts.timeout_primary_s", 15.0)),
            timeout_subtitle_s=float(loader.get("models", "tts.timeout_subtitle_s", 0.5)),
            metrics=metrics,
        )

    # ---------- fallback level handlers ----------
    # Handler nhận request, tự stream + push chunk vào player, trả level marker.

    async def _synth_primary(self, request: TTSRequest) -> int:
        if self._primary is None:
            raise RuntimeError("TTS primary unavailable")
        if self._player is None:
            raise RuntimeError("audio player unavailable")
        await self._stream_to_player(self._primary, request, enqueue_audio=True)
        return 0

    async def _synth_subtitle(self, request: TTSRequest) -> int:
        if self._subtitle is None:
            raise RuntimeError("subtitle fallback unavailable")
        await self._stream_to_player(self._subtitle, request, enqueue_audio=False)
        return 1

    async def _stream_to_player(
        self,
        svc,
        request: TTSRequest,
        *,
        enqueue_audio: bool,
    ) -> AudioChunk | None:
        first_chunk: AudioChunk | None = None
        async for chunk in svc.synthesize_stream(request):
            turn_request_id = request.request_id.split("#", 1)[0]
            if turn_request_id in self._cancelled:
                await svc.cancel(request.request_id)
                break
            if chunk.audio_bytes and first_chunk is None:
                first_chunk = chunk
            if enqueue_audio:
                await self._player.enqueue(chunk)
            # Mark TTFA khi chunk audio ĐẦU TIÊN được enqueue (ngay lúc user
            # sắp nghe âm đầu), KHÔNG phải khi cả câu synth xong.
            if chunk.audio_bytes and not self._speak_first_marked and self._speak_t0 is not None:
                self._speak_first_marked = True
                self._last_ttfa_ms = (time.perf_counter() - self._speak_t0) * 1000
        return first_chunk

    # ---------- public ----------

    async def speak(self, request_id: str, text: str) -> TTSDeliveryResult:
        """Nói `text`. Trả về khi ĐÃ ENQUEUE hết chunk (không đợi phát xong).

        TTFA đo từ lúc gọi speak() tới khi chunk audio đầu tiên non-empty được
        enqueue vào player. Metric ghi vào self._last_ttfa_ms.
        """
        if self._speak_lock is None:
            self._speak_lock = asyncio.Lock()
        async with self._speak_lock:
            self._active_request_id = request_id
            try:
                return await self._speak_locked(request_id, text)
            finally:
                self._active_request_id = None

    async def _speak_locked(self, request_id: str, text: str) -> TTSDeliveryResult:
        self._cancelled.discard(request_id)
        self._requests_total += 1
        sentences = split_vn(text)
        if not sentences:
            return self._finish_delivery(TTSDeliveryResult(request_id=request_id))

        # Reset state cho lượt speak này. TTFA sẽ được mark trong _stream_to_player
        # ngay khi chunk audio đầu tiên được enqueue (chứ KHÔNG phải khi cả câu xong).
        self._speak_t0 = time.perf_counter()
        self._speak_first_marked = False
        self._last_ttfa_ms = None
        max_level = 0
        audio_sentences = 0
        subtitle_sentences = 0
        failed_sentences = 0

        for idx, sent in enumerate(sentences):
            if request_id in self._cancelled:
                break
            req = TTSRequest(request_id=f"{request_id}#{idx}", text=sent)
            try:
                result = await self._fb.execute(_CHAIN_ID, req)
                max_level = max(max_level, result.level_used)
            except Exception as e:
                self._log.warning("tts_sentence_failed", request_id=request_id, idx=idx, error=str(e))
                failed_sentences += 1
                continue
            if result.level_used == 0:
                audio_sentences += 1
            else:
                subtitle_sentences += 1
            self._sentences_total += 1

        self._last_level_max = max_level
        self._record_metrics(max_level)
        cancelled = request_id in self._cancelled
        delivered_sentences = audio_sentences + subtitle_sentences
        if cancelled:
            mode = TTSDeliveryMode.CANCELLED
        elif audio_sentences and subtitle_sentences:
            mode = TTSDeliveryMode.MIXED
        elif audio_sentences:
            mode = TTSDeliveryMode.AUDIO
        elif subtitle_sentences:
            mode = TTSDeliveryMode.SUBTITLE
        else:
            mode = TTSDeliveryMode.NONE
        result = TTSDeliveryResult(
            request_id=request_id,
            delivered=(
                not cancelled
                and failed_sentences == 0
                and delivered_sentences == len(sentences)
            ),
            mode=mode,
            sentences_total=len(sentences),
            sentences_delivered=delivered_sentences,
            audio_sentences=audio_sentences,
            subtitle_sentences=subtitle_sentences,
            failed_sentences=failed_sentences,
            cancelled=cancelled,
        )
        return self._finish_delivery(result)

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)
        if self._primary is not None:
            await self._primary.cancel(request_id)
        if self._subtitle is not None:
            await self._subtitle.cancel(request_id)
        # audio player: cancel mọi sub-request khớp prefix
        if self._player is not None:
            for i in range(64):  # tối đa 64 câu — quá đủ
                await self._player.cancel_current(f"{request_id}#{i}")

    async def cancel_all(self) -> None:
        """Cancel synthesis/playback without stopping the reusable services."""
        request_id = self._active_request_id
        if request_id is not None:
            await self.cancel(request_id)
        if self._player is not None and hasattr(self._player, "cancel_all"):
            await self._player.cancel_all()

    def get_metrics(self) -> dict[str, Any]:
        return {
            "tts_pipeline_requests_total": self._requests_total,
            "tts_pipeline_sentences_total": self._sentences_total,
            "tts_pipeline_last_ttfa_ms": self._last_ttfa_ms,
            "tts_pipeline_last_level_max": self._last_level_max,
            "tts_pipeline_last_delivery_mode": self._last_delivery.mode.value,
            "tts_pipeline_last_delivery_success": self._last_delivery.delivered,
            **{
                f"tts_pipeline_delivery_{mode}_total": count
                for mode, count in sorted(self._delivery_counts.items())
            },
        }

    def _finish_delivery(self, result: TTSDeliveryResult) -> TTSDeliveryResult:
        self._last_delivery = result
        mode = result.mode.value
        self._delivery_counts[mode] = self._delivery_counts.get(mode, 0) + 1
        return result

    def _record_metrics(self, level_used: int) -> None:
        if self._metrics is None:
            return
        rec = getattr(self._metrics, "record_tts_turn", None)
        if callable(rec):
            rec(ttfa_ms=self._last_ttfa_ms, level_used=level_used)
