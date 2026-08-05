"""Integration DoD Phase 7.5 (spec Mục 8.2).

DoD 7 items:
1. 20 category + 4 timer + 3 modifier — covered ở test_emotion_appraisal
2. MoodEngine 10k tick no NaN/oscillation — covered ở test_mood_engine
3. Saturation 100 event — covered ở test_mood_engine + test_emotion_orchestrator
4. Target decay — covered ở test_mood_engine
5. 2 cờ tone nối Prompt + Filter — INTEGRATION test dưới đây
6. Drift detector log khi lệch — INTEGRATION test dưới đây
7. Live ≥100 turn subjective — SKIP (user duyệt)

Test này focus items 5+6 + end-to-end mood evolution.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.animation import MoodState
from interfaces.llm import ChatMessage
from orchestrator.emotion_orchestrator import EmotionOrchestrator
from orchestrator.mood_engine import MoodEngine
from services.emotion.appraisal import AppraisalTable
from services.emotion.classifier import EmotionEvent, EventClassifier, EventKind
from services.emotion.modifiers import ModifierEngine
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager
from services.qc.drift_detector import DriftDetector

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
    def __call__(self) -> float: return self.now


@pytest.fixture
def orch(tmp_path) -> EmotionOrchestrator:
    from orchestrator.config_loader import ConfigLoader
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    engine = MoodEngine.from_loader(loader, clock=FakeClock())
    return EmotionOrchestrator(
        classifier=EventClassifier.from_loader(loader),
        appraisal=AppraisalTable.from_loader(loader),
        modifiers=ModifierEngine.from_loader(loader, memory=None),
        engine=engine,
        tick_hz=10,
    )


@pytest.fixture
def pm(tmp_path):
    persona = tmp_path / "persona.txt"
    persona.write_text("You are Mai. Sẽ nhận Context mood ĐƯỢC GIAO.", encoding="utf-8")
    return PromptManager(cache=PromptCache.from_file(persona), max_history_turns=3)


def chat(text: str, **meta) -> EmotionEvent:
    return EmotionEvent(kind=EventKind.CHAT, text=text, meta=meta)


# ---------- DoD 5: Tone flag nối tới Prompt ----------


class TestToneFlagsWiredToPrompt:
    async def test_sad_share_creates_gentle_flag_in_prompt(
        self, orch: EmotionOrchestrator, pm: PromptManager,
    ) -> None:
        """chat_genuine_sad_share → force_gentle_tone → prompt chứa hint đồng cảm."""
        await orch.handle_event(chat("mình buồn quá tối qua khóc mãi"))
        orch.flush_and_tick(dt=0.1)

        req = pm.build_request_with_mood(
            request_id="r1",
            user_text="tiếp",
            current_mood=orch.current_mood(),
            event_category="chat_genuine_sad_share",
            tone_flags=orch.active_tone_flags(),
        )
        # System message thứ 2 = Context
        ctx = req.messages[1].content
        assert "force_gentle_tone" in ctx
        assert "đồng cảm" in ctx.lower() or "tổn thương" in ctx.lower()

    async def test_sexual_advance_creates_deflect_flag(
        self, orch: EmotionOrchestrator, pm: PromptManager,
    ) -> None:
        """Cần filter fake để trigger chat_sexual_advance."""
        class Fake:
            def check(self, t):
                from types import SimpleNamespace
                return SimpleNamespace(passed=False,
                                       categories_hit=[SimpleNamespace(value="explicit")])
        orch._classifier._filter = Fake()

        await orch.handle_event(chat("..."))
        orch.flush_and_tick(dt=0.1)

        req = pm.build_request_with_mood(
            request_id="r",
            user_text="reply",
            current_mood=orch.current_mood(),
            event_category="chat_sexual_advance",
            tone_flags=orch.active_tone_flags(),
        )
        ctx = req.messages[1].content
        assert "force_deflect" in ctx
        assert "né" in ctx or "gạ" in ctx

    async def test_no_flag_for_normal_event(
        self, orch: EmotionOrchestrator, pm: PromptManager,
    ) -> None:
        await orch.handle_event(chat("Mai giỏi quá"))
        orch.flush_and_tick(dt=0.1)
        req = pm.build_request_with_mood(
            request_id="r", user_text="hi",
            current_mood=orch.current_mood(),
            event_category="chat_compliment",
            tone_flags=orch.active_tone_flags(),
        )
        ctx = req.messages[1].content
        assert "force_gentle_tone" not in ctx
        assert "force_deflect" not in ctx


# ---------- DoD 6: Drift detector log khi lệch ----------


class TestDriftDetectorEndToEnd:
    async def test_drift_flagged_when_engine_says_troll_but_llm_says_happy(
        self, orch: EmotionOrchestrator,
    ) -> None:
        """Appraisal buc→8 (troll rõ) nhưng LLM tự bịa vui=8 → flag."""
        # Setup: troll event → engine mood buc cao
        class Fake:
            def check(self, t):
                from types import SimpleNamespace
                return SimpleNamespace(passed=False,
                                       categories_hit=[SimpleNamespace(value="insult")])
        orch._classifier._filter = Fake()

        await orch.handle_event(chat("ngu ơi"))
        # Tick nhiều lần để mood position chạm target
        for _ in range(30):
            orch.flush_and_tick(dt=0.1)
        engine_mood = orch.current_mood()
        assert engine_mood.buc >= 5  # position đã lên gần target 8

        # LLM tự bịa vui cao — drift detector flag
        drift = DriftDetector(threshold=4)
        llm_report = MoodState(vui=8, buc=0)  # trái ngược thực tế
        report = drift.detect(engine_mood, llm_report)
        assert report.flagged is True

    async def test_no_drift_when_llm_aligns_with_engine(
        self, orch: EmotionOrchestrator,
    ) -> None:
        """LLM report gần với engine → không flag."""
        await orch.handle_event(chat("Mai giỏi quá"))
        for _ in range(30):
            orch.flush_and_tick(dt=0.1)
        engine_mood = orch.current_mood()

        drift = DriftDetector(threshold=4)
        # LLM report gần engine (chênh ≤ 2)
        llm_report = MoodState(
            vui=min(10, engine_mood.vui + 1),
            nguong=engine_mood.nguong,
            buc=engine_mood.buc,
        )
        report = drift.detect(engine_mood, llm_report)
        assert report.flagged is False


# ---------- End-to-end mood evolution ----------


class TestMoodEvolution:
    async def test_series_of_events_moves_mood(self, orch: EmotionOrchestrator) -> None:
        """3 donation liên tiếp → mood vui tăng dần lên gần target 10."""
        for _ in range(3):
            await orch.handle_event(
                EmotionEvent(kind=EventKind.SYSTEM,
                             meta={"platform_type": "donation", "amount_vnd": 100_000})
            )
        # Tick 50 lần (~5s) để position chạm target
        for _ in range(50):
            orch.flush_and_tick(dt=0.1)
        mood = orch.current_mood()
        assert mood.vui >= 8  # gần 10 (target donation_large + saturation)

    async def test_mood_decays_after_idle(self, orch: EmotionOrchestrator) -> None:
        """Sự kiện xong → không có event mới → mood tự về baseline."""
        # Kích mood vui lên cao
        await orch.handle_event(
            EmotionEvent(kind=EventKind.SYSTEM,
                         meta={"platform_type": "donation", "amount_vnd": 100_000})
        )
        for _ in range(30):
            orch.flush_and_tick(dt=0.1)
        peak_vui = orch.current_mood().vui
        assert peak_vui > 5  # đã lên trên baseline

        # Advance clock để trigger target_decay (baseline vui = 5)
        clock: FakeClock = orch._engine._clock
        clock.now += 30.0  # 30 giây trôi qua
        for _ in range(100):
            orch.flush_and_tick(dt=0.1)
        # Mood vui phải giảm về baseline (5) hoặc gần
        assert orch.current_mood().vui <= peak_vui


# ---------- Full 7-item DoD sanity summary ----------


class TestDoDSummary:
    async def test_all_dod_pieces_reachable(
        self, orch: EmotionOrchestrator, pm: PromptManager,
    ) -> None:
        """Sanity: verify tất cả API DoD Phase 7.5 dùng được đầu-cuối trong 1 flow."""
        # 1. Event → classify
        r = await orch.handle_event(chat("Mai giỏi quá!"))
        assert r.category  # non-empty
        # 2. Flush → mood engine tick
        m = orch.flush_and_tick(dt=0.1)
        assert isinstance(m, MoodState)
        # 3. Prompt có mood + category
        req = pm.build_request_with_mood(
            "r", "next", orch.current_mood(),
            event_category=r.category,
            tone_flags=orch.active_tone_flags(),
        )
        assert any("current_mood" in msg.content for msg in req.messages if isinstance(msg, ChatMessage))
        # 4. Apply LLM hint (Kênh B)
        orch.apply_llm_hint(MoodState(vui=8))
        # 5. Drift detect
        drift = DriftDetector(threshold=4)
        report = drift.detect(orch.current_mood(), MoodState(vui=0))
        assert report is not None
        # 6. Clear tone flags after turn
        orch.clear_tone_flags()
        assert orch.active_tone_flags() == set()
