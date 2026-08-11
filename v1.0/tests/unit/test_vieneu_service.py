"""Test VieNeuTtsService với fake engine (không cần GPU/vieneu package) — Phase 4."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from interfaces.tts import TTSRequest
from services.tts.vieneu_service import VieNeuError, VieNeuTtsService, _VOICE_NAME

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeEngine:
    """Giả VieNeu engine — không cần torch/CUDA/vieneu."""

    def __init__(
        self,
        chunks: list[np.ndarray] | None = None,
        stream_delay_s: float = 0.0,
        raise_at: int | None = None,
    ) -> None:
        # 4800 samples @ 48kHz = 100ms
        self.chunks = chunks or [np.ones(4800, dtype=np.float32) * 0.1] * 3
        self.stream_delay_s = stream_delay_s
        self.raise_at = raise_at
        self.add_voice_calls: list[tuple[str, str, bool]] = []
        self.stream_calls = 0
        self.last_kwargs: dict = {}

    def add_voice(self, name, ref_audio, denoise=True):
        self.add_voice_calls.append((name, str(ref_audio), denoise))

    def infer_stream(self, text, voice, **kw):
        self.stream_calls += 1
        self.last_kwargs = {"text": text, "voice": voice, **kw}
        for i, chunk in enumerate(self.chunks):
            if self.raise_at is not None and i == self.raise_at:
                raise RuntimeError("boom")
            if self.stream_delay_s:
                time.sleep(self.stream_delay_s)
            yield chunk


class FakeV3Core:
    def __init__(self) -> None:
        self.prepare_calls: list[tuple[str, bool, bool]] = []

    def prepare_reference(self, path: str, *, denoise: bool, use_ref_codes: bool):
        self.prepare_calls.append((path, denoise, use_ref_codes))
        return np.ones(192, dtype=np.float32), np.ones((4, 16), dtype=np.int64)


class FakeV3Engine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.engine = FakeV3Core()


@pytest.fixture
def ref_wav(tmp_path: Path) -> Path:
    """Ref audio giả (empty file) — VieNeuTtsService chỉ check tồn tại."""
    p = tmp_path / "ref.wav"
    p.write_bytes(b"RIFF")
    return p


def make_svc(ref: Path, engine: FakeEngine | None = None, **over) -> VieNeuTtsService:
    kw: dict = dict(
        reference_audio=ref,
        style="tu_nhien",
        sample_rate=48000,
        device="cpu",
        engine=engine or FakeEngine(),
    )
    kw.update(over)
    return VieNeuTtsService(**kw)


class TestLifecycle:
    async def test_start_enrolls_voice_once(self, ref_wav: Path) -> None:
        engine = FakeEngine()
        svc = make_svc(ref_wav, engine=engine)
        await svc.start()
        assert len(engine.add_voice_calls) == 1
        name, path, denoise = engine.add_voice_calls[0]
        assert name == _VOICE_NAME
        assert path == str(ref_wav)
        assert denoise is True
        assert svc._voice_enrolled is True
        h = await svc.health_check()
        assert h.is_ok is True

    async def test_start_missing_ref_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.wav"
        svc = make_svc(missing, engine=FakeEngine())
        with pytest.raises(VieNeuError, match="reference_audio không tồn tại"):
            await svc.start()

    async def test_v3_enrolls_directly_without_wrapper_temp_file(
        self, ref_wav: Path,
    ) -> None:
        engine = FakeV3Engine()
        svc = make_svc(ref_wav, engine=engine)

        await svc.start()
        _ = [
            chunk async for chunk in svc.synthesize_stream(
                TTSRequest(request_id="v3", text="xin chào"),
            )
        ]

        assert engine.add_voice_calls == []
        assert engine.engine.prepare_calls == [(str(ref_wav), True, True)]
        assert isinstance(engine.last_kwargs["voice"], dict)
        assert engine.last_kwargs["voice"]["speaker_emb"].shape == (192,)

    async def test_health_unhealthy_before_start(self, ref_wav: Path) -> None:
        svc = make_svc(ref_wav)
        h = await svc.health_check()
        assert h.is_ok is False

    async def test_stop_clears(self, ref_wav: Path) -> None:
        svc = make_svc(ref_wav)
        await svc.start()
        await svc.stop()
        assert svc._engine is None
        assert svc._voice_enrolled is False

    async def test_stream_without_start_raises(self, ref_wav: Path) -> None:
        svc = make_svc(ref_wav)
        with pytest.raises(VieNeuError, match="chưa start"):
            _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="chào"))]


class TestStream:
    async def test_yields_chunks_plus_final(self, ref_wav: Path) -> None:
        engine = FakeEngine(chunks=[np.ones(4800, dtype=np.float32) * 0.1] * 3)
        svc = make_svc(ref_wav, engine=engine)
        await svc.start()
        req = TTSRequest(request_id="r1", text="chào cậu")
        chunks = [c async for c in svc.synthesize_stream(req)]
        assert len(chunks) == 4  # 3 audio + 1 final
        assert all(not c.is_final for c in chunks[:3])
        assert chunks[-1].is_final is True
        assert chunks[-1].audio_bytes == b""

    async def test_chunk_index_increments(self, ref_wav: Path) -> None:
        svc = make_svc(ref_wav)
        await svc.start()
        chunks = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r1", text="hi"))]
        assert [c.chunk_index for c in chunks] == [0, 1, 2, 3]

    async def test_audio_bytes_shape_48khz(self, ref_wav: Path) -> None:
        # 4800 samples @ 48kHz = 100ms; float32 = 4 bytes/sample = 19200 bytes
        engine = FakeEngine(chunks=[np.ones(4800, dtype=np.float32)])
        svc = make_svc(ref_wav, engine=engine)
        await svc.start()
        c0 = None
        async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="x")):
            if not c.is_final:
                c0 = c
                break
        assert c0 is not None
        assert len(c0.audio_bytes) == 4800 * 4
        assert c0.duration_ms == 100

    async def test_passes_kwargs(self, ref_wav: Path) -> None:
        engine = FakeEngine()
        svc = make_svc(
            ref_wav, engine=engine,
            style="doc_truyen", temperature=0.7, top_k=15, max_new_frames=150,
        )
        await svc.start()
        _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="chào"))]
        assert engine.last_kwargs["voice"] == _VOICE_NAME
        assert engine.last_kwargs["style"] == "doc_truyen"
        assert engine.last_kwargs["temperature"] == 0.7
        assert engine.last_kwargs["top_k"] == 15
        assert engine.last_kwargs["max_new_frames"] == 150
        assert engine.last_kwargs["text"] == "chào"

    async def test_skips_empty_chunks(self, ref_wav: Path) -> None:
        engine = FakeEngine(chunks=[
            np.ones(4800, dtype=np.float32),
            np.array([], dtype=np.float32),  # empty chunk (VieNeu đôi khi yield)
            np.ones(4800, dtype=np.float32),
        ])
        svc = make_svc(ref_wav, engine=engine)
        await svc.start()
        chunks = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="x"))]
        # 2 audio (empty skipped) + 1 final
        assert len(chunks) == 3


class TestMetrics:
    async def test_ttfa_recorded(self, ref_wav: Path) -> None:
        engine = FakeEngine(
            chunks=[np.ones(4800, dtype=np.float32)] * 2, stream_delay_s=0.02
        )
        svc = make_svc(ref_wav, engine=engine)
        await svc.start()
        _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="hi"))]
        m = svc.get_metrics()
        assert m["tts_requests_total"] == 1
        assert m["tts_last_ttfa_ms"] is not None and m["tts_last_ttfa_ms"] > 0
        assert m["tts_last_chunks"] == 2
        assert m["tts_last_audio_ms"] == 200  # 2 × 100ms
        assert m["tts_last_rtf"] is not None


class TestErrorAndCancel:
    async def test_producer_error_propagates(self, ref_wav: Path) -> None:
        engine = FakeEngine(chunks=[np.ones(4800, dtype=np.float32)] * 3, raise_at=1)
        svc = make_svc(ref_wav, engine=engine)
        await svc.start()
        with pytest.raises(VieNeuError, match="infer_stream failed"):
            _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="hi"))]
        assert svc.get_metrics()["tts_errors_total"] == 1

    async def test_cancel_stops_stream_early(self, ref_wav: Path) -> None:
        engine = FakeEngine(
            chunks=[np.ones(1200, dtype=np.float32)] * 20, stream_delay_s=0.01
        )
        svc = make_svc(ref_wav, engine=engine)
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
    def test_reads_config(self, ref_wav: Path) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        svc = VieNeuTtsService.from_loader(loader, engine=FakeEngine())
        assert svc.style == "tu_nhien"
        assert svc.temperature == 0.75
        assert svc.max_new_frames == 500
        assert svc.sample_rate == 48000
        assert svc.backend == "pytorch"
        assert str(svc.reference_audio).endswith("vi_sample.wav")
