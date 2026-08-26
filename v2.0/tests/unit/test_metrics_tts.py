"""Test TTS metrics + dashboard snapshot (Phase 4, 4.E)."""
from __future__ import annotations

from services.operations.metrics import MetricsCollector


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
