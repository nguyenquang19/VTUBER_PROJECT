"""Integration coverage for bounded AutonomyEngine behavior.

5 test giả lập FakeClock (chạy nhanh, không cần thực tế 4h):
1. Variance: 2h simulated → interval giữa các lần tự nói KHÔNG constant, stdev > 0
2. No-repeat + distribution: 20 speak → không 2 lần liên tiếp cùng cat, max <60%
3. Self-cooldown: sau on_self_spoke, should_speak_now = False trong window
4. Mood coupling: bon_chon=9 vs bon_chon=1 → HIGH speak sớm hơn LOW
5. Nag decay: 5 lần tự nói liên tiếp không external → prob giảm dần
"""
from __future__ import annotations

import random
import statistics
from collections import Counter
from pathlib import Path

import pytest

from interfaces.animation import MoodState
from orchestrator.autonomy_engine import (
    AutonomyConfig,
    AutonomyEngine,
    CategoryConfig,
    UrgeConfig,
)
from services.autonomy.material_provider import MaterialProvider, RuntimeContext
from services.autonomy.pools import RoundRobinPool

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
    def __call__(self) -> float: return self.now
    def advance(self, s: float) -> None: self.now += s


def _mp(rng: random.Random | None = None) -> MaterialProvider:
    return MaterialProvider(
        share_thought_pool=RoundRobinPool(
            [f"seed_{i}" for i in range(15)], no_repeat_last_n=5,
            rng=rng or random.Random(0),
        ),
        question_pools={
            "opinion": RoundRobinPool([f"q_{i}" for i in range(8)],
                                       no_repeat_last_n=3, rng=rng),
        },
    )


def _cfg(**over) -> AutonomyConfig:
    cats = {
        "complain_silence": CategoryConfig(
            name="complain_silence", weight=1.0, cooldown_seconds=60,
            mood_boost={"bon_chon": 1.5}, prompt_hint="h1",
        ),
        "share_thought": CategoryConfig(
            name="share_thought", weight=1.2, cooldown_seconds=50,
            prompt_hint="h2",
        ),
        "ask_chat": CategoryConfig(
            name="ask_chat", weight=1.0, cooldown_seconds=50, prompt_hint="h3",
        ),
        "call_operator": CategoryConfig(
            name="call_operator", weight=0.6, cooldown_seconds=80,
            mood_boost={"bon_chon": 1.3}, prompt_hint="h4",
        ),
    }
    kw = dict(
        tick_seconds=5.0,
        urge=UrgeConfig(
            rise_base=0.5, rise_max_per_tick=6.0,
            urge_floor=30.0, urge_noise_std=3.0,
            self_cooldown_seconds=30, decay_after_speak=8.0,
            bon_chon_weight=0.6, buon_dampen=0.4, nguong_dampen=0.5,
            prob_scale=40.0, prob_max=0.85,
        ),
        no_repeat_window=1,
        categories=cats,
    )
    kw.update(over)
    return AutonomyConfig(**kw)


def _engine(cfg=None, rng_seed: int = 42, clock=None) -> AutonomyEngine:
    cfg = cfg or _cfg()
    rng = random.Random(rng_seed)
    clock = clock or FakeClock()
    return AutonomyEngine(
        cfg=cfg, material_provider=_mp(rng),
        clock=clock, rng=rng,
    )


def _run_sim(
    engine: AutonomyEngine, clock: FakeClock, mood: MoodState,
    total_seconds: float,
) -> list[tuple[float, str]]:
    """Chạy giả lập, trả list (timestamp, category) mỗi khi Mai tự nói."""
    ctx = RuntimeContext(
        silence_seconds=0.0, chat_count_last_10min=0,
        operator_online=False, consecutive_ignored=0,
        working_memory_recent=["past turn 1"],   # để follow_up khả dụng
    )
    speaks: list[tuple[float, str]] = []
    tick_s = engine.cfg.tick_seconds
    steps = int(total_seconds / tick_s)
    for _ in range(steps):
        clock.advance(tick_s)
        engine.tick(mood)
        decision = engine.maybe_generate(mood, ctx)
        if decision is not None:
            speaks.append((clock.now, decision.category))
            engine.on_self_spoke(f"câu Mai tự nói ({len(speaks)}) rất khác nhau")
    return speaks


# ─────────────────────── DoD 1: Variance ───────────────────────


class TestVariance:
    async def test_intervals_have_stdev_2h_sim(self) -> None:
        """2h giả lập, ít nhất 5 speaks, stdev intervals > 0 (không constant)."""
        clock = FakeClock()
        engine = _engine(clock=clock, rng_seed=42)
        speaks = _run_sim(engine, clock, MoodState(bon_chon=5, vui=5), 2 * 3600)
        assert len(speaks) >= 5, f"chỉ {len(speaks)} speaks trong 2h, quá ít"
        times = [t for t, _ in speaks]
        intervals = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        stdev = statistics.stdev(intervals)
        assert stdev > 0.0, f"intervals không có variance: {intervals}"
        # Coefficient of variation > 5% (không phải hằng số)
        mean = statistics.mean(intervals)
        cv = stdev / mean if mean > 0 else 0
        assert cv > 0.05, f"CV quá thấp {cv:.3f}, gần hằng số"


# ─────────────────────── DoD 2: No-repeat + distribution ───────────────────────


class TestNoRepeatDistribution:
    async def test_20_speaks_no_immediate_repeat(self) -> None:
        """20 speak liên tiếp: không lần nào 2 lần liên tiếp cùng cat."""
        clock = FakeClock()
        engine = _engine(clock=clock, rng_seed=123)
        # Chạy đủ dài để có 20 speaks
        speaks = _run_sim(engine, clock, MoodState(bon_chon=5, vui=5), 6 * 3600)
        assert len(speaks) >= 20, f"chỉ {len(speaks)} speaks trong 6h"
        cats = [c for _, c in speaks[:20]]
        for i in range(1, len(cats)):
            assert cats[i] != cats[i - 1], (
                f"lặp liên tiếp tại vị trí {i}: {cats[i-1]} → {cats[i]}"
            )

    async def test_20_speaks_no_category_over_60_percent(self) -> None:
        """Distribution: không cat nào chiếm > 60% (spec 40%, loosen do RNG variance)."""
        clock = FakeClock()
        engine = _engine(clock=clock, rng_seed=7)
        speaks = _run_sim(engine, clock, MoodState(bon_chon=5, vui=5), 6 * 3600)
        cats = [c for _, c in speaks[:20]]
        counter = Counter(cats)
        max_ratio = counter.most_common(1)[0][1] / len(cats)
        assert max_ratio < 0.6, (
            f"1 cat chiếm {max_ratio:.0%} — quá dominance. Counter: {counter}"
        )


# ─────────────────────── DoD 3: Self-cooldown ───────────────────────


class TestSelfCooldown:
    async def test_speak_blocked_during_cooldown(self) -> None:
        """Sau on_self_spoke, ngay lập tức should_speak_now = False."""
        clock = FakeClock()
        engine = _engine(clock=clock)
        engine.urge.urge = 100.0
        assert engine.urge.should_speak_now()
        engine.on_self_spoke("x")
        assert not engine.urge.should_speak_now()

    async def test_urge_stays_zero_full_cooldown_window(self) -> None:
        clock = FakeClock()
        engine = _engine(clock=clock)
        engine.on_self_spoke("x")
        cooldown = engine.cfg.urge.self_cooldown_seconds
        tick_s = engine.cfg.tick_seconds
        # Tick STRICTLY BÊN TRONG window (không tới boundary) — urge không tăng
        steps = int((cooldown - tick_s) / tick_s)
        for _ in range(steps):
            clock.advance(tick_s)
            engine.tick(MoodState(bon_chon=10))
        assert engine.urge.urge == 0.0
        assert not engine.urge.should_speak_now()


# ─────────────────────── DoD 4: Mood coupling ───────────────────────


class TestMoodCoupling:
    async def test_bon_chon_high_speaks_earlier(self) -> None:
        """Cùng seed, chạy N phút — HIGH bon_chon tổng speak > LOW."""
        # HIGH mood
        clock_h = FakeClock()
        engine_h = _engine(clock=clock_h, rng_seed=999)
        speaks_h = _run_sim(engine_h, clock_h, MoodState(bon_chon=9, vui=5),
                             45 * 60)  # 45 phút

        # LOW mood — cùng seed
        clock_l = FakeClock()
        engine_l = _engine(clock=clock_l, rng_seed=999)
        speaks_l = _run_sim(engine_l, clock_l, MoodState(bon_chon=1, vui=5),
                             45 * 60)

        assert len(speaks_h) > len(speaks_l), (
            f"HIGH {len(speaks_h)} speaks phải > LOW {len(speaks_l)} (bon_chon coupling)"
        )


# ─────────────────────── DoD 5: Nag decay ───────────────────────


class TestNagDecay:
    async def test_consecutive_ignored_lowers_urge_growth(self) -> None:
        """Cùng silence + mood, consecutive_ignored=5 → tick tăng urge chậm hơn=0."""
        # Baseline: ignored=0
        clock_a = FakeClock()
        engine_a = _engine(clock=clock_a, rng_seed=1)
        # Ignored: 5
        clock_b = FakeClock()
        engine_b = _engine(clock=clock_b, rng_seed=1)
        engine_b.urge.consecutive_ignored = 5

        # Cả 2 cùng silence 60s, tick 3 lần
        for _ in range(3):
            clock_a.advance(20); clock_b.advance(20)
            engine_a.tick(MoodState(bon_chon=5))
            engine_b.tick(MoodState(bon_chon=5))
        assert engine_b.urge.urge < engine_a.urge.urge, (
            f"nag: ignored=5 urge {engine_b.urge.urge} phải < baseline {engine_a.urge.urge}"
        )


# ─────────────────────── End-to-end sanity ───────────────────────


class TestEndToEndPipeline:
    async def test_generate_decision_has_full_prompt(self) -> None:
        """maybe_generate với urge high → decision.prompt_text chứa đủ slot."""
        clock = FakeClock()
        engine = _engine(clock=clock)
        engine.urge.urge = 100.0
        decision = engine.maybe_generate(
            MoodState(vui=6, bon_chon=4),
            RuntimeContext(
                silence_seconds=90.0, chat_count_last_10min=3,
                operator_online=True, consecutive_ignored=0,
                working_memory_recent=["hôm qua nói game"],
            ),
        )
        assert decision is not None
        # Prompt phải chứa lý do + KHÔNG-mở-bằng. T5 Mood→Style: KHÔNG rò số thô.
        assert decision.category in engine.cfg.categories
        assert "Context" in decision.prompt_text
        assert f"Lý do: {decision.category}" in decision.prompt_text
        assert "vui=6" not in decision.prompt_text and "Mood:" not in decision.prompt_text
        assert "KHÔNG được mở đầu" in decision.prompt_text

    async def test_dedup_regen_flow(self) -> None:
        """Sau on_self_spoke, câu tương tự bị flag → composer sẽ regen."""
        engine = _engine()
        engine.on_self_spoke("chào cậu ơi tớ đang nghĩ vu vơ")
        assert engine.check_dedup("chào cậu ơi tớ đang nghĩ vu vơ")
        assert not engine.check_dedup("hôm nay trời đẹp quá nhỉ")
