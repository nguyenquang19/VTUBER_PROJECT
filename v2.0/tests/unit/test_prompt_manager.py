"""Test PromptCache + PromptManager (ARCHITECTURE 8.2, 1.C)."""
from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.llm import ChatMessage
from orchestrator.config_loader import ConfigLoader
from services.llm.prompt_cache import PromptCache, PromptCacheError
from services.llm.prompt_manager import PromptManager

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONA = "Bạn là Mai, một AI VTuber. Xưng 'tớ', gọi 'cậu'."


def cache(text: str = PERSONA) -> PromptCache:
    return PromptCache(text)


def mgr(**kw) -> PromptManager:
    kw.setdefault("max_history_turns", 2)
    return PromptManager(cache(), **kw)


class TestPromptCache:
    def test_holds_text_and_version(self) -> None:
        c = cache()
        assert c.text == PERSONA
        assert len(c.version) == 12

    def test_strips_whitespace(self) -> None:
        assert cache("  xin chào  \n").text == "xin chào"

    def test_empty_raises(self) -> None:
        with pytest.raises(PromptCacheError):
            cache("   \n  ")

    def test_version_stable_for_same_text(self) -> None:
        assert cache().version == cache().version

    def test_version_changes_with_text(self) -> None:
        assert cache("A").version != cache("B").version

    def test_as_message_is_system(self) -> None:
        m = cache().as_message()
        assert isinstance(m, ChatMessage)
        assert m.role == "system"
        assert m.content == PERSONA

    def test_from_file(self, tmp_path: Path) -> None:
        p = tmp_path / "persona.txt"
        p.write_text("persona test nội dung", encoding="utf-8")
        assert PromptCache.from_file(p).text == "persona test nội dung"

    def test_from_file_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PromptCacheError):
            PromptCache.from_file(tmp_path / "khong_ton_tai.txt")

    def test_real_config_file_loads(self) -> None:
        # A1: persona đã BỎ mood block instruction. Verify KHÔNG còn khuôn.
        c = PromptCache.from_file(REPO_ROOT / "config" / "prompts" / "persona_system.txt")
        assert "Mai" in c.text
        assert "vui:N" not in c.text
        assert "Định dạng trả lời BẮT BUỘC" not in c.text
        # A5: persona có rào chống bịa
        assert "KHÔNG BỊA" in c.text

    def test_from_loader_combines_persona_and_lore(self, tmp_path: Path) -> None:
        persona = tmp_path / "persona.txt"
        lore = tmp_path / "lore.txt"
        persona.write_text("persona ổn định", encoding="utf-8")
        lore.write_text("lore của Mai", encoding="utf-8")

        class Loader:
            def get(self, name: str, key: str, default=None):
                values = {
                    ("models", "llm_main.persona_prompt_path"): str(persona),
                    ("models", "llm_main.lore_prompt_path"): str(lore),
                }
                return values.get((name, key), default)

        combined = PromptCache.from_loader(Loader())
        persona_only = PromptCache("persona ổn định")
        assert combined.text == "persona ổn định\n\nlore của Mai"
        assert combined.version != persona_only.version

    def test_from_loader_missing_or_empty_lore_falls_back_to_persona(
        self, tmp_path: Path,
    ) -> None:
        persona = tmp_path / "persona.txt"
        persona.write_text("persona ổn định", encoding="utf-8")

        class Loader:
            def __init__(self, lore_path: Path) -> None:
                self.lore_path = lore_path

            def get(self, name: str, key: str, default=None):
                if (name, key) == ("models", "llm_main.persona_prompt_path"):
                    return str(persona)
                if (name, key) == ("models", "llm_main.lore_prompt_path"):
                    return str(self.lore_path)
                return default

        missing = PromptCache.from_loader(Loader(tmp_path / "missing.txt"))
        empty_path = tmp_path / "empty.txt"
        empty_path.write_text("  \n", encoding="utf-8")
        empty = PromptCache.from_loader(Loader(empty_path))
        assert missing.text == "persona ổn định"
        assert empty.text == "persona ổn định"

    def test_from_loader_missing_persona_fails_fast(self, tmp_path: Path) -> None:
        class Loader:
            def get(self, name: str, key: str, default=None):
                if (name, key) == ("models", "llm_main.persona_prompt_path"):
                    return str(tmp_path / "missing.txt")
                return default

        with pytest.raises(PromptCacheError, match="không thấy persona prompt"):
            PromptCache.from_loader(Loader())

    def test_real_loader_paths_do_not_depend_on_process_cwd(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        monkeypatch.chdir(tmp_path)

        combined = PromptCache.from_loader(loader)

        assert "KHÔNG BỊA" in combined.text
        assert "đội quân" in combined.text


class TestBuildMessages:
    def test_first_turn_system_then_user(self) -> None:
        m = mgr()
        msgs = m.build_messages("chào Mai")
        assert [x.role for x in msgs] == ["system", "user"]
        assert msgs[0].content == PERSONA
        assert msgs[-1].content == "chào Mai"

    def test_build_does_not_mutate_history(self) -> None:
        m = mgr()
        m.build_messages("hỏi 1")
        m.build_messages("hỏi 2")
        assert m.history() == []  # build thuần, không ghi

    def test_history_inserted_between_system_and_user(self) -> None:
        m = mgr()
        m.commit_turn("u1", "a1")
        msgs = m.build_messages("u2")
        assert [x.role for x in msgs] == ["system", "user", "assistant", "user"]
        assert msgs[1].content == "u1"
        assert msgs[2].content == "a1"
        assert msgs[3].content == "u2"


class TestHistoryWindow:
    def test_commit_appends_pair(self) -> None:
        m = mgr()
        m.commit_turn("u1", "a1")
        assert [x.content for x in m.history()] == ["u1", "a1"]

    def test_trims_to_max_turns(self) -> None:
        m = mgr(max_history_turns=2)  # giữ 2 cặp = 4 message
        for i in range(4):
            m.commit_turn(f"u{i}", f"a{i}")
        hist = m.history()
        assert len(hist) == 4
        assert [x.content for x in hist] == ["u2", "a2", "u3", "a3"]

    def test_reset_clears(self) -> None:
        m = mgr()
        m.commit_turn("u", "a")
        m.reset()
        assert m.history() == []

    def test_zero_history_keeps_nothing(self) -> None:
        m = mgr(max_history_turns=0)
        m.commit_turn("u", "a")
        assert m.history() == []
        # build vẫn chỉ có system + user
        assert [x.role for x in m.build_messages("x")] == ["system", "user"]

    def test_negative_history_rejected(self) -> None:
        with pytest.raises(ValueError):
            PromptManager(cache(), max_history_turns=-1)

    def test_character_budget_keeps_latest_complete_turn(self) -> None:
        m = mgr(max_history_turns=10, history_max_chars=10)
        m.commit_turn("user-one", "answer-one")
        m.commit_turn("u2", "answer2")
        history = m.history()
        assert [item.content for item in history] == ["u2", "answer2"]
        assert sum(len(item.content) for item in history) <= 10


class TestCommitSelfTalk:
    def test_appends_lone_assistant(self) -> None:
        m = mgr()
        m.commit_self_talk("tớ thấy hơi bị bỏ rơi")
        hist = m.history()
        assert len(hist) == 1
        assert hist[0].role == "assistant"
        assert hist[0].content == "tớ thấy hơi bị bỏ rơi"

    def test_self_talk_visible_in_next_request(self) -> None:
        # BUG A6: chat đáp lại self-talk phải thấy self-talk trong history.
        m = mgr()
        m.commit_self_talk("tớ thấy bị bỏ rơi")
        msgs = m.build_messages("ai dám bỏ rơi cậu")
        contents = [x.content for x in msgs]
        assert "tớ thấy bị bỏ rơi" in contents          # Mai nhớ mình vừa nói
        assert "ai dám bỏ rơi cậu" in contents

    def test_consecutive_self_talk_merges_no_double_assistant(self) -> None:
        # 2 assistant liền → vỡ Gemma template. Phải merge thành 1.
        m = mgr()
        m.commit_self_talk("câu tự nói 1")
        m.commit_self_talk("câu tự nói 2")
        hist = m.history()
        roles = [x.role for x in hist]
        assert roles == ["assistant"]                   # merge, không phải 2
        assert "câu tự nói 1" in hist[0].content
        assert "câu tự nói 2" in hist[0].content

    def test_after_reply_new_self_talk_is_separate(self) -> None:
        m = mgr()
        m.commit_self_talk("tự nói A")
        m.commit_turn("chat hỏi", "Mai đáp")            # user + assistant
        m.commit_self_talk("tự nói B")                  # sau assistant(đáp) → merge vào đó
        roles = [x.role for x in m.history()]
        # không có 2 assistant liền nhau bất kỳ đâu
        for i in range(1, len(roles)):
            assert not (roles[i] == "assistant" and roles[i - 1] == "assistant")

    def test_cap_bounds_merged_length(self) -> None:
        m = mgr(self_talk_history_char_cap=50)
        for _ in range(20):
            m.commit_self_talk("x" * 40)
        hist = m.history()
        assert len(hist) == 1
        assert len(hist[0].content) <= 50               # cap giữ, không bloat

    def test_cap_zero_disables(self) -> None:
        m = mgr(self_talk_history_char_cap=0)
        m.commit_self_talk("không ghi")
        assert m.history() == []

    def test_empty_text_noop(self) -> None:
        m = mgr()
        m.commit_self_talk("   ")
        assert m.history() == []


class TestMoodStyleInPrompt:
    def _mgr_with_style(self):
        from orchestrator.config_loader import ConfigLoader
        from services.emotion.mood_style import MoodStyleTable
        from services.llm.prompt_manager import PromptManager
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        style = MoodStyleTable.from_loader(loader)
        return PromptManager(cache(), max_history_turns=2, mood_style=style)

    def test_buc_high_injects_directive_no_raw_numbers(self) -> None:
        from interfaces.animation import MoodState
        m = self._mgr_with_style()
        req = m.build_request_with_mood("r", "gì thế", MoodState(buc=9))
        ctx = req.messages[1].content
        assert "cộc" in ctx or "gắt" in ctx      # directive chữ
        assert "current_mood" not in ctx          # bỏ số thô
        assert "buc=" not in ctx and "event_category" not in ctx

    def test_gentle_flag_beats_mood_style(self) -> None:
        from interfaces.animation import MoodState
        m = self._mgr_with_style()
        req = m.build_request_with_mood(
            "r", "buồn quá", MoodState(buc=9),
            tone_flags={"force_gentle_tone"},
        )
        ctx = req.messages[1].content
        assert "force_gentle_tone" in ctx        # hint gentle có
        assert "cộc" not in ctx and "gắt" not in ctx   # directive bực bị chặn

    def test_baseline_no_directive(self) -> None:
        from interfaces.animation import MoodState
        m = self._mgr_with_style()
        req = m.build_request_with_mood("r", "chào", MoodState(vui=5))
        ctx = req.messages[1].content
        # gần baseline → không directive, chỉ header
        assert "Đang" not in ctx or "cực kỳ" not in ctx


class TestStageDirection:
    def test_stage_direction_in_system_user_turn_clean(self) -> None:
        # De-AI register: chỉ thị "gộp" ở SYSTEM, user turn = chat thật
        from interfaces.animation import MoodState
        from services.llm.prompt_manager import PromptManager
        m = PromptManager(cache(), max_history_turns=2)
        req = m.build_request_with_mood(
            "r", user_text="chơi game gì thế / mai mấy tuổi",
            current_mood=MoodState(),
            stage_direction="Mấy người cùng hỏi — đáp GỘP 1 lần.",
        )
        system_ctx = req.messages[1].content   # system context
        user_turn = req.messages[-1].content   # user
        assert "đáp GỘP" in system_ctx          # chỉ thị ở system
        assert "GỘP" not in user_turn           # user turn sạch
        assert user_turn == "chơi game gì thế / mai mấy tuổi"


class TestBuildRequest:
    def test_defaults_applied(self) -> None:
        m = mgr(default_max_tokens=222, default_temperature=0.5)
        req = m.build_request("r1", "chào")
        assert req.request_id == "r1"
        assert req.max_tokens == 222
        assert req.temperature == 0.5
        assert req.to_messages()[0]["role"] == "system"
        assert req.to_messages()[-1] == {"role": "user", "content": "chào"}

    def test_overrides(self) -> None:
        m = mgr()
        req = m.build_request("r2", "hi", max_tokens=10, temperature=0.1)
        assert req.max_tokens == 10
        assert req.temperature == 0.1


class TestFromLoader:
    def test_reads_config_and_real_persona(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = PromptManager.from_loader(loader)
        assert len(m.version) == 12
        msgs = m.build_messages("test")
        assert "Mai" in msgs[0].content  # persona thật được nạp
        # Config có thể tune (2026-07-31: hạ 12→10 vì GPU chia với TTS).
        # Kiểm là số dương hợp lý, không hard-code giá trị cụ thể → dễ tune sau.
        assert 4 <= m._max_history_turns <= 20
