"""Test ViXttsService với fake model (không cần GPU) — Phase 4, 4.B."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np
import pytest

from interfaces.tts import TTSRequest
from services.tts.vixtts_service import ViXttsError, ViXttsService

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeModel:
    """Model giả — không cần torch/CUDA. Trả tuple (fake_lat, fake_emb) khi tính
    latents; inference_stream yield np.ndarray chunks."""

    def __init__(
        self,
        chunks: list[np.ndarray] | None = None,
        stream_delay_s: float = 0.0,
        raise_at: int | None = None,
    ) -> None:
        self.chunks = chunks or [np.ones(2400, dtype=np.float32) * 0.1] * 3
        self.stream_delay_s = stream_delay_s
        self.raise_at = raise_at
        self.latents_calls = 0
        self.stream_calls = 0
        self.last_kwargs: dict = {}

    def get_conditioning_latents(self, **kw):
        self.latents_calls += 1
        return ("fake_gpt_lat", "fake_spk_emb")

    def inference_stream(self, text, language, gpt_lat, spk_emb, **kw):
        self.stream_calls += 1
        self.last_kwargs = {"text": text, "language": language, **kw}
        for i, chunk in enumerate(self.chunks):
            if self.raise_at is not None and i == self.raise_at:
                raise RuntimeError("boom")
            if self.stream_delay_s:
                time.sleep(self.stream_delay_s)
            yield chunk


def make_svc(model: FakeModel | None = None, **over) -> ViXttsService:
    kw = dict(
        model_dir="fake_dir",
        speaker_wav="fake.wav",
        language="vi",
        sample_rate=24000,
        device="cpu",
        model=model or FakeModel(),
    )
    kw.update(over)
    return ViXttsService(**kw)


class TestLifecycle:
    async def test_start_computes_latents_once(self) -> None:
        model = FakeModel()
        svc = make_svc(model=model)
        await svc.start()
        assert model.latents_calls == 1
        assert svc._gpt_lat == "fake_gpt_lat"
        h = await svc.health_check()
        assert h.is_ok is True

    async def test_health_unhealthy_before_start(self) -> None:
        svc = make_svc()
        h = await svc.health_check()
        assert h.is_ok is False

    async def test_stop_clears(self) -> None:
        svc = make_svc()
        await svc.start()
        await svc.stop()
        assert svc._model is None
        assert svc._gpt_lat is None

    async def test_stream_without_start_raises(self) -> None:
        svc = make_svc()
        with pytest.raises(ViXttsError):
            _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="chào"))]


class TestStream:
    async def test_yields_chunks_plus_final(self) -> None:
        model = FakeModel(chunks=[np.ones(2400, dtype=np.float32) * 0.1] * 3)
        svc = make_svc(model=model)
        await svc.start()
        req = TTSRequest(request_id="r1", text="chào cậu")
        chunks = [c async for c in svc.synthesize_stream(req)]
        # 3 audio + 1 final
        assert len(chunks) == 4
        assert all(not c.is_final for c in chunks[:3])
        assert chunks[-1].is_final is True
        assert chunks[-1].audio_bytes == b""

    async def test_chunk_index_increments(self) -> None:
        svc = make_svc()
        await svc.start()
        chunks = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r1", text="hi"))]
        assert [c.chunk_index for c in chunks] == [0, 1, 2, 3]

    async def test_audio_bytes_shape(self) -> None:
        # 2400 samples @ 24kHz = 100ms; float32 = 4 bytes/sample = 9600 bytes
        svc = make_svc(model=FakeModel(chunks=[np.ones(2400, dtype=np.float32)]))
        await svc.start()
        c0 = None
        async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="x")):
            if not c.is_final:
                c0 = c
                break
        assert c0 is not None
        assert len(c0.audio_bytes) == 2400 * 4
        assert c0.duration_ms == 100

    async def test_passes_kwargs(self) -> None:
        model = FakeModel()
        svc = make_svc(model=model, temperature=0.9, repetition_penalty=3.0, stream_chunk_size=15)
        await svc.start()
        _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="chào"))]
        assert model.last_kwargs["temperature"] == 0.9
        assert model.last_kwargs["repetition_penalty"] == 3.0
        assert model.last_kwargs["stream_chunk_size"] == 15
        assert model.last_kwargs["language"] == "vi"
        assert model.last_kwargs["text"] == "chào"


class TestMetrics:
    async def test_ttfa_recorded(self) -> None:
        svc = make_svc(model=FakeModel(chunks=[np.ones(2400, dtype=np.float32)] * 2, stream_delay_s=0.02))
        await svc.start()
        _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="hi"))]
        m = svc.get_metrics()
        assert m["tts_requests_total"] == 1
        assert m["tts_last_ttfa_ms"] is not None and m["tts_last_ttfa_ms"] > 0
        assert m["tts_last_chunks"] == 2
        assert m["tts_last_audio_ms"] == 200      # 2 × 100ms
        assert m["tts_last_rtf"] is not None


class TestErrorAndCancel:
    async def test_producer_error_propagates(self) -> None:
        svc = make_svc(model=FakeModel(chunks=[np.ones(2400, dtype=np.float32)] * 3, raise_at=1))
        await svc.start()
        with pytest.raises(ViXttsError, match="inference_stream failed"):
            _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="hi"))]
        assert svc.get_metrics()["tts_errors_total"] == 1

    async def test_cancel_stops_stream_early(self) -> None:
        # 20 chunks nhỏ + delay để cancel kịp
        chunks = [np.ones(1200, dtype=np.float32)] * 20
        svc = make_svc(model=FakeModel(chunks=chunks, stream_delay_s=0.01))
        await svc.start()
        got = 0
        async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="dài")):
            if c.is_final:
                break
            got += 1
            if got >= 3:
                await svc.cancel("r")
        assert got < 20


class TestFromLoader:
    def test_reads_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        svc = ViXttsService.from_loader(loader, model=FakeModel())
        assert svc.language == "vi"
        assert svc.gpt_cond_len == 30
        assert svc.sample_rate == 24000
        assert str(svc.model_dir).endswith("vixtts")
