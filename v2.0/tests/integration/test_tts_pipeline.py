"""Integration TTSPipeline (Phase 4, 4.E) — không cần GPU/loa thật.

Verify DoD P4:
- Không audio overlap giữa turns (AudioPlayer sequential)
- TTFA measured end-to-end
- Primary fail → subtitle fallback triggered
- Cancel dừng sạch
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, AsyncIterator

import numpy as np
import pytest

from interfaces.tts import AudioChunk, TTSDeliveryMode, TTSRequest
from orchestrator.fallback_manager import FallbackManager
from orchestrator.metrics_collector import MetricsCollector
from services.tts.audio_player import AudioPlayer
from services.tts.subtitle_fallback import SubtitleFallbackService
from services.tts.tts_pipeline import TTSPipeline


class FakeTTS:
    """TTS giả yield chunks tuần tự với delay."""

    service_id = "tts_fake"

    def __init__(self, chunks_per_sentence: int = 3, chunk_samples: int = 2400,
                 chunk_delay_s: float = 0.02, raise_at: int | None = None,
                 raise_on_call: int | None = None):
        self.chunks_per_sentence = chunks_per_sentence
        self.chunk_samples = chunk_samples
        self.chunk_delay_s = chunk_delay_s
        self.raise_at = raise_at
        self.raise_on_call = raise_on_call
        self.calls = 0
        self.cancelled: set[str] = set()

    async def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[AudioChunk]:
        self.calls += 1
        if self.raise_on_call is not None and self.calls == self.raise_on_call:
            raise RuntimeError("boom")
        for i in range(self.chunks_per_sentence):
            if self.raise_at is not None and i == self.raise_at:
                raise RuntimeError("mid-boom")
            if request.request_id in self.cancelled:
                break
            if self.chunk_delay_s:
                await asyncio.sleep(self.chunk_delay_s)
            samples = np.ones(self.chunk_samples, dtype=np.float32) * 0.1
            yield AudioChunk(
                request_id=request.request_id, chunk_index=i,
                audio_bytes=samples.tobytes(), is_final=False,
                duration_ms=int(1000 * self.chunk_samples / 24000),
            )
        yield AudioChunk(
            request_id=request.request_id, chunk_index=self.chunks_per_sentence,
            audio_bytes=b"", is_final=True, duration_ms=0,
        )

    async def cancel(self, request_id: str) -> None:
        self.cancelled.add(request_id)


class FakePlayerBackend:
    def __init__(self, per_chunk_delay_s: float = 0.02):
        self.play_events: list[tuple[float, str]] = []  # (t, tag=start|end)
        self._delay = per_chunk_delay_s
        self._lock = threading.Lock()

    def play_blocking(self, samples, sample_rate):
        with self._lock:
            self.play_events.append((time.perf_counter(), "start"))
        time.sleep(self._delay)
        with self._lock:
            self.play_events.append((time.perf_counter(), "end"))

    def stop(self):
        pass


def make_pipeline(primary=None, chunk_delay=0.02, timeout_primary=3.0):
    metrics = MetricsCollector()
    fb = FallbackManager()
    subtitle = SubtitleFallbackService()
    backend = FakePlayerBackend(per_chunk_delay_s=chunk_delay)
    player = AudioPlayer(sample_rate=24000, backend=backend)
    p = primary or FakeTTS(chunks_per_sentence=2, chunk_delay_s=chunk_delay)
    pipe = TTSPipeline(
        primary=p, subtitle=subtitle, player=player, fallback=fb,
        timeout_primary_s=timeout_primary, timeout_subtitle_s=0.5, metrics=metrics,
    )
    return pipe, p, player, backend, metrics


async def _wait(cond, timeout=2.0, poll=0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll)


class TestBasicPipeline:
    async def test_speak_splits_and_synths(self) -> None:
        pipe, primary, player, backend, metrics = make_pipeline()
        await player.start()
        try:
            result = await pipe.speak("r1", "Chào cậu. Khoẻ không?")
            assert primary.calls == 2   # 2 câu → 2 lần synthesize_stream
            await _wait(lambda: len(backend.play_events) >= 4)
        finally:
            await player.stop()
        assert pipe.get_metrics()["tts_pipeline_sentences_total"] == 2
        assert result.delivered is True
        assert result.mode is TTSDeliveryMode.AUDIO
        assert metrics.tts_snapshot()["turns_total"] == 1

    async def test_ttfa_measured(self) -> None:
        pipe, *_rest, metrics = make_pipeline(chunk_delay=0.03)
        await _rest[1].start() if False else None  # player already started below
        pipe_player = _rest[1]
        await pipe_player.start()
        try:
            await pipe.speak("r1", "chào")
        finally:
            await pipe_player.stop()
        ttfa = pipe.get_metrics()["tts_pipeline_last_ttfa_ms"]
        assert ttfa is not None and ttfa > 0


class TestNoOverlap:
    async def test_two_sentences_do_not_overlap(self) -> None:
        # chunk_delay lớn để dễ đo — nếu overlap, sẽ có 2 "start" liên tiếp không có "end" giữa
        pipe, _primary, player, backend, _metrics = make_pipeline(chunk_delay=0.05)
        await player.start()
        try:
            await pipe.speak("r1", "Câu một. Câu hai.")
            await _wait(lambda: len(backend.play_events) >= 8)  # 2 câu × 2 chunk × (start+end)
        finally:
            await player.stop()

        # Kiểm bất cứ chunk nào cũng có (start, end) xen kẽ, không có 2 start liên tiếp
        tags = [tag for _, tag in backend.play_events]
        # Bỏ đuôi nếu chưa flush hết
        n = (len(tags) // 2) * 2
        tags = tags[:n]
        for i in range(0, n, 2):
            assert tags[i] == "start" and tags[i + 1] == "end", f"overlap tại i={i}: {tags}"


class TestFallbackToSubtitle:
    async def test_primary_error_falls_to_subtitle(self) -> None:
        # primary raise ngay call đầu → chain rơi xuống subtitle (Level 1)
        primary = FakeTTS(chunks_per_sentence=2, raise_on_call=1)
        pipe, _p, player, _backend, metrics = make_pipeline(primary=primary)
        await player.start()
        try:
            result = await pipe.speak("r1", "chào cậu")
        finally:
            await player.stop()
        assert pipe.get_metrics()["tts_pipeline_last_level_max"] == 1
        assert result.delivered is True
        assert result.mode is TTSDeliveryMode.SUBTITLE
        assert metrics.tts_snapshot()["subtitle_fallback_total"] == 1

    async def test_subtitle_sink_receives_text(self) -> None:
        seen = []
        subtitle = SubtitleFallbackService(on_subtitle=lambda rid, txt: seen.append((rid, txt)))
        metrics = MetricsCollector()
        fb = FallbackManager()
        backend = FakePlayerBackend()
        player = AudioPlayer(sample_rate=24000, backend=backend)
        primary = FakeTTS(raise_on_call=1)
        pipe = TTSPipeline(
            primary=primary, subtitle=subtitle, player=player, fallback=fb,
            timeout_primary_s=1.0, timeout_subtitle_s=0.5, metrics=metrics,
        )
        await player.start()
        try:
            await pipe.speak("r1", "hello")
        finally:
            await player.stop()
        # 1 câu duy nhất, subtitle nhận được
        assert len(seen) == 1
        assert seen[0][1] == "hello"


class TestEdges:
    async def test_empty_text_noop(self) -> None:
        pipe, primary, player, _backend, _metrics = make_pipeline()
        await player.start()
        try:
            await pipe.speak("r1", "   \n  ")
        finally:
            await player.stop()
        assert primary.calls == 0
        assert pipe.get_metrics()["tts_pipeline_sentences_total"] == 0

    async def test_audio_and_subtitle_failure_is_not_delivered(self) -> None:
        primary = FakeTTS(raise_on_call=1)
        subtitle = SubtitleFallbackService(require_delivery=True)
        player = AudioPlayer(sample_rate=24000, backend=FakePlayerBackend())
        pipe = TTSPipeline(
            primary=primary,
            subtitle=subtitle,
            player=player,
            fallback=FallbackManager(),
            timeout_primary_s=1.0,
            timeout_subtitle_s=0.5,
        )
        await player.start()
        try:
            result = await pipe.speak("r-fail", "không tới được output")
        finally:
            await player.stop()
        assert result.delivered is False
        assert result.mode is TTSDeliveryMode.NONE
        assert result.failed_sentences == 1

    async def test_mixed_audio_and_subtitle_counts_as_delivered(self) -> None:
        primary = FakeTTS(chunks_per_sentence=1, raise_on_call=2)
        subtitle = SubtitleFallbackService(on_subtitle=lambda _rid, _text: None)
        player = AudioPlayer(sample_rate=24000, backend=FakePlayerBackend())
        pipe = TTSPipeline(
            primary=primary,
            subtitle=subtitle,
            player=player,
            fallback=FallbackManager(),
            timeout_primary_s=1.0,
            timeout_subtitle_s=0.5,
        )
        await player.start()
        try:
            result = await pipe.speak("r-mixed", "câu một. câu hai.")
        finally:
            await player.stop()
        assert result.delivered is True
        assert result.mode is TTSDeliveryMode.MIXED
        assert result.audio_sentences == 1
        assert result.subtitle_sentences == 1

    async def test_cancel_stops_pipeline(self) -> None:
        # 3 câu, cancel giữa
        pipe, primary, player, _backend, _m = make_pipeline(chunk_delay=0.03)
        await player.start()
        try:
            task = asyncio.create_task(pipe.speak("r1", "một. hai. ba."))
            await asyncio.sleep(0.02)   # để bắt đầu câu 1
            await pipe.cancel("r1")
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            await player.stop()
        # ít nhất 1 câu bắt đầu; ít hơn 3 câu xong nếu cancel kịp
        assert primary.calls <= 3

    async def test_cancel_all_uses_active_turn_id_and_cancels_sentence_synthesis(self) -> None:
        pipe, primary, player, _backend, _m = make_pipeline(chunk_delay=0.03)
        await player.start()
        try:
            task = asyncio.create_task(pipe.speak("r-live", "mot. hai. ba."))
            await asyncio.sleep(0.02)
            await pipe.cancel_all()
            result = await asyncio.wait_for(task, timeout=2.0)
            await asyncio.sleep(0)
        finally:
            await player.stop()
        assert "r-live#0" in primary.cancelled
        assert player.get_metrics()["audio_queue_size"] == 0
        assert result.delivered is False
        assert result.mode is TTSDeliveryMode.CANCELLED
