"""Test UrgeAccumulator + CategorySelector — Autonomy.A.

Cover 5 DoD spec Mục 4 ở tầng core (unit):
  * variance (tick nhiều lần → urge biến thiên)
  * self-cooldown (after speak, urge=0 trong window)
  * mood coupling (bon_chon cao → time_to_speak ngắn hơn)
  * nag decay (5 lần liên tiếp không external → prob giảm)
  * no-repeat category

Test integration end-to-end DoD (variance qua 4h giả lập, no-repeat 20 lần)
để Aut.E.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from interfaces.animation import MoodState
from orchestrator.autonomy_engine import (
    AutonomyConfig,
    CategoryConfig,
    CategorySelector,
    UrgeAccumulator,
    UrgeConfig,
    _weighted_choice,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
    def __call__(self) -> float: return self.now
    def advance(self, s: float) -> None: self.now += s


def urge_cfg(**over) -> UrgeConfig:
    kw = dict(
        rise_base=0.5, rise_max_per_tick=12.0,
        urge_floor=30.0, urge_noise_std=0.0,  # noise=0 cho test deterministic
        decay_after_speak=8.0, self_cooldown_seconds=45,
        bon_chon_weight=0.6, buon_dampen=0.4, nguong_dampen=0.5,
        prob_scale=40.0, prob_max=0.85,
    )
    kw.update(over)
    return UrgeConfig(**kw)


def cats_cfg(**over) -> AutonomyConfig:
    categories = {
        "complain_silence": CategoryConfig(
            name="complain_silence", weight=1.0, cooldown_seconds=300,
            mood_boost={"bon_chon": 1.5}, prompt_hint="hint1",
        ),
        "share_thought": CategoryConfig(
            name="share_thought", weight=1.2, cooldown_seconds=240,
            mood_boost={"vui": 1.2}, prompt_hint="hint2",
        ),
        "ask_chat": CategoryConfig(
            name="ask_chat", weight=1.0, cooldown_seconds=200,
            mood_boost={}, prompt_hint="hint3",
        ),
    }
    kw = dict(
        tick_seconds=5.0, urge=urge_cfg(),
        no_repeat_window=2, categories=categories,
    )
    kw.update(over)
    return AutonomyConfig(**kw)


# ═══════════════════════ UrgeAccumulator ═══════════════════════


class TestUrgeInit:
    def test_start_at_zero(self) -> None:
        u = UrgeAccumulator(urge_cfg(), clock=FakeClock())
        assert u.urge == 0.0
        assert u.consecutive_ignored == 0


class TestUrgeRises:
    def test_urge_grows_with_silence(self) -> None:
        clock = FakeClock()
        u = UrgeAccumulator(urge_cfg(), clock=clock)
        # Silence 30s → tick → urge tăng
        clock.advance(30.0)
        u.tick(MoodState(vui=5, buc=4, bon_chon=3))
        assert u.urge > 0

    def test_multiple_ticks_accumulate(self) -> None:
        clock = FakeClock()
        u = UrgeAccumulator(urge_cfg(), clock=clock)
        for _ in range(5):
            clock.advance(5.0)
            u.tick(MoodState(bon_chon=3))
        assert u.urge > 5  # đã leo lên đáng kể


class TestSelfCooldown:
    def test_urge_zero_after_speak(self) -> None:
        clock = FakeClock()
        u = UrgeAccumulator(urge_cfg(), clock=clock)
        u.urge = 80.0
        u.on_self_spoke()
        assert u.urge == 0.0

    def test_external_activity_resets_urge(self) -> None:
        # FIX: user gõ → urge về 0 (Mai không tự nói đè lên khi đang chat)
        clock = FakeClock()
        u = UrgeAccumulator(urge_cfg(), clock=clock)
        u.urge = 70.0
        u.on_external_activity()
        assert u.urge == 0.0
        assert not u.should_speak_now()

    def test_urge_stays_low_during_cooldown(self) -> None:
        """DoD 3: sau on_self_spoke, urge không tăng trong self_cooldown_seconds."""
        clock = FakeClock()
        u = UrgeAccumulator(urge_cfg(self_cooldown_seconds=45), clock=clock)
        u.on_self_spoke()
        # 10 tick (mỗi 3s = 30s tổng) trong cooldown 45s
        for _ in range(10):
            clock.advance(3.0)
            u.tick(MoodState(bon_chon=10))  # dù bon_chon max, vẫn không tăng
        assert u.urge == 0.0
        assert not u.should_speak_now()

    def test_urge_can_rise_after_cooldown_expires(self) -> None:
        clock = FakeClock()
        u = UrgeAccumulator(urge_cfg(self_cooldown_seconds=10), clock=clock)
        u.on_self_spoke()
        # Vượt cooldown → tick có tác dụng lại
        clock.advance(15.0)
        u.tick(MoodState(bon_chon=5))
        assert u.urge > 0


class TestMoodCoupling:
    def test_bon_chon_speeds_up_urge(self) -> None:
        """DoD 4: bon_chon=9 vs bon_chon=1 → time-to-speak ngắn hơn."""
        rng_a = random.Random(42)
        rng_b = random.Random(42)  # cùng seed → so sánh fair
        clock_a = FakeClock()
        clock_b = FakeClock()
        # Rise chậm để không bão hoà cả 2 → thấy rõ chênh
        u_high = UrgeAccumulator(urge_cfg(rise_base=0.05, rise_max_per_tick=2.0),
                                 clock=clock_a, rng=rng_a)
        u_low = UrgeAccumulator(urge_cfg(rise_base=0.05, rise_max_per_tick=2.0),
                                clock=clock_b, rng=rng_b)

        # Cùng 5 tick, mỗi tick 5s, mood khác nhau
        for _ in range(5):
            clock_a.advance(5.0); clock_b.advance(5.0)
            u_high.tick(MoodState(bon_chon=9))
            u_low.tick(MoodState(bon_chon=1))
        assert u_high.urge > u_low.urge, (
            f"bon_chon HIGH urge {u_high.urge} phải > LOW urge {u_low.urge}"
        )

    def test_buon_dampens_urge(self) -> None:
        clock_a = FakeClock(); clock_b = FakeClock()
        u_sad = UrgeAccumulator(urge_cfg(urge_noise_std=0), clock=clock_a)
        u_neutral = UrgeAccumulator(urge_cfg(urge_noise_std=0), clock=clock_b)
        for _ in range(10):
            clock_a.advance(5.0); clock_b.advance(5.0)
            u_sad.tick(MoodState(buon=9))
            u_neutral.tick(MoodState(buon=0))
        assert u_sad.urge < u_neutral.urge


class TestNagDecay:
    def test_consecutive_ignored_lowers_urge_growth(self) -> None:
        """DoD 5: nói ambient liên tiếp không phản hồi → nag_penalty giảm urge growth."""
        clock_a = FakeClock(); clock_b = FakeClock()
        u_ignored = UrgeAccumulator(urge_cfg(urge_noise_std=0), clock=clock_a)
        u_fresh = UrgeAccumulator(urge_cfg(urge_noise_std=0), clock=clock_b)

        # ignored đã tự nói 5 lần liên tiếp không được reply
        u_ignored.consecutive_ignored = 5
        # cả 2 cùng silence 30s
        clock_a.advance(30.0); clock_b.advance(30.0)
        u_ignored.tick(MoodState(bon_chon=5))
        u_fresh.tick(MoodState(bon_chon=5))
        assert u_ignored.urge < u_fresh.urge

    def test_external_activity_resets_nag(self) -> None:
        clock = FakeClock()
        u = UrgeAccumulator(urge_cfg(), clock=clock)
        u.consecutive_ignored = 3
        u.on_external_activity()
        assert u.consecutive_ignored == 0


class TestShouldSpeakNow:
    def test_below_floor_never_speaks(self) -> None:
        u = UrgeAccumulator(urge_cfg(urge_floor=30.0), clock=FakeClock())
        u.urge = 20.0
        assert not u.should_speak_now()

    def test_above_prob_max_capped(self) -> None:
        """urge cực cao → prob = prob_max, không phải 1.0."""
        u = UrgeAccumulator(urge_cfg(urge_floor=30, prob_scale=40, prob_max=0.85))
        u.urge = 100.0
        # 200 samples → speaks tỉ lệ ~85%, không phải 100%
        rng = random.Random(0)
        u._rng = rng
        hits = sum(1 for _ in range(200) if u.should_speak_now())
        assert hits < 200
        assert hits > 100  # xa 100% nhưng cao


class TestSnapshot:
    def test_snapshot_shape(self) -> None:
        u = UrgeAccumulator(urge_cfg(), clock=FakeClock())
        s = u.snapshot()
        assert set(s.keys()) == {
            "urge", "silence_seconds", "self_cooldown_remaining",
            "consecutive_ignored", "ticks", "speak_decisions",
        }


# ═══════════════════════ CategorySelector ═══════════════════════


class TestSelectorBasic:
    def test_returns_none_when_all_on_cooldown(self) -> None:
        cfg = cats_cfg()
        clock = FakeClock()
        s = CategorySelector(cfg, clock=clock)
        for name in cfg.categories:
            s._last_used_ts[name] = clock()
        # tất cả cooldown còn 200-300s
        assert s.select(MoodState()) is None

    def test_returns_valid_category(self) -> None:
        cfg = cats_cfg()
        s = CategorySelector(cfg, clock=FakeClock(), rng=random.Random(0))
        cat = s.select(MoodState())
        assert cat in cfg.categories


class TestNoRepeat:
    def test_recent_category_excluded(self) -> None:
        """DoD: no_repeat_window → không chọn lại category vừa dùng."""
        cfg = cats_cfg(no_repeat_window=1)
        s = CategorySelector(cfg, clock=FakeClock(), rng=random.Random(0))
        s.mark_used("share_thought")
        # 10 lần chọn → không lần nào là share_thought
        for _ in range(10):
            cat = s.select(MoodState())
            assert cat != "share_thought"


class TestCategoryCooldown:
    def test_used_category_on_cooldown(self) -> None:
        cfg = cats_cfg(no_repeat_window=1)   # deque 1 → 1 mark_used khác đủ evict
        clock = FakeClock()
        s = CategorySelector(cfg, clock=clock, rng=random.Random(0))
        s.mark_used("ask_chat")
        # Chưa hết cooldown (200s) → không chọn ask_chat
        clock.advance(100.0)
        for _ in range(20):
            assert s.select(MoodState()) != "ask_chat"
        # Đẩy ask_chat rời recent deque (1 mark khác)
        s.mark_used("share_thought")
        # Sau cooldown 200s → có thể chọn lại
        clock.advance(150.0)  # 100+150 = 250 > 200
        found = False
        for _ in range(50):
            if s.select(MoodState()) == "ask_chat":
                found = True
                break
        assert found


class TestMoodBoost:
    def test_bon_chon_high_favours_complain_silence(self) -> None:
        """complain_silence có mood_boost bon_chon 1.5 → bon_chon cao được ưu tiên."""
        cfg = cats_cfg()
        rng = random.Random(0)
        s = CategorySelector(cfg, clock=FakeClock(), rng=rng)
        counts: dict[str, int] = {n: 0 for n in cfg.categories}
        for _ in range(200):
            cat = s.select(MoodState(bon_chon=10))
            if cat:
                counts[cat] += 1
        # complain_silence weight = 1.0 * (1 + 1*0.5) = 1.5 → cao nhất
        assert counts["complain_silence"] > counts["ask_chat"]


class TestFromLoader:
    def test_reads_real_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        cfg = AutonomyConfig.from_loader(loader)
        assert cfg.tick_seconds == 5.0
        assert cfg.urge.urge_floor == 30.0
        assert "complain_silence" in cfg.categories
        assert cfg.categories["complain_silence"].cooldown_seconds == 300
        assert cfg.categories["call_operator"].mood_boost["bon_chon"] == 1.3


class TestWeightedChoice:
    def test_deterministic_with_seed(self) -> None:
        rng = random.Random(42)
        items = [("a", 1.0), ("b", 1.0), ("c", 1.0)]
        chosen = [_weighted_choice(items, rng) for _ in range(20)]
        assert set(chosen).issubset({"a", "b", "c"})

    def test_weight_zero_fallback(self) -> None:
        rng = random.Random(0)
        assert _weighted_choice([("only", 0.0)], rng) == "only"
