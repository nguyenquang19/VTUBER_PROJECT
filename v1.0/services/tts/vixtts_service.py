"""ViXttsService — viXTTS streaming (ARCHITECTURE 8.4, Phase 4 4.B).

Chốt Pre-flight Day 2 (spike/day2_report.md):
- viXTTS voice-clone qua vi_sample.wav, gpt_cond_len=30 + VN cleaner (num2words)
- BẮT BUỘC `Xtts.inference_stream()` (TTFA ~450ms), KHÔNG `synthesize()` (blocking 2.6s)
- `get_conditioning_latents()` gọi 1 LẦN → cache `gpt_cond_lat` + `speaker_emb`

Audio chunk: raw float32 mono PCM @ 24kHz (bytes) — audio player 4.D chịu trách
nhiệm phát; interface AudioChunk chỉ mang bytes + duration_ms.

Test không GPU: inject fake `model` qua constructor. Live test có marker `tts`.
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
from services.tts.vixtts_patches import apply_patches

_SENTINEL = object()  # đánh dấu producer đã xong


class ViXttsError(Exception):
    pass


class ViXttsService(TTSService):
    service_id = "tts"

    def __init__(
        self,
        model_dir: str | Path,
        speaker_wav: str | Path,
        language: str = "vi",
        gpt_cond_len: int = 30,
        gpt_cond_chunk_len: int = 6,
        temperature: float = 0.75,
        length_penalty: float = 1.0,
        repetition_penalty: float = 5.0,
        stream_chunk_size: int = 20,
        sample_rate: int = 24000,
        device: str = "cuda",
        model: Any = None,
        queue_maxsize: int = 32,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.speaker_wav = Path(speaker_wav)
        self.language = language
        self.gpt_cond_len = gpt_cond_len
        self.gpt_cond_chunk_len = gpt_cond_chunk_len
        self.temperature = temperature
        self.length_penalty = length_penalty
        self.repetition_penalty = repetition_penalty
        self.stream_chunk_size = stream_chunk_size
        self.sample_rate = sample_rate
        self.device = device
        self._queue_maxsize = queue_maxsize

        self._model = model
        self._gpt_lat: Any = None
        self._spk_emb: Any = None

        self._cancelled: set[str] = set()
        self._log = get_logger("tts")

        self._requests_total = 0
        self._errors_total = 0
        self._last_ttfa_ms: float | None = None
        self._last_chunks: int = 0
        self._last_audio_ms: int = 0
        self._last_rtf: float | None = None

    @classmethod
    def from_loader(cls, loader, model: Any = None) -> "ViXttsService":
        get = lambda k, d=None: loader.get("models", f"tts.{k}", d)  # noqa: E731
        params = get("params", {}) or {}
        return cls(
            model_dir=get("model_dir"),
            speaker_wav=get("reference_audio"),
            language=params.get("language", "vi"),
            gpt_cond_len=int(params.get("gpt_cond_len", 30)),
            gpt_cond_chunk_len=int(params.get("gpt_cond_chunk_len", 6)),
            temperature=float(params.get("temperature", 0.75)),
            length_penalty=float(params.get("length_penalty", 1.0)),
            repetition_penalty=float(params.get("repetition_penalty", 5.0)),
            sample_rate=int(get("sample_rate", 24000)),
            device=str(get("device", "cuda")),
            model=model,
        )

    # ---------- Service ----------

    async def start(self) -> None:
        apply_patches()
        if self._model is None:
            self._model = await asyncio.to_thread(self._load_model)
        # Cache conditioning latents 1 lần (spike day2 quyết định)
        self._gpt_lat, self._spk_emb = await asyncio.to_thread(self._compute_latents)
        # Sau khi model đã lên GPU + latents cached, dọn CPU-side để RAM sụt lại
        await asyncio.to_thread(self._reclaim_memory)
        info: dict[str, Any] = {
            "model_dir": str(self.model_dir),
            "sample_rate": self.sample_rate,
            "gpt_cond_len": self.gpt_cond_len,
            "device": self.device,
        }
        try:
            import torch

            if self.device == "cuda" and torch.cuda.is_available():
                info["vram_alloc_gb"] = round(torch.cuda.memory_allocated() / 1024**3, 2)
                info["vram_reserved_gb"] = round(torch.cuda.memory_reserved() / 1024**3, 2)
        except Exception:  # pragma: no cover
            pass
        self._log.info("tts_ready", **info)

    async def stop(self) -> None:
        self._model = None
        self._gpt_lat = None
        self._spk_emb = None
        await asyncio.to_thread(self._reclaim_memory)

    async def health_check(self) -> HealthStatus:
        if self._model is None or self._gpt_lat is None:
            return HealthStatus.unhealthy(self.service_id, "chưa start() hoặc model chưa load")
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

    def _load_model(self) -> Any:
        import torch
        from TTS.tts.configs.xtts_config import XttsConfig
        from TTS.tts.models.xtts import Xtts

        # eval-only: bỏ autograd để giảm RAM/VRAM (không cần graph)
        torch.set_grad_enabled(False)

        config = XttsConfig()
        config.load_json(str(self.model_dir / "config.json"))
        model = Xtts.init_from_config(config)
        model.load_checkpoint(
            config, checkpoint_dir=str(self.model_dir), use_deepspeed=False, eval=True
        )
        if self.device == "cuda":
            if not torch.cuda.is_available():
                raise ViXttsError(
                    "config yêu cầu device=cuda nhưng torch.cuda KHÔNG khả dụng"
                )
            model.cuda()
        return model

    def _reclaim_memory(self) -> None:
        """Giải phóng CPU-side tensor còn treo + trim CUDA cache. Gọi sau move CUDA."""
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _compute_latents(self) -> tuple[Any, Any]:
        assert self._model is not None
        return self._model.get_conditioning_latents(
            audio_path=str(self.speaker_wav),
            gpt_cond_len=self.gpt_cond_len,
            gpt_cond_chunk_len=self.gpt_cond_chunk_len,
        )

    # ---------- streaming ----------

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)

    async def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[AudioChunk]:
        if self._model is None or self._gpt_lat is None:
            raise ViXttsError("service chưa start()")
        self._cancelled.discard(request.request_id)
        self._requests_total += 1

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_maxsize)

        def _producer() -> None:
            try:
                gen = self._model.inference_stream(
                    request.text,
                    self.language,
                    self._gpt_lat,
                    self._spk_emb,
                    stream_chunk_size=self.stream_chunk_size,
                    temperature=self.temperature,
                    length_penalty=self.length_penalty,
                    repetition_penalty=self.repetition_penalty,
                )
                for chunk in gen:
                    if request.request_id in self._cancelled:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:  # forward exception qua queue
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        t0 = time.perf_counter()
        t_first: float | None = None
        idx = 0
        total_samples = 0
        producer_task = asyncio.get_running_loop().run_in_executor(None, _producer)

        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    self._errors_total += 1
                    raise ViXttsError(f"inference_stream failed: {item}") from item
                if request.request_id in self._cancelled:
                    self._log.info("tts_cancelled", request_id=request.request_id)
                    break

                if t_first is None:
                    t_first = time.perf_counter()
                    self._last_ttfa_ms = (t_first - t0) * 1000

                audio_np, samples = _to_float32_mono(item)
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

            # Final marker (empty audio)
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
            with_suppress = getattr(producer_task, "cancel", None)
            if with_suppress and not producer_task.done():
                producer_task.cancel()


def _to_float32_mono(item: Any) -> tuple[np.ndarray, int]:
    """Chuyển 1 chunk từ Xtts.inference_stream về float32 mono numpy.

    Xtts yield torch tensor (CUDA hoặc CPU). Cũng chấp nhận np.ndarray/list cho test.
    """
    if hasattr(item, "detach"):        # torch.Tensor
        arr = item.detach().cpu().numpy()
    elif isinstance(item, np.ndarray):
        arr = item
    else:
        arr = np.asarray(item, dtype=np.float32)
    arr = np.asarray(arr, dtype=np.float32).squeeze()
    if arr.ndim > 1:
        arr = arr.mean(axis=0)         # collapse channels → mono
    return arr, int(arr.shape[0])
