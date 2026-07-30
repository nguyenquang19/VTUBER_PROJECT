"""Metrics collector: prometheus_client (ARCHITECTURE 5.3, Phase 0 task 10).

Phase 0 scope: định nghĩa metric objects thật (5.3) + vài "metric giả" tự cập
nhật để dashboard có gì hiển thị realtime trước khi có LLM/TTS thật.

Dùng CollectorRegistry riêng (không phải global REGISTRY) để test tạo nhiều
instance không bị "Duplicated timeseries".
"""
from __future__ import annotations

import math
import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class MetricsCollector:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        # --- Metric thật theo spec 5.3 ---
        self.ttfa_seconds = Histogram(
            "mai_pipeline_ttfa_seconds",
            "Time to first audio playback",
            buckets=[0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0],
            registry=self.registry,
        )
        self.trigger_decisions_total = Counter(
            "mai_trigger_decisions_total",
            "Trigger manager decisions",
            ["trigger_type", "decision"],
            registry=self.registry,
        )
        self.state_transitions_total = Counter(
            "mai_state_transitions_total",
            "State machine transitions",
            ["from_state", "to_state"],
            registry=self.registry,
        )

        # --- 3 "metric giả" cho Phase 0 (chưa có service thật) ---
        # DoD: "Metric giả cập nhật realtime trên chart"
        self.fake_gpu_util = Gauge(
            "mai_fake_gpu_util_percent", "Fake GPU utilization (Phase 0 demo)",
            registry=self.registry,
        )
        self.fake_vram_mb = Gauge(
            "mai_fake_vram_mb", "Fake VRAM usage (Phase 0 demo)",
            registry=self.registry,
        )
        self.fake_chat_rate = Gauge(
            "mai_fake_chat_rate_per_min", "Fake chat rate (Phase 0 demo)",
            registry=self.registry,
        )

        self._start = time.perf_counter()

    # ---------- recorders (service thật gọi ở phase sau) ----------

    def record_state_transition(self, from_state: str, to_state: str) -> None:
        self.state_transitions_total.labels(from_state=from_state, to_state=to_state).inc()

    def record_trigger_decision(self, trigger_type: str, decision: str) -> None:
        self.trigger_decisions_total.labels(trigger_type=trigger_type, decision=decision).inc()

    def observe_ttfa(self, seconds: float) -> None:
        self.ttfa_seconds.observe(seconds)

    # ---------- fake updater (Phase 0 demo) ----------

    def tick_fake_metrics(self, t: float | None = None) -> dict[str, float]:
        """Cập nhật 3 metric giả bằng sóng sin lệch pha → chart có chuyển động.

        Trả snapshot để dashboard push qua WebSocket.
        """
        t = t if t is not None else (time.perf_counter() - self._start)
        gpu = 50 + 40 * math.sin(t / 3)
        vram = 9800 + 300 * math.sin(t / 5 + 1)
        chat = max(0.0, 30 + 25 * math.sin(t / 7 + 2))
        self.fake_gpu_util.set(gpu)
        self.fake_vram_mb.set(vram)
        self.fake_chat_rate.set(chat)
        return {
            "gpu_util_percent": round(gpu, 1),
            "vram_mb": round(vram, 1),
            "chat_rate_per_min": round(chat, 1),
        }

    # ---------- export ----------

    def snapshot(self) -> dict[str, Any]:
        """Giá trị hiện tại của các metric giả (cho dashboard)."""
        return {
            "gpu_util_percent": round(self._gauge_value(self.fake_gpu_util), 1),
            "vram_mb": round(self._gauge_value(self.fake_vram_mb), 1),
            "chat_rate_per_min": round(self._gauge_value(self.fake_chat_rate), 1),
        }

    @staticmethod
    def _gauge_value(gauge: Gauge) -> float:
        # prometheus_client Gauge: đọc value hiện tại
        return gauge._value.get()  # type: ignore[attr-defined]

    def prometheus_text(self) -> bytes:
        """Export Prometheus exposition format (cho /metrics endpoint)."""
        return generate_latest(self.registry)
