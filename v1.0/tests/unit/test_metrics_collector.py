"""Test MetricsCollector (ARCHITECTURE 5.3)."""
from __future__ import annotations

from prometheus_client import CollectorRegistry

from orchestrator.metrics_collector import MetricsCollector


def fresh() -> MetricsCollector:
    return MetricsCollector(registry=CollectorRegistry())


class TestMetricDefinitions:
    def test_all_spec_metrics_exist(self) -> None:
        m = fresh()
        assert m.ttfa_seconds is not None
        assert m.trigger_decisions_total is not None
        assert m.state_transitions_total is not None

    def test_three_fake_metrics_exist(self) -> None:
        """PROCESS 0.F: 3 metric giả."""
        m = fresh()
        assert m.fake_gpu_util is not None
        assert m.fake_vram_mb is not None
        assert m.fake_chat_rate is not None

    def test_separate_registry_no_duplicate_error(self) -> None:
        # tạo nhiều instance không lỗi "Duplicated timeseries"
        for _ in range(3):
            fresh()


class TestRecorders:
    def test_state_transition_counted(self) -> None:
        m = fresh()
        m.record_state_transition("IDLE", "THINKING")
        m.record_state_transition("IDLE", "THINKING")
        text = m.prometheus_text().decode()
        assert 'mai_state_transitions_total{from_state="IDLE",to_state="THINKING"} 2.0' in text

    def test_trigger_decision_counted(self) -> None:
        m = fresh()
        m.record_trigger_decision("chat_normal", "skip")
        text = m.prometheus_text().decode()
        assert 'trigger_type="chat_normal"' in text
        assert 'decision="skip"' in text

    def test_ttfa_observed(self) -> None:
        m = fresh()
        m.observe_ttfa(0.72)
        text = m.prometheus_text().decode()
        assert "mai_pipeline_ttfa_seconds_count 1.0" in text


class TestFakeMetrics:
    def test_tick_updates_values(self) -> None:
        m = fresh()
        snap = m.tick_fake_metrics(t=0.0)
        assert "gpu_util_percent" in snap
        assert "vram_mb" in snap
        assert "chat_rate_per_min" in snap

    def test_values_change_over_time(self) -> None:
        m = fresh()
        a = m.tick_fake_metrics(t=0.0)
        b = m.tick_fake_metrics(t=5.0)
        assert a != b  # DoD: metric giả cập nhật realtime

    def test_gpu_in_reasonable_range(self) -> None:
        m = fresh()
        for t in range(0, 50):
            snap = m.tick_fake_metrics(t=float(t))
            assert 0 <= snap["gpu_util_percent"] <= 100

    def test_chat_rate_non_negative(self) -> None:
        m = fresh()
        for t in range(0, 50):
            assert m.tick_fake_metrics(t=float(t))["chat_rate_per_min"] >= 0

    def test_snapshot_matches_last_tick(self) -> None:
        m = fresh()
        m.tick_fake_metrics(t=3.0)
        snap = m.snapshot()
        assert set(snap) == {"gpu_util_percent", "vram_mb", "chat_rate_per_min"}


class TestPrometheusExport:
    def test_export_is_bytes(self) -> None:
        m = fresh()
        m.tick_fake_metrics(t=1.0)
        assert isinstance(m.prometheus_text(), bytes)

    def test_export_contains_fake_gauges(self) -> None:
        m = fresh()
        m.tick_fake_metrics(t=1.0)
        text = m.prometheus_text().decode()
        assert "mai_fake_gpu_util_percent" in text
        assert "mai_fake_vram_mb" in text
