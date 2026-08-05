"""VieNeuTtsService — VieNeu-TTS v3 Turbo streaming (Phase 4).

Chốt spike day_vieneu (benchmark_clone.py):
- VieNeu-TTS v3 Turbo (48kHz, GPU PyTorch), clone qua vi_sample.wav
- BẮT BUỘC `add_voice()` trong start() để enroll ref audio 1 lần → cache
  speaker_emb + ref_codes. Nếu KHÔNG cache, mỗi infer re-encode ref → TTFA 5626ms.
  Sau khi cache: TTFA ~308ms (đo được, nhanh hơn viXTTS 450ms 32%).
- Streaming qua `infer_stream(voice=name)` yield np.float32 chunks

Audio chunk: raw float32 mono PCM @ 48kHz (bytes) — audio player 4.D dùng nguyên.
Test không GPU: inject fake `engine` qua constructor.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np

from interfaces.base import HealthStatus
from interfaces.tts import AudioChunk, TTSRequest, TTSService
from orchestrator.logger import get_logger

_SENTINEL = object()
_VOICE_NAME = "mai_ref"  # tên nội bộ cho enrolled voice


class VieNeuError(Exception):
    pass


class VieNeuTtsService(TTSService):
    service_id = "tts"

    def __init__(
        self,
        reference_audio: str | Path,
        style: str = "tu_nhien",
        denoise: bool = True,
        temperature: float = 0.8,
        top_k: int = 25,
        top_p: float = 0.95,
        max_new_frames: int = 300,
        repetition_penalty: float = 1.2,
        sample_rate: int = 48000,
        device: str = "cuda",
        backend: str = "pytorch",
        engine: Any = None,
        queue_maxsize: int = 32,
    ) -> None:
        self.reference_audio = Path(reference_audio)
        self.style = style
        self.denoise = denoise
        self.temperature = temperature
        self.top_k = top_k
        self.top_p = top_p
        self.max_new_frames = max_new_frames
        self.repetition_penalty = repetition_penalty
        self.sample_rate = sample_rate
        self.device = device
        self.backend = backend
        self._queue_maxsize = queue_maxsize

        self._engine = engine
        self._voice_enrolled = False

        self._cancelled: set[str] = set()
        self._log = get_logger("tts")

        self._requests_total = 0
        self._errors_total = 0
        self._last_ttfa_ms: float | None = None
        self._last_chunks: int = 0
        self._last_audio_ms: int = 0
        self._last_rtf: float | None = None

    @classmethod
    def from_loader(cls, loader, engine: Any = None) -> "VieNeuTtsService":
        get = lambda k, d=None: loader.get("models", f"tts.{k}", d)  # noqa: E731
        params = get("params", {}) or {}
        return cls(
            reference_audio=get("reference_audio"),
            style=str(params.get("style", "tu_nhien")),
            denoise=bool(params.get("denoise", True)),
            temperature=float(params.get("temperature", 0.8)),
            top_k=int(params.get("top_k", 25)),
            top_p=float(params.get("top_p", 0.95)),
            max_new_frames=int(params.get("max_new_frames", 300)),
            repetition_penalty=float(params.get("repetition_penalty", 1.2)),
            sample_rate=int(get("sample_rate", 48000)),
            device=str(get("device", "cuda")),
            backend=str(get("backend", "pytorch")),
            engine=engine,
        )

    # ---------- Service ----------

    async def start(self) -> None:
        if self._engine is None:
            self._engine = await asyncio.to_thread(self._load_engine)
        # Enroll ref audio 1 LẦN (cache speaker_emb + ref_codes) — critical.
        await asyncio.to_thread(self._enroll_reference)
        info: dict[str, Any] = {
            "reference_audio": str(self.reference_audio),
            "sample_rate": self.sample_rate,
            "style": self.style,
            "device": self.device,
            "backend": self.backend,
        }
        try:
            import torch
            if self.device == "cuda" and torch.cuda.is_available():
                info["vram_alloc_gb"] = round(torch.cuda.memory_allocated() / 1024**3, 2)
        except Exception:  # pragma: no cover
            pass
        self._log.info("tts_ready", **info)

    async def stop(self) -> None:
        self._engine = None
        self._voice_enrolled = False
        await asyncio.to_thread(self._reclaim_memory)

    async def health_check(self) -> HealthStatus:
        if self._engine is None or not self._voice_enrolled:
            return HealthStatus.unhealthy(self.service_id, "chưa start() hoặc voice chưa enrolled")
        return HealthStatus.healthy(self.service_id, sample_rate=self.sample_rate)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "tts_requests_total": self._requests_total,
            "tts_errors_total": self._errors_total,
            "tts_last_ttfa_ms": self._last_ttfa_ms,
            "tts_last_chunks": self._last_chunks,
            "tts_last_audio_ms": self._last_audio_ms,
            "tts_last_rtf": self._last_rtf,
        }

    def _load_engine(self) -> Any:
        from vieneu import Vieneu
        return Vieneu(mode="v3turbo", backend=self.backend)

    def _enroll_reference(self) -> None:
        if not self.reference_audio.exists():
            raise VieNeuError(f"reference_audio không tồn tại: {self.reference_audio}")
        # add_voice encode ref → speaker_emb + ref_codes (chậm 1 lần ~500-2000ms)
        # sau đó infer_stream(voice=_VOICE_NAME) chỉ lookup dict → cực nhanh.
        self._engine.add_voice(_VOICE_NAME, str(self.reference_audio), denoise=self.denoise)
        self._voice_enrolled = True

    def _reclaim_memory(self) -> None:
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    # ---------- streaming ----------

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)

    async def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[AudioChunk]:
        if self._engine is None or not self._voice_enrolled:
            raise VieNeuError("service chưa start() hoặc voice chưa enrolled")
        self._cancelled.discard(request.request_id)
        self._requests_total += 1

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_maxsize)

        def _producer() -> None:
            try:
                gen = self._engine.infer_stream(
                    text=request.text,
                    voice=_VOICE_NAME,
                    style=self.style,
                    temperature=self.temperature,
                    top_k=self.top_k,
                    top_p=self.top_p,
                    max_new_frames=self.max_new_frames,
                    repetition_penalty=self.repetition_penalty,
                )
                for chunk in gen:
                    if request.request_id in self._cancelled:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        t0 = time.perf_counter()
        t_first: float | None = None
        idx = 0
        total_samples = 0
        producer_task = loop.run_in_executor(None, _producer)

        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    self._errors_total += 1
                    raise VieNeuError(f"infer_stream failed: {item}") from item
                if request.request_id in self._cancelled:
                    self._log.info("tts_cancelled", request_id=request.request_id)
                    break

                if t_first is None:
                    t_first = time.perf_counter()
                    self._last_ttfa_ms = (t_first - t0) * 1000

                audio_np, samples = _to_float32_mono(item)
                if samples == 0:
                    continue
                total_samples += samples
                duration_ms = int(1000 * samples / self.sample_rate)
                yield AudioChunk(
                    request_id=request.request_id,
                    chunk_index=idx,
                    audio_bytes=audio_np.tobytes(),
                    is_final=False,
                    duration_ms=duration_ms,
                )
                idx += 1

            self._last_chunks = idx
            self._last_audio_ms = int(1000 * total_samples / self.sample_rate)
            if t_first is not None and self._last_audio_ms > 0:
                self._last_rtf = (time.perf_counter() - t0) / (self._last_audio_ms / 1000)
            yield AudioChunk(
                request_id=request.request_id,
                chunk_index=idx,
                audio_bytes=b"",
                is_final=True,
                duration_ms=0,
            )
        finally:
            self._cancelled.discard(request.request_id)
            if not producer_task.done():
                producer_task.cancel()


def _to_float32_mono(item: Any) -> tuple[np.ndarray, int]:
    """Chuyển 1 chunk từ VieNeu infer_stream về float32 mono numpy."""
    if hasattr(item, "detach"):
        arr = item.detach().cpu().numpy()
    elif isinstance(item, np.ndarray):
        arr = item
    else:
        arr = np.asarray(item, dtype=np.float32)
    arr = np.asarray(arr, dtype=np.float32).squeeze()
    if arr.ndim > 1:
        arr = arr.mean(axis=0)
    return arr, int(arr.shape[0])
