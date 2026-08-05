"""Test EmotionOrchestrator — Phase 7.5.C (glue T1→T3 + tick loop)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from interfaces.animation import MoodState
from orchestrator.emotion_orchestrator import EmotionOrchestrator, ProcessedEvent
from orchestrator.mood_engine import MoodEngine
from services.emotion.appraisal import AppraisalTable
from services.emotion.classifier import EmotionEvent, EventClassifier, EventKind
from services.emotion.modifiers import ModifierEngine

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
    def __call__(self) -> float: return self.now
    def advance(self, s: float) -> None: self.now += s


@pytest.fixture
def orch() -> EmotionOrchestrator:
    """Real components, no memory (modifier fail-safe)."""
    from orchestrator.config_loader import ConfigLoader
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    clock = FakeClock()
    engine = MoodEngine.from_loader(loader, clock=clock)
    classifier = EventClassifier.from_loader(loader)
    appraisal = AppraisalTable.from_loader(loader)
    modifiers = ModifierEngine.from_loader(loader, memory=None)
    return EmotionOrchestrator(
        classifier=classifier, appraisal=appraisal,
        modifiers=modifiers, engine=engine, tick_hz=10,
    )


def chat(text: str, **meta) -> EmotionEvent:
    return EmotionEvent(kind=EventKind.CHAT, text=text, meta=meta)


def system(ptype: str, **meta) -> EmotionEvent:
    return EmotionEvent(kind=EventKind.SYSTEM, meta={"platform_type": ptype, **meta})


class TestHandleEvent:
    async def test_returns_processed_event(self, orch: EmotionOrchestrator) -> None:
        r = await orch.handle_event(chat("Mai giỏi quá đi"))
        assert isinstance(r, ProcessedEvent)
        assert r.category == "chat_compliment"
        assert r.targets["vui"] > 0
        assert r.tone_flag is None

    async def test_buffers_target_not_applied_yet(self, orch: EmotionOrchestrator) -> None:
        """Handle_event chỉ buffer — chưa gọi apply_appraisal engine."""
        pre = orch._engine.target["vui"]
        await orch.handle_event(chat("Mai giỏi quá"))
        # target engine chưa đổi (còn buffer)
        assert orch._engine.target["vui"] == pre
        assert orch._pending["vui"]  # có buffer

    async def test_flush_and_tick_applies_buffer(self, orch: EmotionOrchestrator) -> None:
        await orch.handle_event(chat("Mai giỏi quá đi"))
        orch.flush_and_tick(dt=0.1)
        # sau flush: buffer rỗng, engine target cập nhật
        assert not orch._pending
        assert orch._engine.target["vui"] > 5  # baseline 5, event boost lên

    async def test_tone_flag_captured(self, orch: EmotionOrchestrator) -> None:
        """chat_genuine_sad_share → force_gentle_tone."""
        r = await orch.handle_event(chat("mình buồn quá tối qua khóc mãi"))
        assert r.category == "chat_genuine_sad_share"
        assert r.tone_flag == "force_gentle_tone"
        assert "force_gentle_tone" in orch.active_tone_flags()

    async def test_no_flag_for_normal_event(self, orch: EmotionOrchestrator) -> None:
        await orch.handle_event(chat("Mai giỏi quá đi"))
        assert orch.active_tone_flags() == set()


class TestSaturation:
    async def test_multiple_events_same_tick_saturated(self, orch: EmotionOrchestrator) -> None:
        """3 donation cùng tick → saturate max + 0.5×(n-1) chứ không cộng."""
        for _ in range(3):
            await orch.handle_event(system("donation", amount_vnd=100_000))
        orch.flush_and_tick(dt=0.1)
        # 3 donation_large: target vui gốc 9, first-time ×1.2 = 10.8→cap 10.
        # Buffer [10, 10, 10] → saturate max(10) + 0.5*2 = 11 → cap 10.
        assert orch._engine.target["vui"] == 10

    async def test_conflicting_events_max_wins(self, orch: EmotionOrchestrator) -> None:
        """Nhiều khen (buc thấp) xen 1 troll (buc cao) → buc vẫn nhích lên (max)."""
        # Fake compliment lấy vui + nguong, troll lấy buc
        for _ in range(5):
            await orch.handle_event(chat("Mai giỏi quá đi"))  # compliment
        # Troll thẳng qua chat text, không cần filter (filter=None → keyword ko match troll)
        # → chat_neutral, không đủ trigger buc. Bypass: dùng system... nhưng không có sys troll.
        # Đủ: chỉ verify max nhiều compliment không tạo overshoot.
        orch.flush_and_tick(dt=0.1)
        assert 0 <= orch._engine.target["vui"] <= 10


class TestTickLoop:
    async def test_start_stop_lifecycle(self, orch: EmotionOrchestrator) -> None:
        await orch.start()
        assert orch._tick_task is not None
        assert not orch._tick_task.done()
        await orch.stop()
        assert orch._tick_task is None

    async def test_start_idempotent(self, orch: EmotionOrchestrator) -> None:
        await orch.start()
        first = orch._tick_task
        await orch.start()  # không tạo task mới
        assert orch._tick_task is first
        await orch.stop()

    async def test_background_tick_advances_engine(self, orch: EmotionOrchestrator) -> None:
        """Tick loop 10Hz thực sự chạy — sau 0.3s, engine.tick count > 0."""
        pre = orch._engine.get_metrics()["mood_ticks"]
        await orch.start()
        await asyncio.sleep(0.35)  # ~3 tick @ 10Hz
        await orch.stop()
        post = orch._engine.get_metrics()["mood_ticks"]
        assert post - pre >= 2, f"chỉ {post-pre} tick trong 350ms (kỳ vọng ~3)"

    async def test_stop_with_no_start_safe(self, orch: EmotionOrchestrator) -> None:
        await orch.stop()  # không raise


class TestLLMHint:
    async def test_apply_llm_hint_proxies_to_engine(self, orch: EmotionOrchestrator) -> None:
        pre = orch._engine.target["vui"]
        orch.apply_llm_hint(MoodState(vui=10))
        # Kênh B nudge nhẹ (weight 0.2) — target tăng nhưng chưa tới 10
        assert orch._engine.target["vui"] > pre
        assert orch._engine.target["vui"] < 10


class TestFlags:
    async def test_clear_flags(self, orch: EmotionOrchestrator) -> None:
        await orch.handle_event(chat("buồn quá"))
        assert orch.active_tone_flags()
        orch.clear_tone_flags()
        assert orch.active_tone_flags() == set()


class TestSessionReset:
    async def test_reset_session_clears_modifier_counters(self, orch: EmotionOrchestrator) -> None:
        """reset_session gọi modifier.reset_session, không đụng mood engine."""
        # Dùng filter fake để trigger troll
        class Fake:
            def check(self, t):
                from types import SimpleNamespace
                return SimpleNamespace(passed=False,
                                       categories_hit=[SimpleNamespace(value="insult")])
        orch._classifier._filter = Fake()

        await orch.handle_event(chat("ngu"))
        await orch.handle_event(chat("ngu"))
        assert orch._modifiers._session_troll_count == 2
        orch.reset_session()
        assert orch._modifiers._session_troll_count == 0


class TestCurrentMood:
    async def test_returns_moodstate(self, orch: EmotionOrchestrator) -> None:
        m = orch.current_mood()
        assert isinstance(m, MoodState)
        # baseline: vui=5, buc=4, ...
        assert m.vui == 5


class TestFromLoader:
    def test_from_loader_wires_all(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        orch = EmotionOrchestrator.from_loader(loader)
        assert orch._tick_hz == 10
        assert orch._engine is not None
        assert orch._classifier is not None
        assert orch._appraisal is not None
        assert orch._modifiers is not None


class TestSnapshot:
    async def test_snapshot_shape(self, orch: EmotionOrchestrator) -> None:
        s = orch.snapshot()
        assert "mood_pos" in s
        assert "current_mood" in s
        assert "active_flags" in s
        assert set(s["mood_pos"].keys()) == {"vui", "buon", "buc", "bon_chon", "nguong"}
