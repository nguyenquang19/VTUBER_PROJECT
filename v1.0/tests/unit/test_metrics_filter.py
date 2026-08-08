"""Test filter metrics + dashboard snapshot (Phase 3, 3.C)."""
from __future__ import annotations

import pytest

from dashboard.dashboard_server import DashboardServer
from orchestrator.metrics_collector import MetricsCollector


def mc() -> MetricsCollector:
    return MetricsCollector()


class TestRecordFilterCheck:
    def test_pass_only_counts_check(self) -> None:
        m = mc()
        m.record_filter_check(passed=True)
        s = m.filter_snapshot()
        assert s == {
            "checks_total": 1, "hits_total": 0, "hit_rate_percent": 0.0,
            "by_category": {}, "fail_open_total": 0, "recent": [],
        }

    def test_hit_counts_category_and_recent(self) -> None:
        m = mc()
        m.record_filter_check(passed=False, categories=["persona_break"], action="regenerate")
        s = m.filter_snapshot()
        assert s["hits_total"] == 1
        assert s["hit_rate_percent"] == 100.0
        assert s["by_category"] == {"persona_break": 1}
        assert s["recent"] == [{"categories": ["persona_break"], "action": "regenerate"}]

    def test_hit_rate_mixed(self) -> None:
        m = mc()
        for _ in range(9):
            m.record_filter_check(passed=True)
        m.record_filter_check(passed=False, categories=["harmful"], action="block")
        assert m.filter_snapshot()["hit_rate_percent"] == 10.0

    def test_recent_capped_at_10(self) -> None:
        m = mc()
        for i in range(15):
            m.record_filter_check(passed=False, categories=[f"c{i}"], action="regenerate")
        s = m.filter_snapshot()
        assert len(s["recent"]) == 10
        # mới nhất trước
        assert s["recent"][0]["categories"] == ["c14"]

    def test_fail_open_counter(self) -> None:
        m = mc()
        m.record_filter_check(passed=True, fail_open=True)
        assert m.filter_snapshot()["fail_open_total"] == 1

    def test_regeneration_outcome_is_exported(self) -> None:
        m = mc()
        m.record_filter_regeneration("recovered")
        text = m.prometheus_text().decode("utf-8")
        assert 'mai_filter_regen_total{result="recovered"} 1.0' in text

    def test_unknown_regeneration_outcome_is_rejected(self) -> None:
        m = mc()
        with pytest.raises(ValueError, match="unknown filter regeneration outcome"):
            m.record_filter_regeneration("bad")


class TestDashboardFilterSection:
    async def test_snapshot_has_filter_when_metrics_set(self) -> None:
        m = mc()
        m.record_filter_check(passed=False, categories=["persona_break"], action="regenerate")
        server = DashboardServer(metrics=m)
        snap = await server.build_snapshot()
        assert "filter" in snap
        assert snap["filter"]["hits_total"] == 1

    async def test_snapshot_merges_regen_when_provided(self) -> None:
        m = mc()

        class FakeRegen:
            def get_metrics(self):
                return {
                    "filter_regen_attempts_total": 3,
                    "filter_regen_recovered_total": 2,
                    "filter_regen_exhausted_total": 1,
                }

        server = DashboardServer(metrics=m, regenerator=FakeRegen())
        snap = await server.build_snapshot()
        r = snap["filter"]["regen"]
        assert r == {"attempts_total": 3, "recovered_total": 2, "exhausted_total": 1}

    async def test_snapshot_merges_service_fail_open(self) -> None:
        m = mc()

        class FakeFilter:
            def get_metrics(self):
                return {"filter_fail_open_total": 5}

        server = DashboardServer(metrics=m, filter_svc=FakeFilter())
        snap = await server.build_snapshot()
        assert snap["filter"]["service_fail_open_total"] == 5

    async def test_no_metrics_no_section(self) -> None:
        server = DashboardServer()
        snap = await server.build_snapshot()
        assert "filter" not in snap
