"""Test SubtitleFallbackService (Phase 4, 4.C)."""
from __future__ import annotations

import pytest

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

    async def test_required_delivery_without_sink_fails(self) -> None:
        svc = SubtitleFallbackService(require_delivery=True)
        with pytest.raises(RuntimeError, match="no working delivery sink"):
            _ = [
                chunk async for chunk in svc.synthesize_stream(
                    TTSRequest(request_id="r", text="x"),
                )
            ]

    async def test_writes_atomic_overlay_file(self, tmp_path) -> None:
        output = tmp_path / "live" / "subtitle.txt"
        svc = SubtitleFallbackService(output_file=output, require_delivery=True)
        _ = [
            chunk async for chunk in svc.synthesize_stream(
                TTSRequest(request_id="r", text="Mai đang nói"),
            )
        ]
        assert output.read_text(encoding="utf-8") == "Mai đang nói"
        assert not output.with_suffix(".txt.tmp").exists()


class TestHealth:
    async def test_without_verified_sink_is_degraded(self) -> None:
        svc = SubtitleFallbackService()
        h = await svc.health_check()
        assert h.is_ok is False
        assert h.state.value == "degraded"

    async def test_callback_sink_is_healthy(self) -> None:
        svc = SubtitleFallbackService(on_subtitle=lambda _rid, _text: None)
        assert (await svc.health_check()).is_ok is True

    async def test_writable_file_sink_is_healthy(self, tmp_path) -> None:
        svc = SubtitleFallbackService(
            output_file=tmp_path / "live" / "subtitle.txt", require_delivery=True,
        )
        assert (await svc.health_check()).is_ok is True

    async def test_required_delivery_without_sink_is_unhealthy(self) -> None:
        svc = SubtitleFallbackService(require_delivery=True)
        h = await svc.health_check()
        assert h.is_ok is False
        assert h.state.value == "unhealthy"
