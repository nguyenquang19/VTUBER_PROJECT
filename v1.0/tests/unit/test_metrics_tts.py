"""Test TTS metrics + dashboard snapshot (Phase 4, 4.E)."""
from __future__ import annotations

from dashboard.dashboard_server import DashboardServer
from orchestrator.metrics_collector import MetricsCollector


def mc() -> MetricsCollector:
    return MetricsCollector()


class TestRecordTTSTurn:
    def test_basic_turn(self) -> None:
        m = mc()
        m.record_tts_turn(ttfa_ms=450.0, level_used=0)
        s = m.tts_snapshot()
        assert s == {"turns_total": 1, "last_ttfa_ms": 450.0, "subtitle_fallback_total": 0}

    def test_subtitle_fallback_counted(self) -> None:
        m = mc()
        m.record_tts_turn(ttfa_ms=None, level_used=1)
        assert m.tts_snapshot()["subtitle_fallback_total"] == 1

    def test_none_ttfa_safe(self) -> None:
        m = mc()
        m.record_tts_turn(ttfa_ms=None, level_used=0)
        assert m.tts_snapshot()["last_ttfa_ms"] is None

    def test_prometheus_exposes_tts(self) -> None:
        m = mc()
        m.record_tts_turn(ttfa_ms=500.0, level_used=0)
        text = m.prometheus_text().decode("utf-8")
        assert "mai_tts_turns_total" in text
        assert "mai_tts_ttfa_seconds" in text


class TestDashboardTTSSection:
    async def test_snapshot_has_tts(self) -> None:
        m = mc()
        m.record_tts_turn(ttfa_ms=450.0, level_used=0)
        server = DashboardServer(metrics=m)
        snap = await server.build_snapshot()
        assert "tts" in snap
        assert snap["tts"]["turns_total"] == 1

    async def test_merges_service_player_pipeline(self) -> None:
        m = mc()

        class FakeTTSSvc:
            def get_metrics(self):
                return {
                    "tts_requests_total": 3, "tts_errors_total": 1,
                    "tts_last_ttfa_ms": 460.0, "tts_last_chunks": 5,
                    "tts_last_rtf": 0.48,
                }

        class FakePlayer:
            def get_metrics(self):
                return {"audio_chunks_played": 7, "audio_chunks_dropped": 2,
                        "audio_queue_size": 1, "audio_is_playing": True}

        class FakePipeline:
            def get_metrics(self):
                return {"tts_pipeline_sentences_total": 12}

        server = DashboardServer(
            metrics=m, tts_service=FakeTTSSvc(), audio_player=FakePlayer(),
            tts_pipeline=FakePipeline(),
        )
        snap = await server.build_snapshot()
        t = snap["tts"]
        assert t["service"]["requests_total"] == 3
        assert t["player"]["chunks_played"] == 7
        assert t["player"]["is_playing"] is True
        assert t["pipeline"]["sentences_total"] == 12

    async def test_no_metrics_no_section(self) -> None:
        server = DashboardServer()
        snap = await server.build_snapshot()
        assert "tts" not in snap
