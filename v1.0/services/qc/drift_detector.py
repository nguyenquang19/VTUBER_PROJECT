"""DriftDetector — log lệch mood engine (appraisal) vs LLM self-report (Phase 7.5.E).

Spec Mục 7: độ lệch lớn giữa 2 kênh = dấu hiệu persona hiểu sai ngữ cảnh.
Log để review Phase 8 (fine-tune data), KHÔNG âm thầm bỏ 1 bên.

Ví dụ bắt được: appraisal `buc→8` (bị troll) nhưng LLM report `vui:8` → lệch 8+ → flag.
Threshold mặc định 4 (nửa thang 10) — tune theo log thật.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from interfaces.animation import MoodState
from orchestrator.logger import get_logger
from orchestrator.mood_engine import DIMENSIONS


@dataclass
class DriftReport:
    deltas: dict[str, int]     # abs diff per dimension
    max_delta: int
    max_dim: str               # dim có delta lớn nhất
    flagged: bool              # max_delta > threshold


class DriftDetector:
    def __init__(self, threshold: int = 4) -> None:
        self.threshold = int(threshold)
        self._log = get_logger("drift_detector")
        self._checks_total = 0
        self._flagged_total = 0
        self._last_report: DriftReport | None = None

    @classmethod
    def from_loader(cls, loader) -> "DriftDetector":
        return cls(
            threshold=int(loader.get("mood_engine", "drift.threshold", 4)),
        )

    def detect(self, engine_mood: MoodState, llm_mood: MoodState) -> DriftReport:
        deltas: dict[str, int] = {}
        for d in DIMENSIONS:
            e = int(getattr(engine_mood, d))
            l = int(getattr(llm_mood, d))
            deltas[d] = abs(e - l)
        max_dim = max(deltas, key=lambda k: deltas[k])
        max_delta = deltas[max_dim]
        flagged = max_delta > self.threshold
        self._checks_total += 1
        if flagged:
            self._flagged_total += 1
            self._log.warning(
                "mood_drift_flagged",
                max_dim=max_dim, max_delta=max_delta,
                deltas=deltas,
                engine=engine_mood.model_dump(),
                llm=llm_mood.model_dump(),
            )
        report = DriftReport(deltas=deltas, max_delta=max_delta,
                             max_dim=max_dim, flagged=flagged)
        self._last_report = report
        return report

    def get_metrics(self) -> dict[str, Any]:
        return {
            "drift_checks_total": self._checks_total,
            "drift_flagged_total": self._flagged_total,
            "drift_flagged_rate": (
                self._flagged_total / self._checks_total
                if self._checks_total else 0.0
            ),
            "drift_last_max_delta": self._last_report.max_delta if self._last_report else None,
        }
