"""Test LLMTurnRunner — turn qua fallback chain (ARCHITECTURE 8.7.1, 1.E)."""
from __future__ import annotations

import asyncio
import random

import pytest

from interfaces.llm import LLMToken
from orchestrator.fallback_manager import FallbackManager
from services.llm.canned_response import CannedResponder
from services.llm.llm_turn import LLMTurnRunner
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager

VALID = ["Chào cậu.", "\n\n[vui:5 buon:0 buc:0 bon_chon:0 nguong:0]", "\nlý do: x\ncòn nữa: không"]


class FakeLLM:
    def __init__(self, tokens=None, raise_exc=None, delay=0.0):
        self._tokens = tokens or []
        self._raise = raise_exc
        self._delay = delay

    async def generate_stream(self, request):
        if self._raise is not None:
            raise self._raise
        for t in self._tokens:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield LLMToken(request_id=request.request_id, token=t, is_final=False)
        yield LLMToken(request_id=request.request_id, token="", is_final=True)


def make_runner(fake, responses=None, timeout_primary=5.0, timeout_canned=0.5):
    pm = PromptManager(PromptCache("persona test"), max_history_turns=4)
    fb = FallbackManager()
    canned = CannedResponder(responses or {"default": ["CANNED"]}, rng=random.Random(0))
    seen: list[str] = []
    runner = LLMTurnRunner(
        fake, pm, fb, canned,
        timeout_primary_s=timeout_primary, timeout_canned_s=timeout_canned,
        on_token=seen.append,
    )
    return runner, pm, canned, seen


class TestHistoryUserText:
    async def test_history_uses_history_text_not_prompt(self) -> None:
        # TASK 5: prompt có ngoặc nhưng history/commit dùng text chat gốc
        runner, pm, canned, seen = make_runner(FakeLLM(VALID))
        await runner.run_turn(
            "r1", user_text="[Mấy người cùng hỏi: X / Y]",
            history_user_text="chơi game gì thế",
        )
        hist = [m.content for m in pm.history()]
        assert "chơi game gì thế" in hist
        assert not any("Mấy người cùng hỏi" in c for c in hist)

    async def test_commit_history_false_skips_history(self) -> None:
        # TASK 5: SUMMARY/VIBE → commit_history=False → history KHÔNG thêm turn
        runner, pm, canned, seen = make_runner(FakeLLM(VALID))
        await runner.run_turn("r1", user_text="[chat trôi nhanh]", commit_history=False)
        assert pm.history() == []

    async def test_default_backward_compat(self) -> None:
        # Không truyền → dùng user_text như cũ
        runner, pm, canned, seen = make_runner(FakeLLM(VALID))
        await runner.run_turn("r1", "câu chào bình thường")
        assert any("câu chào bình thường" in m.content for m in pm.history())


class TestPrimarySuccess:
    async def test_returns_parsed_level0(self) -> None:
        runner, pm, canned, seen = make_runner(FakeLLM(VALID))
        parsed, level = await runner.run_turn("r1", "chào")
        assert level == 0
        assert parsed.ok is True
        assert parsed.text == "Chào cậu."
        assert parsed.mood.vui == 5

    async def test_streams_tokens_not_canned(self) -> None:
        runner, *_rest, seen = make_runner(FakeLLM(VALID))
        await runner.run_turn("r1", "chào")
        assert "".join(seen).startswith("Chào cậu.")
        assert "CANNED" not in "".join(seen)

    async def test_commits_cleaned_text_to_history(self) -> None:
        runner, pm, *_ = make_runner(FakeLLM(VALID))
        await runner.run_turn("r1", "chào")
        hist = pm.history()
        assert [h.role for h in hist] == ["user", "assistant"]
        assert hist[0].content == "chào"
        assert hist[1].content == "Chào cậu."  # mood block ĐÃ bị tách

    async def test_updates_canned_mood_on_success(self) -> None:
        # sau turn ok (vui=5) → canned dùng mood vui
        runner, pm, canned, _ = make_runner(
            FakeLLM(VALID), responses={"default": ["D"], "vui": ["V"]}
        )
        await runner.run_turn("r1", "chào")
        assert canned.pick() == "V"


class TestFallbackToCanned:
    async def test_primary_raises_falls_to_canned(self) -> None:
        runner, pm, canned, seen = make_runner(FakeLLM(raise_exc=RuntimeError("boom")))
        parsed, level = await runner.run_turn("r1", "chào")
        assert level == 1
        assert parsed.text == "CANNED"
        assert parsed.raw == "<canned>"
        assert "CANNED" in "".join(seen)

    async def test_primary_timeout_falls_to_canned(self) -> None:
        # nhiều token + delay, timeout primary cực ngắn → wait_for timeout → canned
        slow = FakeLLM(tokens=["a"] * 50, delay=0.05)
        runner, *_ = make_runner(slow, timeout_primary=0.02)
        parsed, level = await runner.run_turn("r1", "chào")
        assert level == 1
        assert parsed.text == "CANNED"

    async def test_canned_not_update_mood_on_fail(self) -> None:
        # turn canned (primary raise) → mood không được set → pick vẫn default
        runner, pm, canned, _ = make_runner(
            FakeLLM(raise_exc=RuntimeError("x")), responses={"default": ["D"], "vui": ["V"]}
        )
        await runner.run_turn("r1", "chào")
        assert canned.pick() == "D"

    async def test_history_committed_even_on_canned(self) -> None:
        runner, pm, *_ = make_runner(FakeLLM(raise_exc=RuntimeError("x")))
        await runner.run_turn("r1", "chào")
        assert pm.history()[-1].content == "CANNED"


class TestPrimaryTextOnly:
    async def test_text_only_output_is_ok_after_a1(self) -> None:
        # A1: text non-empty → ok=True, mood=neutral. Không có mood block cũng OK.
        runner, pm, canned, _ = make_runner(FakeLLM(["chỉ có text, không mood block"]))
        parsed, level = await runner.run_turn("r1", "chào")
        assert level == 0
        assert parsed.ok is True
        assert parsed.text == "chỉ có text, không mood block"
        # mood neutral → không update canned (A1: chỉ update khi có tín hiệu mood).
        assert canned.pick() == "CANNED"


class TestFromLoader:
    def test_registers_chain_with_config_timeouts(self) -> None:
        from pathlib import Path

        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(Path(__file__).resolve().parents[2] / "config")
        loader.load_all()
        pm = PromptManager.from_loader(loader)
        fb = FallbackManager()
        canned = CannedResponder.from_loader(loader)
        LLMTurnRunner.from_loader(loader, FakeLLM(VALID), pm, fb, canned)
        assert fb.has_chain("llm")
