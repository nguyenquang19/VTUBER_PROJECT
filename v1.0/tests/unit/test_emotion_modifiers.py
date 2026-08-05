"""Test ModifierEngine — Phase 7.5.B (3 modifier nhân hệ số target)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from interfaces.memory import MemoryEntry, MemoryTier
from services.emotion.modifiers import ModifierEngine

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeMemory:
    """Return preset entries theo query text."""

    def __init__(self, responses: dict[str, list[MemoryEntry]] | None = None) -> None:
        self.responses = responses or {}
        self.query_calls: list[tuple] = []

    async def query(self, text, top_k=3, tier=None, viewer_id=None):
        self.query_calls.append((text, top_k, tier, viewer_id))
        return list(self.responses.get(text, []))


def make(memory=None, **over) -> ModifierEngine:
    kw = dict(
        memory=memory,
        repeated_shutdown_window_days=7,
        repeated_shutdown_threshold=3,
        repeated_shutdown_multiplier=1.3,
        repeated_troll_bonus_per_hit=0.5,
        first_time_multiplier=1.2,
    )
    kw.update(over)
    return ModifierEngine(**kw)


def past_entry(cat: str, days_ago: int = 1) -> MemoryEntry:
    return MemoryEntry(
        entry_id=f"e-{cat}-{days_ago}",
        content=f"past {cat}",
        timestamp=datetime.now() - timedelta(days=days_ago),
        tier=MemoryTier.PERSISTENT,
        tags=[cat],
    )


class TestEmptyTargets:
    async def test_empty_targets_stay_empty(self) -> None:
        m = make()
        assert await m.apply("chat_neutral", {}) == {}


class TestRepeatedTroll:
    """Isolate repeated_troll bằng first_time_multiplier=1.0 (khỏi trộn 2 modifier)."""

    async def test_first_troll_no_bonus(self) -> None:
        m = make(first_time_multiplier=1.0)
        out = await m.apply("chat_insult_troll", {"buc": 8})
        assert out["buc"] == 8  # lần đầu chưa +bonus (spec: mỗi lần thứ N)

    async def test_second_troll_plus_bonus(self) -> None:
        m = make(repeated_troll_bonus_per_hit=0.5, first_time_multiplier=1.0)
        await m.apply("chat_insult_troll", {"buc": 8})  # +1
        out = await m.apply("chat_insult_troll", {"buc": 8})  # +2 → bonus 0.5
        assert out["buc"] == pytest.approx(8.5)

    async def test_multiple_trolls_accumulate(self) -> None:
        m = make(repeated_troll_bonus_per_hit=0.5, first_time_multiplier=1.0)
        for _ in range(5):
            out = await m.apply("chat_insult_troll", {"buc": 8})
        # Lần thứ 5: 8 + 0.5×4 = 10
        assert out["buc"] == pytest.approx(10.0)

    async def test_buc_caps_at_10(self) -> None:
        m = make(repeated_troll_bonus_per_hit=2.0, first_time_multiplier=1.0)
        for _ in range(10):
            out = await m.apply("chat_insult_troll", {"buc": 8})
        assert out["buc"] == 10

    async def test_reset_session_clears_troll_count(self) -> None:
        m = make(repeated_troll_bonus_per_hit=0.5, first_time_multiplier=1.0)
        await m.apply("chat_insult_troll", {"buc": 8})
        await m.apply("chat_insult_troll", {"buc": 8})
        m.reset_session()
        out = await m.apply("chat_insult_troll", {"buc": 8})
        assert out["buc"] == 8  # lần đầu sau reset


class TestFirstTime:
    async def test_first_time_no_memory_boost(self) -> None:
        """Không có memory → coi first_time → ×1.2."""
        m = make(memory=None, first_time_multiplier=1.2)
        out = await m.apply("chat_compliment", {"vui": 7})
        assert out["vui"] == pytest.approx(7 * 1.2)

    async def test_second_time_same_session_no_boost(self) -> None:
        m = make(first_time_multiplier=1.2)
        await m.apply("chat_compliment", {"vui": 7})   # first time in session
        out = await m.apply("chat_compliment", {"vui": 7})  # second → no boost
        assert out["vui"] == 7

    async def test_memory_has_past_no_boost(self) -> None:
        mem = FakeMemory({"chat_compliment": [past_entry("chat_compliment")]})
        m = make(memory=mem, first_time_multiplier=1.2)
        out = await m.apply("chat_compliment", {"vui": 7})
        assert out["vui"] == 7

    async def test_neutral_category_no_boost(self) -> None:
        m = make()
        out = await m.apply("chat_neutral", {"vui": 5})
        assert out.get("vui", 0) == 5  # empty target → no change

    async def test_first_time_caps_at_10(self) -> None:
        m = make(first_time_multiplier=1.2)
        out = await m.apply("donation_large", {"vui": 9})
        assert out["vui"] == pytest.approx(min(10.0, 9 * 1.2))


class TestRepeatedShutdown:
    async def test_no_memory_no_boost(self) -> None:
        m = make(memory=None, repeated_shutdown_multiplier=1.3)
        out = await m.apply("operator_sudden_shutdown", {"buon": 8, "buc": 6})
        assert out["buon"] == pytest.approx(8 * 1.2)  # chỉ first_time (memory=None)

    async def test_3_shutdowns_in_week_boost(self) -> None:
        mem = FakeMemory({
            "operator_sudden_shutdown": [
                past_entry("operator_sudden_shutdown", days_ago=1),
                past_entry("operator_sudden_shutdown", days_ago=2),
                past_entry("operator_sudden_shutdown", days_ago=3),
            ],
        })
        m = make(memory=mem, repeated_shutdown_multiplier=1.3, first_time_multiplier=1.0)
        out = await m.apply("operator_sudden_shutdown", {"buon": 8})
        assert out["buon"] == pytest.approx(min(10.0, 8 * 1.3))

    async def test_2_shutdowns_no_boost(self) -> None:
        mem = FakeMemory({
            "operator_sudden_shutdown": [past_entry("operator_sudden_shutdown", days_ago=1)] * 2,
        })
        m = make(memory=mem, first_time_multiplier=1.0)
        out = await m.apply("operator_sudden_shutdown", {"buon": 8})
        assert out["buon"] == 8  # dưới threshold

    async def test_shutdowns_outside_window_ignored(self) -> None:
        mem = FakeMemory({
            "operator_sudden_shutdown": [
                past_entry("operator_sudden_shutdown", days_ago=30),
                past_entry("operator_sudden_shutdown", days_ago=45),
                past_entry("operator_sudden_shutdown", days_ago=60),
            ],
        })
        m = make(memory=mem, first_time_multiplier=1.0)
        out = await m.apply("operator_sudden_shutdown", {"buon": 8})
        assert out["buon"] == 8  # ngoài 7 ngày


class TestFailSafe:
    async def test_memory_query_error_no_raise(self) -> None:
        class BrokenMemory:
            async def query(self, *args, **kw): raise RuntimeError("db down")
        m = make(memory=BrokenMemory(), first_time_multiplier=1.2)
        # Không raise, chấp nhận modifier bị bỏ qua
        out = await m.apply("chat_compliment", {"vui": 7})
        assert out["vui"] > 0  # vẫn có kết quả (mod fail-safe)


class TestFromLoader:
    def test_reads_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = ModifierEngine.from_loader(loader)
        assert m._shutdown_threshold == 3
        assert m._shutdown_mult == pytest.approx(1.3)
        assert m._troll_bonus == pytest.approx(0.5)
        assert m._first_time_mult == pytest.approx(1.2)


class TestMetrics:
    async def test_counters(self) -> None:
        m = make()
        await m.apply("chat_insult_troll", {"buc": 8})
        await m.apply("chat_insult_troll", {"buc": 8})
        met = m.get_metrics()
        assert met["mod_session_troll_count"] == 2
        assert met["mod_repeated_troll_applies"] == 1  # lần 2 mới bonus
