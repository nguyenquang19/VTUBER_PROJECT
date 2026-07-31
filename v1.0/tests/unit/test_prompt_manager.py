"""Test PromptCache + PromptManager (ARCHITECTURE 8.2, 1.C)."""
from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.llm import ChatMessage
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
        # file thật config/prompts/persona_system.txt phải load + có nội dung persona
        c = PromptCache.from_file(REPO_ROOT / "config" / "prompts" / "persona_system.txt")
        assert "Mai" in c.text
        assert "vui:N" in c.text  # có khuôn mood block


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
