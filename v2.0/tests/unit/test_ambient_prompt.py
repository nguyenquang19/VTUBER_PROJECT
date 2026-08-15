"""Test ambient content gen 7.9.4 (Phase 2, 2.C)."""
from __future__ import annotations

from pathlib import Path

from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager

REPO_ROOT = Path(__file__).resolve().parents[2]
TPL = "[im lặng {silence} phút, mood {mood}]\nTự mở lời đi."


def mgr(ambient_template=TPL, **kw) -> PromptManager:
    kw.setdefault("max_history_turns", 4)
    return PromptManager(PromptCache("persona test"), ambient_template=ambient_template, **kw)


class TestBuildAmbient:
    def test_structure_system_then_user(self) -> None:
        req = mgr().build_ambient_request("amb1", silence_seconds=120)
        msgs = req.to_messages()
        assert msgs[0]["role"] == "system"
        assert msgs[-1]["role"] == "user"

    def test_fills_silence_minutes(self) -> None:
        req = mgr().build_ambient_request("amb1", silence_seconds=90)  # 1.5 phút
        assert "1.5 phút" in req.to_messages()[-1]["content"]

    def test_fills_mood(self) -> None:
        req = mgr().build_ambient_request("amb1", silence_seconds=60, mood="bực")
        assert "mood bực" in req.to_messages()[-1]["content"]

    def test_mood_default_when_empty(self) -> None:
        req = mgr().build_ambient_request("amb1", silence_seconds=60)
        assert "bình thường" in req.to_messages()[-1]["content"]

    def test_includes_history(self) -> None:
        m = mgr()
        m.commit_turn("u1", "a1")
        msgs = m.build_ambient_request("amb1", silence_seconds=60).to_messages()
        assert [x["role"] for x in msgs] == ["system", "user", "assistant", "user"]
        assert msgs[1]["content"] == "u1"

    def test_does_not_mutate_history(self) -> None:
        m = mgr()
        m.build_ambient_request("amb1", silence_seconds=60)
        assert m.history() == []

    def test_uses_default_tokens_temp(self) -> None:
        m = mgr(default_max_tokens=111, default_temperature=0.4)
        req = m.build_ambient_request("amb1", silence_seconds=60)
        assert req.max_tokens == 111
        assert req.temperature == 0.4


class TestFromLoader:
    def test_loads_real_ambient_template(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = PromptManager.from_loader(loader)
        req = m.build_ambient_request("amb1", silence_seconds=120, mood="vui")
        content = req.to_messages()[-1]["content"]
        assert "2.0 phút" in content
        assert "vui" in content
        assert "mood block" in content  # template thật nhắc kèm mood block


class TestFallbackTemplate:
    def test_default_template_when_none(self) -> None:
        m = PromptManager(PromptCache("p"), ambient_template=None)
        req = m.build_ambient_request("amb1", silence_seconds=60)
        assert "im lặng" in req.to_messages()[-1]["content"]
