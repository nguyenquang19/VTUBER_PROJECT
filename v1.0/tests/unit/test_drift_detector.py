"""Test DriftDetector — Phase 7.5.E."""
from __future__ import annotations

import pytest

from interfaces.animation import MoodState
from services.qc.drift_detector import DriftDetector, DriftReport


class TestDetect:
    def test_no_drift_below_threshold(self) -> None:
        d = DriftDetector(threshold=4)
        r = d.detect(MoodState(vui=5, buc=4), MoodState(vui=7, buc=5))
        assert r.max_delta == 2
        assert r.flagged is False

    def test_flagged_above_threshold(self) -> None:
        """Appraisal buc=8 (bị troll rõ) nhưng LLM report buc=0 → lệch 8 → flag."""
        d = DriftDetector(threshold=4)
        r = d.detect(MoodState(buc=8), MoodState(buc=0))
        assert r.max_delta == 8
        assert r.max_dim == "buc"
        assert r.flagged is True

    def test_flagged_at_threshold_boundary(self) -> None:
        """max_delta == threshold không flag (spec: > threshold)."""
        d = DriftDetector(threshold=4)
        r = d.detect(MoodState(vui=8), MoodState(vui=4))
        assert r.max_delta == 4
        assert r.flagged is False

    def test_returns_deltas_all_dims(self) -> None:
        d = DriftDetector(threshold=4)
        r = d.detect(
            MoodState(vui=5, buon=3, buc=4, bon_chon=3, nguong=2),
            MoodState(vui=6, buon=2, buc=8, bon_chon=1, nguong=0),
        )
        assert r.deltas == {"vui": 1, "buon": 1, "buc": 4, "bon_chon": 2, "nguong": 2}
        assert r.max_dim == "buc"

    def test_max_dim_ties_deterministic(self) -> None:
        """Tie → first dim theo iteration order."""
        d = DriftDetector(threshold=10)
        r = d.detect(MoodState(vui=5, buon=5), MoodState(vui=8, buon=8))
        assert r.max_delta == 3
        assert r.max_dim in {"vui", "buon"}


class TestMetrics:
    def test_counters(self) -> None:
        d = DriftDetector(threshold=4)
        d.detect(MoodState(vui=5), MoodState(vui=5))     # no drift
        d.detect(MoodState(buc=9), MoodState(buc=0))     # flagged
        d.detect(MoodState(vui=5), MoodState(vui=6))     # no drift
        m = d.get_metrics()
        assert m["drift_checks_total"] == 3
        assert m["drift_flagged_total"] == 1
        assert m["drift_flagged_rate"] == pytest.approx(1 / 3)
        assert m["drift_last_max_delta"] == 1

    def test_zero_checks_rate_zero(self) -> None:
        d = DriftDetector()
        assert d.get_metrics()["drift_flagged_rate"] == 0.0


class TestFromLoader:
    def test_default_threshold(self, tmp_path) -> None:
        from orchestrator.config_loader import ConfigLoader
        from pathlib import Path
        REPO_ROOT = Path(__file__).resolve().parents[2]
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        d = DriftDetector.from_loader(loader)
        assert d.threshold == 4  # default khi chưa có key trong mood_engine.yaml
