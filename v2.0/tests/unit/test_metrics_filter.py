"""Test filter metrics exposed by the canonical operations collector."""
from __future__ import annotations

import pytest

from services.operations.metrics import MetricsCollector


def mc() -> MetricsCollector:
    return MetricsCollector()


class TestRecordFilterCheck:
    def test_pass_only_counts_check(self) -> None:
        m = mc()
        m.record_filter_check(passed=True)
        assert m.filter_snapshot() == {
            "checks_total": 1,
            "hits_total": 0,
            "hit_rate_percent": 0.0,
            "by_category": {},
            "fail_open_total": 0,
            "recent": [],
        }

    def test_hit_counts_category_and_recent(self) -> None:
        m = mc()
        m.record_filter_check(
            passed=False, categories=["persona_break"], action="regenerate",
        )
        snapshot = m.filter_snapshot()
        assert snapshot["hits_total"] == 1
        assert snapshot["hit_rate_percent"] == 100.0
        assert snapshot["by_category"] == {"persona_break": 1}
        assert snapshot["recent"] == [{
            "categories": ["persona_break"], "action": "regenerate",
        }]

    def test_hit_rate_mixed(self) -> None:
        m = mc()
        for _ in range(9):
            m.record_filter_check(passed=True)
        m.record_filter_check(passed=False, categories=["harmful"], action="block")
        assert m.filter_snapshot()["hit_rate_percent"] == 10.0

    def test_recent_capped_at_10(self) -> None:
        m = mc()
        for index in range(15):
            m.record_filter_check(
                passed=False, categories=[f"c{index}"], action="regenerate",
            )
        snapshot = m.filter_snapshot()
        assert len(snapshot["recent"]) == 10
        assert snapshot["recent"][0]["categories"] == ["c14"]

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
