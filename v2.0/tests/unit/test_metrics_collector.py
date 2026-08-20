"""Test MetricsCollector (ARCHITECTURE 5.3)."""
from __future__ import annotations

from prometheus_client import CollectorRegistry

from orchestrator.metrics_collector import MetricsCollector


def fresh(gpu_query_runner=None) -> MetricsCollector:
    return MetricsCollector(
        registry=CollectorRegistry(), gpu_query_runner=gpu_query_runner,
    )


class TestMetricDefinitions:
    def test_all_spec_metrics_exist(self) -> None:
        m = fresh()
        assert m.ttfa_seconds is not None
        assert m.trigger_decisions_total is not None
        assert m.state_transitions_total is not None

    def test_real_gpu_metrics_exist(self) -> None:
        m = fresh()
        assert m.gpu_util is not None
        assert m.vram_used_mb is not None
        assert m.vram_total_mb is not None
        assert m.gpu_metrics_available is not None

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

    def test_phase14_human_review_and_trajectory_outcomes_are_observable(self) -> None:
        m = fresh()
        m.record_human_like_review("finalized")
        m.record_trajectory("replay_match")
        assert m.human_like_review_snapshot() == {"finalized": 1}
        assert m.trajectory_snapshot() == {"replay_match": 1}
        text = m.prometheus_text().decode()
        assert 'mai_human_like_reviews_total{outcome="finalized"} 1.0' in text
        assert 'mai_trajectory_records_total{outcome="replay_match"} 1.0' in text

    def test_phase15_closed_loop_canary_outcomes_are_observable(self) -> None:
        m = fresh()
        m.record_closed_loop_canary("passed")
        assert m.closed_loop_canary_snapshot() == {"passed": 1}
        assert 'mai_closed_loop_canary_total{outcome="passed"} 1.0' in (
            m.prometheus_text().decode()
        )


class TestGpuMetrics:
    def test_nvidia_csv_updates_real_snapshot(self) -> None:
        m = fresh(lambda _command, _timeout: "17, 2048, 16384\n")
        snap = m.sample_gpu_metrics(refresh_s=0.0)
        assert snap["gpu_util_percent"] == 17.0
        assert snap["vram_mb"] == 2048.0
        assert snap["vram_total_mb"] == 16384.0
        assert snap["gpu_metrics_available"] is True
        assert snap["gpu_metrics_stale"] is False
        assert snap["source"] == "nvidia-smi"

    def test_failed_query_never_invents_gpu_values(self) -> None:
        def fail(_command: str, _timeout: float) -> str:
            raise FileNotFoundError("nvidia-smi")

        snap = fresh(fail).sample_gpu_metrics(refresh_s=0.0)
        assert snap["gpu_util_percent"] is None
        assert snap["vram_mb"] is None
        assert snap["gpu_metrics_available"] is False
        assert snap["gpu_metrics_stale"] is False
        assert "FileNotFoundError" in snap["gpu_metrics_error"]

    def test_failed_refresh_marks_last_real_sample_stale(self) -> None:
        outputs = iter(("8, 1024, 16384", RuntimeError("driver unavailable")))

        def query(_command: str, _timeout: float) -> str:
            value = next(outputs)
            if isinstance(value, Exception):
                raise value
            return value

        m = fresh(query)
        assert m.sample_gpu_metrics(refresh_s=0.0)["gpu_metrics_available"] is True
        stale = m.sample_gpu_metrics(refresh_s=0.0)
        assert stale["gpu_util_percent"] == 8.0
        assert stale["gpu_metrics_available"] is False
        assert stale["gpu_metrics_stale"] is True

    def test_invalid_values_are_rejected_without_fake_fallback(self) -> None:
        m = fresh(lambda _command, _timeout: "140, 10, 20")
        snap = m.sample_gpu_metrics(refresh_s=0.0)
        assert snap["gpu_util_percent"] is None
        assert snap["gpu_metrics_available"] is False


class TestPrometheusExport:
    def test_export_is_bytes(self) -> None:
        m = fresh()
        assert isinstance(m.prometheus_text(), bytes)

    def test_export_contains_real_gpu_gauges(self) -> None:
        m = fresh(lambda _command, _timeout: "20, 3000, 16000")
        m.sample_gpu_metrics(refresh_s=0.0)
        text = m.prometheus_text().decode()
        assert "mai_gpu_util_percent 20.0" in text
        assert "mai_vram_used_mb 3000.0" in text
        assert "mai_gpu_metrics_available 1.0" in text
