"""Test MoodEngine — Phase 7.5.A (spec EMOTION_SIMULATION Mục 5)."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from interfaces.animation import MoodState
from orchestrator.mood_engine import DIMENSIONS, MoodEngine, MoodEngineError

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    """Deterministic clock để test decay theo elapsed time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(clock: FakeClock | None = None, **over) -> MoodEngine:
    kw: dict = dict(
        tick_hz=10, stiffness=0.30, damping=0.75,
        target_decay_rate=0.15, llm_hint_weight=0.20,
        baseline={"vui": 5, "buon": 3, "buc": 4, "bon_chon": 3, "nguong": 2},
        clock=clock,
    )
    kw.update(over)
    return MoodEngine(**kw)


class TestInit:
    def test_defaults_from_baseline(self) -> None:
        e = make()
        assert e.pos == e.baseline
        assert e.target == e.baseline
        assert all(v == 0.0 for v in e.vel.values())

    def test_missing_dim_in_baseline_defaults_5(self) -> None:
        e = make(baseline={"vui": 8})
        for d in DIMENSIONS:
            assert d in e.baseline


class TestApplyAppraisal:
    def test_sets_target(self) -> None:
        clock = FakeClock()
        e = make(clock=clock)
        e.apply_appraisal({"buc": 8, "vui": 6})
        assert e.target["buc"] == 8
        assert e.target["vui"] == 6
        # last_set_ts updated
        assert e.last_set_ts["buc"] == clock.now

    def test_clamp_0_10(self) -> None:
        e = make()
        e.apply_appraisal({"buc": 15, "vui": -5})
        assert e.target["buc"] == 10
        assert e.target["vui"] == 0

    def test_ignores_unknown_dim(self) -> None:
        e = make()
        e.apply_appraisal({"random_dim": 5, "vui": 7})
        assert e.target["vui"] == 7


class TestSaturation:
    def test_single_event_uses_as_is(self) -> None:
        e = make()
        out = e.saturate({"vui": [7]})
        assert out["vui"] == 7

    def test_multi_events_max_plus_bonus(self) -> None:
        e = make(saturation_bonus=0.5)
        # 3 sự kiện: 6, 7, 5 → max=7 + 0.5×2 = 8
        out = e.saturate({"vui": [6, 7, 5]})
        assert out["vui"] == 8

    def test_saturation_caps_at_10(self) -> None:
        e = make(saturation_bonus=0.5)
        # 10 sự kiện donation vui=9 → 9 + 0.5×9 = 13.5, cap 10
        out = e.saturate({"vui": [9] * 10})
        assert out["vui"] == 10

    def test_100_events_no_overshoot(self) -> None:
        """DoD Phase 7.5: saturation 100 event → không overshoot >10, không kẹt."""
        e = make(saturation_bonus=0.5)
        out = e.saturate({"buc": [8] * 100})
        assert 0 <= out["buc"] <= 10


class TestApplyLlmHint:
    def test_nudge_toward_hint(self) -> None:
        e = make(llm_hint_weight=0.5)  # 50% khoảng cách
        # target ban đầu = baseline (buc=4), LLM report buc=10 → +50% (10−4)=+3 → 7
        e.apply_llm_hint(MoodState(buc=10))
        assert e.target["buc"] == pytest.approx(7.0)

    def test_hint_weight_low_small_change(self) -> None:
        e = make(llm_hint_weight=0.20)
        # buc=4 → nudge 20% về 10 → +1.2 → 5.2
        e.apply_llm_hint(MoodState(buc=10))
        assert e.target["buc"] == pytest.approx(5.2)


class TestTick:
    def test_pos_moves_toward_target(self) -> None:
        e = make()
        e.apply_appraisal({"buc": 9})
        # position ban đầu = baseline (buc=4). Sau 1 tick, position phải nhích lên
        m = e.tick(dt=0.1)
        assert e.pos["buc"] > 4.0
        assert e.pos["buc"] < 9.0  # chưa tới target (spring)

    def test_tick_returns_moodstate_int(self) -> None:
        e = make()
        m = e.tick(dt=0.1)
        assert isinstance(m, MoodState)
        assert isinstance(m.vui, int)

    def test_float_position_changes_before_rounded_mood_changes(self) -> None:
        """Dashboard must render `pos`, otherwise smooth movement looks frozen."""
        e = make()
        e.apply_appraisal({"buc": 9})
        rounded_before = e.current_state().buc
        float_before = e.snapshot()["pos"]["buc"]
        e.tick(dt=0.1)
        assert e.snapshot()["pos"]["buc"] > float_before
        assert e.current_state().buc == rounded_before

    def test_negative_dt_raises(self) -> None:
        e = make()
        with pytest.raises(MoodEngineError):
            e.tick(dt=-0.1)

    def test_dt_none_uses_default(self) -> None:
        e = make(tick_hz=10)
        # Không raise, dùng 1/tick_hz = 0.1
        m = e.tick()
        assert isinstance(m, MoodState)


class TestStability10k:
    """DoD Phase 7.5: 10k tick không NaN/dao động."""

    def test_10k_ticks_no_nan(self) -> None:
        clock = FakeClock()
        e = make(clock=clock)
        e.apply_appraisal({"buc": 9})
        for _ in range(10_000):
            m = e.tick(dt=0.1)
            clock.advance(0.1)
            for d in DIMENSIONS:
                assert math.isfinite(e.pos[d]), f"pos[{d}] NaN/inf after tick"
                assert math.isfinite(e.vel[d]), f"vel[{d}] NaN/inf after tick"
                assert 0.0 <= e.pos[d] <= 10.0

    def test_no_oscillation_at_default_config(self) -> None:
        """Over-damped nhẹ: position không đảo dấu velocity nhiều lần liên tục."""
        e = make()
        e.apply_appraisal({"buc": 9})
        prev_vel_sign = 0
        sign_changes = 0
        for _ in range(200):
            e.tick(dt=0.1)
            cur_sign = _sign(e.vel["buc"])
            if cur_sign != 0 and prev_vel_sign != 0 and cur_sign != prev_vel_sign:
                sign_changes += 1
            prev_vel_sign = cur_sign
        # Over-damped → ≤2 sign changes (một lần đổi khi qua đích + noise)
        assert sign_changes <= 2, f"dao động: {sign_changes} sign changes"


class TestTargetDecay:
    """DoD: target decay về baseline sau sự kiện."""

    def test_target_decays_to_baseline_over_time(self) -> None:
        clock = FakeClock()
        e = make(clock=clock, target_decay_rate=1.0)  # decay nhanh cho test
        e.apply_appraisal({"buc": 10})
        assert e.target["buc"] == 10
        # 10 giây trôi qua, decay×elapsed = 10.0 (clamp 1.0) → target đến baseline
        clock.advance(10.0)
        e.tick(dt=0.1)
        assert e.target["buc"] == pytest.approx(e.baseline["buc"], abs=0.1)

    def test_pos_returns_to_baseline_after_long_idle(self) -> None:
        """Sau nhiều tick không có sự kiện, position tự về baseline."""
        clock = FakeClock()
        e = make(clock=clock)
        e.apply_appraisal({"buc": 10})
        # Tick 200 lần (20s) với clock advance thật
        for _ in range(200):
            e.tick(dt=0.1)
            clock.advance(0.1)
        # buc phải quay gần baseline (4), không kẹt ở 10
        assert abs(e.pos["buc"] - e.baseline["buc"]) < 1.0


class TestFromLoader:
    def test_reads_config_yaml(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        e = MoodEngine.from_loader(loader)
        assert e.tick_hz == 10
        assert e.stiffness == pytest.approx(0.30)
        assert e.damping == pytest.approx(0.75)
        assert e.baseline["buc"] == 4  # Mai ngang sẵn


class TestMetrics:
    def test_counters(self) -> None:
        e = make()
        e.apply_appraisal({"buc": 8})
        e.apply_llm_hint(MoodState(vui=5))
        e.tick(dt=0.1)
        e.tick(dt=0.1)
        m = e.get_metrics()
        assert m["mood_appraisal_applies"] == 1
        assert m["mood_llm_applies"] == 1
        assert m["mood_ticks"] == 2


def _sign(x: float) -> int:
    if x > 1e-6: return 1
    if x < -1e-6: return -1
    return 0
