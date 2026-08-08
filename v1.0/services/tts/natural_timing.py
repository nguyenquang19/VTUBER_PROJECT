"""TTFA-calibrated pacing policy; chat replies never receive filler (M5.4)."""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NaturalTimingConfig:
    min_ttfa_samples: int = 3
    sample_window: int = 20
    ttfa_ceiling_ms: float = 1000.0
    proactive_filler_only: bool = True
    proactive_prefixes: tuple[str, ...] = ("self_", "room_", "trans_")

    @classmethod
    def from_loader(cls, loader: Any) -> "NaturalTimingConfig":
        prefix = "natural_timing."
        value = cls(
            min_ttfa_samples=int(loader.get("pacing", prefix + "min_ttfa_samples", 3)),
            sample_window=int(loader.get("pacing", prefix + "sample_window", 20)),
            ttfa_ceiling_ms=float(loader.get("pacing", prefix + "ttfa_ceiling_ms", 1000)),
            proactive_filler_only=bool(
                loader.get("pacing", prefix + "proactive_filler_only", True)
            ),
            proactive_prefixes=tuple(
                str(item) for item in loader.get(
                    "pacing", prefix + "proactive_prefixes", ["self_", "room_", "trans_"],
                )
            ),
        )
        if min(value.min_ttfa_samples, value.sample_window, value.ttfa_ceiling_ms) <= 0:
            raise ValueError("natural timing limits must be positive")
        if value.min_ttfa_samples > value.sample_window:
            raise ValueError("natural timing min samples cannot exceed window")
        return value


@dataclass(frozen=True)
class TimingPlan:
    delay_seconds: float
    allow_filler: bool
    turn_kind: str
    reason: str


class NaturalTimingPolicy:
    def __init__(
        self, config: NaturalTimingConfig, *, metrics: Any = None, enabled: bool = True,
    ) -> None:
        self.config = config
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._samples: deque[float] = deque(maxlen=config.sample_window)

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None, enabled: bool | None = None,
    ) -> "NaturalTimingPolicy":
        configured = bool(loader.get("pacing", "natural_timing.enabled", True))
        return cls(
            NaturalTimingConfig.from_loader(loader), metrics=metrics,
            enabled=configured if enabled is None else enabled,
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def observe_ttfa(self, ttfa_ms: float | None) -> bool:
        if ttfa_ms is None or float(ttfa_ms) <= 0:
            return False
        value = float(ttfa_ms)
        self._samples.append(value)
        if self._metrics is not None and hasattr(self._metrics, "observe_natural_timing_ttfa"):
            try:
                self._metrics.observe_natural_timing_ttfa(value)
            except Exception:
                pass
        return True

    def plan(self, request_id: str, text: str, pacer: Any) -> TimingPlan:
        turn_kind = self.turn_kind(request_id)
        if not self.enabled:
            return self._plan(0.0, False, turn_kind, "disabled")
        if len(self._samples) < self.config.min_ttfa_samples:
            return self._plan(0.0, False, turn_kind, "awaiting_real_ttfa")
        median = statistics.median(self._samples)
        if median > self.config.ttfa_ceiling_ms:
            return self._plan(0.0, False, turn_kind, "ttfa_above_ceiling")
        delay = max(0.0, float(pacer.delay(text)))
        allow_filler = turn_kind == "proactive" if self.config.proactive_filler_only else True
        return self._plan(delay, allow_filler, turn_kind, "calibrated")

    def turn_kind(self, request_id: str) -> str:
        value = str(request_id or "")
        return (
            "proactive"
            if any(value.startswith(prefix) for prefix in self.config.proactive_prefixes)
            else "chat"
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ttfa_samples": len(self._samples),
            "ttfa_median_ms": (
                round(statistics.median(self._samples), 1) if self._samples else None
            ),
            "ready": len(self._samples) >= self.config.min_ttfa_samples,
        }

    def _plan(
        self, delay: float, filler: bool, turn_kind: str, reason: str,
    ) -> TimingPlan:
        if self._metrics is not None and hasattr(self._metrics, "record_natural_timing"):
            try:
                self._metrics.record_natural_timing(turn_kind, reason)
            except Exception:
                pass
        return TimingPlan(delay, filler, turn_kind, reason)
