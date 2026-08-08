"""AudioPlayer — phát AudioChunk tuần tự, không overlap (Phase 4, 4.D).

Nhận `AudioChunk` (float32 mono PCM @ sample_rate), enqueue, phát tuần tự:
chunk N+1 chỉ bắt đầu khi N phát xong → KHÔNG overlap giữa turns (DoD P4).

Backend abstracted: default là `sounddevice.play + wait` (blocking) chạy trong
`asyncio.to_thread`. Test inject `FakeBackend` để không mở device thật.
`cancel_current(request_id)` dừng ngay chunk đang phát + drop mọi chunk cùng
request_id còn trong queue.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol

import numpy as np

from interfaces.tts import AudioChunk
from orchestrator.logger import get_logger


class AudioBackend(Protocol):
    """Backend phát audio synchronous. `play_blocking` phải chờ tới khi kết thúc."""

    def play_blocking(self, samples: np.ndarray, sample_rate: int) -> None: ...
    def stop(self) -> None: ...


class SounddeviceBackend:
    """Backend thật bằng sounddevice.

    Dùng OutputStream persistent + `stream.write` per chunk (KHÔNG `sd.play+sd.wait`
    per chunk — cách đó mở/đóng stream mỗi lần → click/gap giữa chunk).
    `stream.write` blocking khi buffer đầy → pacing gần realtime, không giật.
    `stop()` abort stream đang phát (dùng khi cancel_current).
    """

    def __init__(self) -> None:
        self._stream: Any = None
        self._sr: int | None = None

    def _ensure_stream(self, sample_rate: int) -> None:
        import sounddevice as sd

        if self._stream is not None and self._sr == sample_rate:
            return
        self._teardown()
        self._stream = sd.OutputStream(
            samplerate=sample_rate, channels=1, dtype="float32", blocksize=0,
        )
        self._stream.start()
        self._sr = sample_rate

    def play_blocking(self, samples: np.ndarray, sample_rate: int) -> None:
        self._ensure_stream(sample_rate)
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        # write blocks khi buffer PortAudio đầy → giữ pacing gần realtime.
        # Không gọi wait() giữa chunk (đó là nguồn gap/click). Cuối turn, worker
        # xử chunk tiếp; giữa turn, Queue của AudioPlayer serialize.
        self._stream.write(samples.astype("float32"))

    def stop(self) -> None:
        # cancel_current: abort ngay + drop buffered
        self._teardown()

    def _teardown(self) -> None:
        stream = self._stream
        self._stream = None
        self._sr = None
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass


class AudioPlayer:
    def __init__(
        self,
        sample_rate: int = 24000,
        backend: AudioBackend | None = None,
        queue_maxsize: int = 128,
    ) -> None:
        self.sample_rate = sample_rate
        self._backend: AudioBackend = backend or SounddeviceBackend()
        self._queue: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker: asyncio.Task[None] | None = None
        self._running = False
        self._current_request_id: str | None = None
        self._cancelled_ids: set[str] = set()
        self._log = get_logger("audio_player")

        self.chunks_played = 0
        self.chunks_dropped = 0

    async def start(self) -> None:
        if self._worker is None:
            self._running = True
            self._worker = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        # đẩy None-marker để worker thoát vòng chờ queue
        try:
            self._queue.put_nowait(_STOP)  # type: ignore[arg-type]
        except asyncio.QueueFull:
            pass
        self._backend.stop()
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._worker, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker.cancel()
                with contextlib.suppress(Exception):
                    await self._worker
            self._worker = None

    async def enqueue(self, chunk: AudioChunk) -> None:
        await self._queue.put(chunk)

    async def cancel_current(self, request_id: str) -> None:
        """Đánh dấu request_id bị huỷ + stop chunk hiện tại (nếu cùng id)."""
        self._cancelled_ids.add(request_id)
        if self._current_request_id == request_id:
            self._backend.stop()

    async def cancel_all(self) -> None:
        """Immediately stop playback and discard every queued audio chunk."""
        if self._current_request_id is not None:
            self._cancelled_ids.add(self._current_request_id)
        self._backend.stop()
        while True:
            try:
                chunk = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(chunk, AudioChunk):
                self._cancelled_ids.add(chunk.request_id)

    @property
    def is_playing(self) -> bool:
        return self._current_request_id is not None

    def get_metrics(self) -> dict[str, Any]:
        return {
            "audio_chunks_played": self.chunks_played,
            "audio_chunks_dropped": self.chunks_dropped,
            "audio_queue_size": self._queue.qsize(),
            "audio_is_playing": self.is_playing,
        }

    # ---------- worker ----------

    async def _loop(self) -> None:
        while self._running:
            try:
                chunk = await self._queue.get()
            except asyncio.CancelledError:
                return
            if chunk is _STOP:
                return
            if not isinstance(chunk, AudioChunk):
                continue  # ignore junk
            # drop chunk bị cancel
            if chunk.request_id in self._cancelled_ids:
                self.chunks_dropped += 1
                continue
            if not chunk.audio_bytes:
                # final marker — reset current + đợi lệnh sau
                if self._current_request_id == chunk.request_id:
                    self._current_request_id = None
                continue
            samples = np.frombuffer(chunk.audio_bytes, dtype=np.float32)
            self._current_request_id = chunk.request_id
            try:
                await asyncio.to_thread(self._backend.play_blocking, samples, self.sample_rate)
                self.chunks_played += 1
            except Exception as e:
                self._log.warning("audio_play_failed", error=str(e))


class _StopSentinel:
    pass


_STOP = _StopSentinel()
