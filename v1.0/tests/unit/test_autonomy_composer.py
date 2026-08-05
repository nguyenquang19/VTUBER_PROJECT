"""Test AutonomyEngine composer + prompt_builder — Aut.C."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from interfaces.animation import MoodState
from orchestrator.autonomy_engine import (
    AmbientDecision,
    AutonomyConfig,
    AutonomyEngine,
    CategoryConfig,
    UrgeConfig,
)
from services.autonomy.dedup import DedupBuffer
from services.autonomy.material_provider import MaterialProvider, RuntimeContext
from services.autonomy.opener_tracker import OpenerTracker
from services.autonomy.pools import RoundRobinPool
from services.autonomy.prompt_builder import render_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
    def __call__(self) -> float: return self.now
    def advance(self, s: float) -> None: self.now += s


def make_engine(clock=None, rng=None, urge_floor: float = 30.0) -> AutonomyEngine:
    cats = {
        "complain_silence": CategoryConfig(
            name="complain_silence", weight=1.0, cooldown_seconds=100,
            prompt_hint="càm ràm nhẹ",
        ),
        "share_thought": CategoryConfig(
            name="share_thought", weight=1.0, cooldown_seconds=100,
            prompt_hint="kể chuyện vặt",
        ),
        "follow_up_topic": CategoryConfig(
            name="follow_up_topic", weight=1.0, cooldown_seconds=100,
            prompt_hint="quay lại chủ đề",
        ),
    }
    cfg = AutonomyConfig(
        tick_seconds=5.0,
        urge=UrgeConfig(
            urge_floor=urge_floor, urge_noise_std=0.0,
            self_cooldown_seconds=1,  # ngắn cho test
            prob_scale=10.0, prob_max=1.0,  # deterministic khi urge cao
        ),
        no_repeat_window=1,
        categories=cats,
    )
    mp = MaterialProvider(
        share_thought_pool=RoundRobinPool(["chuyện x", "chuyện y"],
                                           no_repeat_last_n=1,
                                           rng=random.Random(0)),
        question_pools={},
    )
    return AutonomyEngine(
        cfg=cfg, material_provider=mp,
        opener_tracker=OpenerTracker(window=3),
        dedup_buffer=DedupBuffer(window=3, threshold=0.6),
        clock=clock or FakeClock(), rng=rng or random.Random(0),
    )


def ctx(**over) -> RuntimeContext:
    kw = dict(
        silence_seconds=60.0, chat_count_last_10min=1,
        operator_online=True, consecutive_ignored=0,
        working_memory_recent=[],
    )
    kw.update(over)
    return RuntimeContext(**kw)


# ═══════════════════════ AutonomyEngine composer ═══════════════════════


class TestTickForwarding:
    def test_tick_updates_urge(self) -> None:
        clock = FakeClock()
        e = make_engine(clock=clock)
        clock.advance(30.0)
        e.tick(MoodState(bon_chon=5))
        assert e.urge.urge > 0

    def test_on_external_activity_resets(self) -> None:
        e = make_engine()
        e.urge.consecutive_ignored = 3
        e.on_external_activity()
        assert e.urge.consecutive_ignored == 0


class TestMaybeGenerate:
    def test_none_when_urge_below_floor(self) -> None:
        e = make_engine()
        e.urge.urge = 10.0  # < floor 30
        assert e.maybe_generate(MoodState(), ctx()) is None

    def test_returns_decision_when_ready(self) -> None:
        e = make_engine()
        e.urge.urge = 90.0
        d = e.maybe_generate(MoodState(), ctx())
        assert d is not None
        assert isinstance(d, AmbientDecision)
        assert d.category in e.cfg.categories
        assert "Context" in d.prompt_text
        assert d.mood_snapshot is not None

    def test_none_when_material_unavailable_all(self) -> None:
        """follow_up cần memory, share_thought cần pool, complain cần silence.
        Tất cả cat còn cooldown → không có candidate."""
        e = make_engine()
        e.urge.urge = 100.0
        # mark tất cả cat as recently used (cooldown)
        for name in e.cfg.categories:
            e.selector.mark_used(name)
        e.selector._recent.clear()  # nhưng cho no_repeat clear (chỉ cooldown block)
        d = e.maybe_generate(MoodState(), ctx())
        assert d is None

    def test_skips_follow_up_when_no_memory(self) -> None:
        """follow_up_topic không có memory → skip, phải chọn cat khác."""
        e = make_engine()
        e.urge.urge = 100.0
        # Loop nhiều lần — không lần nào chọn follow_up (vì material None)
        chosen = set()
        for _ in range(10):
            e.urge.urge = 100.0  # keep high
            # Reset cooldowns để không exhaust
            e.selector._recent.clear()
            e.selector._last_used_ts.clear()
            d = e.maybe_generate(MoodState(), ctx(working_memory_recent=[]))
            if d:
                chosen.add(d.category)
        assert "follow_up_topic" not in chosen

    def test_follow_up_available_when_memory_present(self) -> None:
        """Có memory → follow_up khả dụng."""
        e = make_engine()
        e.urge.urge = 100.0
        chosen = set()
        for _ in range(30):
            e.urge.urge = 100.0
            e.selector._recent.clear()
            e.selector._last_used_ts.clear()
            d = e.maybe_generate(MoodState(), ctx(working_memory_recent=["turn1", "turn2"]))
            if d:
                chosen.add(d.category)
        # Với memory, follow_up có material → có thể được chọn (weighted)
        # Không guarantee mỗi lần nhưng ít nhất 1 lần trong 30
        assert "follow_up_topic" in chosen

    def test_metric_increments(self) -> None:
        e = make_engine()
        e.urge.urge = 100.0
        d = e.maybe_generate(MoodState(), ctx())
        assert d is not None
        assert e.get_metrics()["autonomy_generated_total"] == 1


class TestOnSelfSpoke:
    def test_records_opener_and_dedup(self) -> None:
        e = make_engine()
        e.on_self_spoke("Chào cậu ơi cậu đang làm gì đấy")
        assert "chào cậu ơi" in e.opener.recent()
        assert "Chào cậu ơi cậu đang làm gì đấy" in e.dedup.recent()

    def test_resets_urge(self) -> None:
        e = make_engine()
        e.urge.urge = 80.0
        e.on_self_spoke("hello")
        assert e.urge.urge == 0.0
        assert e.urge.consecutive_ignored == 1


class TestCheckDedup:
    def test_flags_similar(self) -> None:
        e = make_engine()
        e.on_self_spoke("chào cậu ơi cậu đâu rồi")
        assert e.check_dedup("chào cậu ơi cậu đâu rồi") is True

    def test_not_flags_different(self) -> None:
        e = make_engine()
        e.on_self_spoke("chào cậu ơi")
        assert e.check_dedup("hôm nay trời đẹp quá đi") is False


class TestPromptBuilder:
    def test_complain_silence_includes_data(self) -> None:
        p = render_prompt(
            category="complain_silence",
            material={"silence_seconds": 90, "chat_count_10min": 2},
            mood=MoodState(vui=5, bon_chon=6),
            forbidden_openers='"chào cậu..."',
            prompt_hint="càm ràm nhẹ",
        )
        assert "complain_silence" in p
        assert "90s" in p
        assert "2 tin" in p
        assert "bon_chon=6" in p
        assert '"chào cậu..."' in p

    def test_share_thought_includes_seed(self) -> None:
        p = render_prompt(
            "share_thought", {"topic_seed": "hôm nay trời mát"},
            MoodState(), "(không có)", "kể chuyện",
        )
        assert "hôm nay trời mát" in p
        assert "share_thought" in p

    def test_ask_chat_includes_seed_and_kind(self) -> None:
        p = render_prompt(
            "ask_chat", {"question_seed": "cậu thức đêm sao", "question_kind": "personal"},
            MoodState(), "(không có)", "hỏi tò mò",
        )
        assert "cậu thức đêm sao" in p
        assert "personal" in p

    def test_call_operator_online_vs_offline(self) -> None:
        p_on = render_prompt(
            "call_operator", {"operator_online": True, "ignored_streak": 2},
            MoodState(), "(không có)", "gọi ông",
        )
        p_off = render_prompt(
            "call_operator", {"operator_online": False, "ignored_streak": 0},
            MoodState(), "(không có)", "gọi ông",
        )
        assert "đang online" in p_on
        assert "chưa thấy" in p_off

    def test_follow_up_includes_snippet(self) -> None:
        p = render_prompt(
            "follow_up_topic", {"memory_snippet": "hôm qua kể chuyện chơi game"},
            MoodState(), "(không có)", "quay lại chủ đề",
        )
        assert "hôm qua kể chuyện chơi game" in p

    def test_unknown_category_has_fallback_body(self) -> None:
        p = render_prompt(
            "random_cat", {}, MoodState(), "(không có)", "fallback",
        )
        assert "không có material cụ thể" in p


class TestFromLoader:
    def test_builds_full_stack(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        e = AutonomyEngine.from_loader(loader, rng=random.Random(0))
        assert e.cfg.tick_seconds == 5.0
        assert e.material is not None
        assert e.opener is not None
        assert e.dedup is not None

        # Force high urge → sinh 1 decision từ config thật
        e.urge.urge = 100.0
        d = e.maybe_generate(MoodState(vui=5), ctx(working_memory_recent=["past"]))
        assert d is not None
