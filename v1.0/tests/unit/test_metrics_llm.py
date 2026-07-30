"""Test LLM metrics trong MetricsCollector + dashboard snapshot (1.F)."""
from __future__ import annotations

from dashboard.dashboard_server import DashboardServer
from orchestrator.metrics_collector import MetricsCollector


def mc() -> MetricsCollector:
    return MetricsCollector()


class TestRecordLLMTurn:
    def test_basic_snapshot(self) -> None:
        m = mc()
        m.record_llm_turn(ttft_ms=120.0, decode_tps=40.0, parse_ok=True, level_used=0)
        s = m.llm_snapshot()
        assert s["last_ttft_ms"] == 120.0
        assert s["last_decode_tps"] == 40.0
        assert s["requests_total"] == 1
        assert s["parse_ok"] == 1
        assert s["parse_total"] == 1
        assert s["parse_rate_percent"] == 100.0
        assert s["fallback_total"] == 0

    def test_parse_rate(self) -> None:
        m = mc()
        for _ in range(3):
            m.record_llm_turn(100.0, 40.0, parse_ok=True, level_used=0)
        m.record_llm_turn(100.0, 40.0, parse_ok=False, level_used=0)
        assert m.llm_snapshot()["parse_rate_percent"] == 75.0

    def test_fallback_counted(self) -> None:
        m = mc()
        m.record_llm_turn(None, None, parse_ok=False, level_used=1)
        s = m.llm_snapshot()
        assert s["fallback_total"] == 1
        assert s["requests_total"] == 1

    def test_none_ttft_safe(self) -> None:
        m = mc()
        m.record_llm_turn(None, None, parse_ok=True, level_used=0)
        s = m.llm_snapshot()
        assert s["last_ttft_ms"] is None
        assert s["last_decode_tps"] is None

    def test_empty_snapshot_rate_none(self) -> None:
        assert mc().llm_snapshot()["parse_rate_percent"] is None

    def test_prometheus_exposes_llm(self) -> None:
        m = mc()
        m.record_llm_turn(100.0, 40.0, parse_ok=True, level_used=0)
        text = m.prometheus_text().decode("utf-8")
        assert "mai_llm_requests_total" in text
        assert "mai_llm_parse_total" in text


class TestDashboardLLMSection:
    async def test_snapshot_includes_llm(self) -> None:
        m = mc()
        m.record_llm_turn(150.0, 42.0, parse_ok=True, level_used=0)
        server = DashboardServer(metrics=m)
        snap = await server.build_snapshot()
        assert "llm" in snap
        assert snap["llm"]["requests_total"] == 1
        assert snap["llm"]["last_ttft_ms"] == 150.0
