"""Test AudioPlayer với FakeBackend (không mở audio device) — Phase 4, 4.D."""
from __future__ import annotations

import asyncio
import threading
import time

import numpy as np

from interfaces.tts import AudioChunk
from services.tts.audio_player import AudioPlayer


class FakeBackend:
    """Ghi lại (samples, sr) đã phát. play_blocking sleep để mô phỏng thời gian."""

    def __init__(self, per_chunk_delay_s: float = 0.02):
        self.played: list[tuple[int, int]] = []  # (nsamples, sr)
        self.stops = 0
        self._delay = per_chunk_delay_s
        self._stop_event = threading.Event()

    def play_blocking(self, samples, sample_rate):
        self._stop_event.clear()
        # simulate blocking playback interruptible by stop()
        deadline = time.perf_counter() + self._delay
        while time.perf_counter() < deadline:
            if self._stop_event.is_set():
                return
            time.sleep(0.005)
        self.played.append((int(samples.shape[0]), sample_rate))

    def stop(self):
        self.stops += 1
        self._stop_event.set()


def chunk(rid: str, idx: int, samples: int = 240, is_final: bool = False, sample_rate: int = 24000) -> AudioChunk:
    audio = np.zeros(samples, dtype=np.float32).tobytes() if not is_final else b""
    return AudioChunk(request_id=rid, chunk_index=idx, audio_bytes=audio,
                      is_final=is_final, duration_ms=int(1000 * samples / sample_rate))


async def _wait_played(backend: FakeBackend, n: int, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while len(backend.played) < n and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)


class TestSequential:
    async def test_plays_in_order_no_overlap(self) -> None:
        be = FakeBackend(per_chunk_delay_s=0.03)
        p = AudioPlayer(sample_rate=24000, backend=be)
        await p.start()
        try:
            for i in range(3):
                await p.enqueue(chunk("r1", i))
            await p.enqueue(chunk("r1", 3, is_final=True))
            await _wait_played(be, 3)
            assert len(be.played) == 3
            assert p.get_metrics()["audio_chunks_played"] == 3
            assert p.is_playing is False    # final → reset current
        finally:
            await p.stop()

    async def test_two_requests_dont_overlap(self) -> None:
        be = FakeBackend(per_chunk_delay_s=0.03)
        p = AudioPlayer(sample_rate=24000, backend=be)
        await p.start()
        try:
            await p.enqueue(chunk("r1", 0))
            await p.enqueue(chunk("r1", 1, is_final=True))
            await p.enqueue(chunk("r2", 0))
            await p.enqueue(chunk("r2", 1, is_final=True))
            await _wait_played(be, 2)
            assert be.played == [(240, 24000), (240, 24000)]
        finally:
            await p.stop()


class TestCancel:
    async def test_cancel_all_stops_backend_and_drains_queue(self) -> None:
        be = FakeBackend(per_chunk_delay_s=0.3)
        p = AudioPlayer(sample_rate=24000, backend=be)
        await p.start()
        try:
            for i in range(5):
                await p.enqueue(chunk(f"r{i}", 0))
            await asyncio.sleep(0.02)
            await p.cancel_all()
            assert p.get_metrics()["audio_queue_size"] == 0
            assert be.stops >= 1
        finally:
            await p.stop()

    async def test_cancel_drops_pending_of_same_request(self) -> None:
        be = FakeBackend(per_chunk_delay_s=0.05)
        p = AudioPlayer(sample_rate=24000, backend=be)
        await p.start()
        try:
            # nhét 5 chunk r1, cancel ngay → chỉ chunk đang chạy có thể đã play (bị stop),
            # còn lại drop.
            for i in range(5):
                await p.enqueue(chunk("r1", i))
            await asyncio.sleep(0.01)   # để worker bắt chunk đầu
            await p.cancel_current("r1")
            # nhét thêm r2 sau khi cancel r1
            await p.enqueue(chunk("r2", 0))
            await _wait_played(be, 1)
            # ít nhất 1 chunk r2 chạy được; nhiều chunk r1 bị drop
            assert p.get_metrics()["audio_chunks_dropped"] >= 3
        finally:
            await p.stop()

    async def test_cancel_stops_current_chunk_immediately(self) -> None:
        be = FakeBackend(per_chunk_delay_s=0.3)   # chunk dài
        p = AudioPlayer(sample_rate=24000, backend=be)
        await p.start()
        try:
            await p.enqueue(chunk("r1", 0))
            await asyncio.sleep(0.02)     # để worker vào giữa chunk
            t0 = asyncio.get_event_loop().time()
            await p.cancel_current("r1")
            # cho worker time để hủy
            await asyncio.sleep(0.05)
            elapsed = asyncio.get_event_loop().time() - t0
            assert elapsed < 0.25              # dừng trước khi hết 300ms
            assert be.stops >= 1
        finally:
            await p.stop()


class TestLifecycle:
    async def test_stop_clean_when_idle(self) -> None:
        p = AudioPlayer(sample_rate=24000, backend=FakeBackend())
        await p.start()
        await asyncio.wait_for(p.stop(), timeout=2.0)

    async def test_metrics_empty_initially(self) -> None:
        p = AudioPlayer(sample_rate=24000, backend=FakeBackend())
        m = p.get_metrics()
        assert m["audio_chunks_played"] == 0
        assert m["audio_chunks_dropped"] == 0
        assert m["audio_is_playing"] is False


class TestFinalChunk:
    async def test_final_empty_chunk_resets_current(self) -> None:
        be = FakeBackend(per_chunk_delay_s=0.01)
        p = AudioPlayer(sample_rate=24000, backend=be)
        await p.start()
        try:
            await p.enqueue(chunk("r1", 0))
            await p.enqueue(chunk("r1", 1, is_final=True))
            await _wait_played(be, 1)
            await asyncio.sleep(0.03)  # đợi worker xử final
            assert p.is_playing is False
        finally:
            await p.stop()


class CapturingBackend:
    """Ghi lại chính mảng samples đã phát (để kiểm pitch-shift áp đúng)."""

    def __init__(self) -> None:
        self.samples: list[np.ndarray] = []

    def play_blocking(self, samples, sample_rate):
        self.samples.append(np.asarray(samples, dtype=np.float32).copy())

    def stop(self):
        pass


def _sine(freq: float, sr: int, n: int) -> np.ndarray:
    t = np.arange(n) / sr
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _dominant_hz(sig: np.ndarray, sr: int) -> float:
    spectrum = np.abs(np.fft.rfft(sig))
    return float(np.fft.rfftfreq(sig.size, 1 / sr)[int(np.argmax(spectrum))])


class TestPitchShift:
    def test_clamp_bounds(self) -> None:
        from services.tts.pitch import clamp_semitones
        assert clamp_semitones(99) == 12.0
        assert clamp_semitones(-99) == -12.0
        assert clamp_semitones(3.5) == 3.5

    def test_zero_is_noop_identity(self) -> None:
        from services.tts.pitch import pitch_shift_samples
        x = _sine(220.0, 22050, 4096)
        out = pitch_shift_samples(x, 22050, 0.0)
        assert out is x  # fast path: trả nguyên object, không tốn CPU

    def test_pitch_up_one_octave_doubles_frequency(self) -> None:
        from services.tts.pitch import pitch_shift_samples
        sr, f0 = 22050, 220.0
        x = _sine(f0, sr, 22050)  # 1s
        up = pitch_shift_samples(x, sr, 12.0)  # +1 octave
        assert up.shape == x.shape  # giữ nguyên độ dài
        assert abs(_dominant_hz(up, sr) - 2 * f0) < 20.0  # ~440Hz

    def test_player_metric_reports_and_clamps(self) -> None:
        p = AudioPlayer(sample_rate=48000, pitch_semitones=50.0, backend=FakeBackend())
        assert p.get_metrics()["audio_pitch_semitones"] == 12.0  # clamped
        p2 = AudioPlayer(sample_rate=48000, pitch_semitones=3.0, backend=FakeBackend())
        assert p2.get_metrics()["audio_pitch_semitones"] == 3.0

    async def test_player_applies_pitch_when_nonzero(self) -> None:
        sr = 24000
        raw = _sine(200.0, sr, 4096)
        ch = AudioChunk(request_id="r1", chunk_index=0, audio_bytes=raw.tobytes(),
                        is_final=False, duration_ms=int(1000 * raw.size / sr))
        be = CapturingBackend()
        p = AudioPlayer(sample_rate=sr, backend=be, pitch_semitones=5.0)
        await p.start()
        try:
            await p.enqueue(ch)
            deadline = asyncio.get_event_loop().time() + 2.0
            while not be.samples and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert be.samples, "chunk chưa được phát"
            played = be.samples[0]
            assert played.shape == raw.shape          # độ dài giữ nguyên
            assert not np.allclose(played, raw)       # cao độ đã đổi
        finally:
            await p.stop()

    async def test_player_zero_pitch_passes_through(self) -> None:
        sr = 24000
        raw = _sine(200.0, sr, 4096)
        ch = AudioChunk(request_id="r1", chunk_index=0, audio_bytes=raw.tobytes(),
                        is_final=False, duration_ms=int(1000 * raw.size / sr))
        be = CapturingBackend()
        p = AudioPlayer(sample_rate=sr, backend=be, pitch_semitones=0.0)
        await p.start()
        try:
            await p.enqueue(ch)
            deadline = asyncio.get_event_loop().time() + 2.0
            while not be.samples and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert be.samples, "chunk chưa được phát"
            assert np.array_equal(be.samples[0], raw)  # no-op: y hệt input
        finally:
            await p.stop()
