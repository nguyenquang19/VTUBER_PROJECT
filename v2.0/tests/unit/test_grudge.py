"""Test A4 — GrudgeTracker trong ModifierEngine (docs/03_COMPONENT_REFERENCE.md §PHASE A).

DoD A4 (phần grudge):
- grudge tự hết sau ngưỡng thời gian (decay)
- reset khi tương tác tích cực
- CAP — không leo thang (chống toxic)
"""
from __future__ import annotations

import pytest

from services.emotion.modifiers import ModifierEngine


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.now = t

    def __call__(self) -> float:
        return self.now


def _me(clock: FakeClock, **over) -> ModifierEngine:
    kw = dict(
        memory=None,
        first_time_multiplier=1.0,   # isolate grudge (tắt first_time ×1.2)
        grudge_window_seconds=900.0,
        grudge_bonus_per_level=0.5,
        grudge_max_bonus=1.5,
        grudge_max_level=3,
        clock=clock,
    )
    kw.update(over)
    return ModifierEngine(**kw)


class TestGrudge:
    async def test_first_negative_no_bonus_yet(self) -> None:
        # Lần tiêu cực ĐẦU: chưa có grudge trước đó → không cộng gì (chỉ bump).
        clk = FakeClock()
        me = _me(clk)
        out = await me.apply("chat_insult_troll", {"buc": 5.0}, viewer_id="v1")
        # session_troll_count=1 → repeated_troll chưa cộng; grudge lần đầu chưa cộng
        assert out["buc"] == 5.0

    async def test_second_negative_same_viewer_gets_grudge_bonus(self) -> None:
        clk = FakeClock()
        me = _me(clk)
        await me.apply("chat_insult_troll", {"buc": 5.0}, viewer_id="v1")
        clk.now += 30  # trong window
        out = await me.apply("chat_insult_troll", {"buc": 5.0}, viewer_id="v1")
        # grudge level 1 (từ lần trước) → +0.5; repeated_troll (count 2) → +0.5
        assert out["buc"] > 5.0

    async def test_grudge_decays_after_window(self) -> None:
        # DoD: grudge tự hết sau ngưỡng thời gian.
        clk = FakeClock()
        me = _me(clk, grudge_window_seconds=900.0)
        await me.apply("chat_insult_troll", {"buc": 5.0}, viewer_id="v1")
        clk.now += 1000  # > 900 window → grudge hết
        out = await me.apply("chat_neutral", {"buc": 3.0}, viewer_id="v1")
        assert out["buc"] == 3.0  # không còn bonus grudge
        assert me.get_metrics()["mod_grudge_active_viewers"] == 0

    async def test_positive_interaction_resets_grudge(self) -> None:
        clk = FakeClock()
        me = _me(clk)
        await me.apply("chat_insult_troll", {"buc": 5.0}, viewer_id="v1")
        # cùng viewer khen → tha
        await me.apply("chat_compliment", {"vui": 7.0}, viewer_id="v1")
        assert me.get_metrics()["mod_grudge_active_viewers"] == 0
        # lượt sau không còn grudge bonus
        out = await me.apply("chat_neutral", {"buc": 2.0}, viewer_id="v1")
        assert out["buc"] == 2.0

    async def test_grudge_bonus_capped_no_escalation(self) -> None:
        # DoD: CAP — dồn nhiều lần vẫn không vượt grudge_max_bonus (không leo thang).
        clk = FakeClock()
        me = _me(clk, grudge_max_bonus=1.5, grudge_max_level=3)
        # dồn 10 lần tiêu cực cùng viewer, mỗi lần cách 10s (trong window)
        for _ in range(10):
            await me.apply("chat_jailbreak_attempt", {"buc": 4.0}, viewer_id="v1")
            clk.now += 10
        # lượt kiểm: bonus grudge không vượt cap 1.5
        out = await me.apply("chat_neutral", {"buc": 4.0}, viewer_id="v1")
        assert out["buc"] <= 4.0 + 1.5 + 1e-9

    async def test_grudge_per_viewer_isolated(self) -> None:
        clk = FakeClock()
        me = _me(clk)
        await me.apply("chat_insult_troll", {"buc": 5.0}, viewer_id="v1")
        clk.now += 30
        # v2 mới, chưa có grudge → không bonus
        out = await me.apply("chat_neutral", {"buc": 3.0}, viewer_id="v2")
        assert out["buc"] == 3.0

    async def test_no_viewer_id_no_grudge(self) -> None:
        clk = FakeClock()
        me = _me(clk)
        out = await me.apply("chat_insult_troll", {"buc": 5.0}, viewer_id=None)
        assert out["buc"] == 5.0  # không viewer → không grudge state
        assert me.get_metrics()["mod_grudge_active_viewers"] == 0
