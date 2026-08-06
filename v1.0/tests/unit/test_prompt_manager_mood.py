"""Test PromptManager.build_request_with_mood — Phase 7.5.D."""
from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.animation import MoodState
from interfaces.llm import ChatMessage
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager, _format_mood_context

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pm(tmp_path: Path) -> PromptManager:
    persona = tmp_path / "persona.txt"
    persona.write_text("You are Mai.", encoding="utf-8")
    cache = PromptCache.from_file(persona)
    return PromptManager(cache=cache, max_history_turns=3)


class TestFormatMoodContext:
    def test_basic_shape(self) -> None:
        # Mood→Style: BỎ số thô current_mood + event_category khỏi prompt
        m = MoodState(vui=6, buon=2, buc=4, bon_chon=3, nguong=1)
        s = _format_mood_context(m, "chat_compliment", None)
        assert "[Context" in s
        assert "current_mood" not in s
        assert "event_category" not in s

    def test_no_category(self) -> None:
        m = MoodState()
        s = _format_mood_context(m, None, None)
        assert "event_category" not in s

    def test_gentle_tone_flag_hint(self) -> None:
        m = MoodState()
        s = _format_mood_context(m, "chat_genuine_sad_share", {"force_gentle_tone"})
        assert "force_gentle_tone" in s
        assert "đồng cảm" in s.lower() or "tổn thương" in s.lower()

    def test_deflect_tone_flag_hint(self) -> None:
        m = MoodState()
        s = _format_mood_context(m, "chat_sexual_advance", {"force_deflect"})
        assert "force_deflect" in s
        assert "né" in s or "gạ" in s

    def test_unknown_flag_fallback(self) -> None:
        m = MoodState()
        s = _format_mood_context(m, None, {"force_unknown"})
        assert "force_unknown" in s


class TestBuildRequestWithMood:
    def test_returns_llm_request_with_context_message(self, pm: PromptManager) -> None:
        req = pm.build_request_with_mood(
            request_id="r1",
            user_text="cậu ơi",
            current_mood=MoodState(vui=6, buc=4),
            event_category="chat_mention_direct",
        )
        # persona (system) + context (system) + user = 3 messages
        assert len(req.messages) == 3
        assert req.messages[0].role == "system"     # persona
        assert req.messages[1].role == "system"     # context mood
        assert "[Context" in req.messages[1].content
        assert "current_mood" not in req.messages[1].content   # Mood→Style: bỏ số thô
        assert req.messages[2].role == "user"
        assert req.messages[2].content == "cậu ơi"

    def test_history_placed_between_context_and_user(self, pm: PromptManager) -> None:
        pm.commit_turn("cũ 1", "trả lời 1")
        pm.commit_turn("cũ 2", "trả lời 2")
        req = pm.build_request_with_mood(
            request_id="r2",
            user_text="mới",
            current_mood=MoodState(vui=5),
        )
        # persona + context + 4 history + 1 user = 7
        assert len(req.messages) == 7
        assert req.messages[2].content == "cũ 1"
        assert req.messages[-1].content == "mới"

    def test_tone_flag_present_in_context(self, pm: PromptManager) -> None:
        req = pm.build_request_with_mood(
            request_id="r",
            user_text="mình buồn",
            current_mood=MoodState(buon=6),
            event_category="chat_genuine_sad_share",
            tone_flags={"force_gentle_tone"},
        )
        assert "force_gentle_tone" in req.messages[1].content

    def test_default_max_tokens_temperature(self, pm: PromptManager) -> None:
        req = pm.build_request_with_mood(
            request_id="r",
            user_text="hi",
            current_mood=MoodState(),
        )
        assert req.max_tokens == pm._default_max_tokens
        assert req.temperature == pm._default_temperature

    def test_override_max_tokens_and_temperature(self, pm: PromptManager) -> None:
        req = pm.build_request_with_mood(
            request_id="r",
            user_text="hi",
            current_mood=MoodState(),
            max_tokens=99,
            temperature=0.5,
        )
        assert req.max_tokens == 99
        assert req.temperature == 0.5

    def test_does_not_mutate_history(self, pm: PromptManager) -> None:
        pm.commit_turn("u", "a")
        before = len(pm.history())
        pm.build_request_with_mood("r", "test", MoodState())
        assert len(pm.history()) == before


class TestPersonaFileHasMoodInstruction:
    """Verify persona_system.txt CHỨA hướng dẫn Phase 7.5."""

    def test_persona_has_mood_context_directive(self) -> None:
        persona = REPO_ROOT / "config" / "prompts" / "persona_system.txt"
        text = persona.read_text(encoding="utf-8")
        # Cần có mention Context/mood được giao
        assert "Context" in text or "mood ĐƯỢC GIAO" in text
        assert "mood engine" in text.lower() or "ĐƯỢC GIAO" in text
