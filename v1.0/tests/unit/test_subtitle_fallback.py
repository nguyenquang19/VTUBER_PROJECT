"""Test SubtitleFallbackService (Phase 4, 4.C)."""
from __future__ import annotations

from interfaces.tts import TTSRequest
from services.tts.subtitle_fallback import SubtitleFallbackService


class TestBasics:
    async def test_yields_single_final_chunk_empty_audio(self) -> None:
        svc = SubtitleFallbackService()
        chunks = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r1", text="chào"))]
        assert len(chunks) == 1
        assert chunks[0].is_final is True
        assert chunks[0].audio_bytes == b""
        assert chunks[0].duration_ms == 0

    async def test_invokes_subtitle_sink(self) -> None:
        seen = []
        svc = SubtitleFallbackService(on_subtitle=lambda rid, txt: seen.append((rid, txt)))
        _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r1", text="hello"))]
        assert seen == [("r1", "hello")]

    async def test_publishes_event_bus(self) -> None:
        events = []

        class Bus:
            def publish(self, topic, payload):
                events.append((topic, payload))

        svc = SubtitleFallbackService(event_bus=Bus())
        _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r1", text="hi"))]
        assert events == [("tts_subtitle", {"request_id": "r1", "text": "hi"})]

    async def test_metrics(self) -> None:
        svc = SubtitleFallbackService()
        _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r1", text="a"))]
        _ = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r2", text="b"))]
        assert svc.get_metrics()["tts_subtitle_total"] == 2


class TestFailSafe:
    async def test_sink_error_does_not_kill(self) -> None:
        def boom(*_):
            raise RuntimeError("bad sink")

        svc = SubtitleFallbackService(on_subtitle=boom)
        chunks = [c async for c in svc.synthesize_stream(TTSRequest(request_id="r", text="x"))]
        assert len(chunks) == 1        # vẫn trả final
        assert chunks[0].is_final is True

    async def test_cancel_is_noop(self) -> None:
        svc = SubtitleFallbackService()
        await svc.cancel("r")           # không raise


class TestHealth:
    async def test_always_healthy(self) -> None:
        svc = SubtitleFallbackService()
        h = await svc.health_check()
        assert h.is_ok is True
