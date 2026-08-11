"""Test FilterRegenerator (Phase 3, 3.B)."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from interfaces.filter import FilterCategory, FilterVerdict
from interfaces.llm import ChatMessage, LLMRequest, LLMToken
from orchestrator.fallback_manager import FallbackManager
from services.filter.regenerator import FilterRegenerator
from services.filter.rule_filter import RuleFilter
from services.llm.canned_response import CannedResponder
from services.llm.llm_turn import LLMTurnRunner
from services.llm.parser import parse_response
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager

REPO_ROOT = Path(__file__).resolve().parents[2]

CLEAN_BODY = (
    "Chào cậu.\n\n"
    "[vui:5 buon:0 buc:0 bon_chon:0 nguong:0]\n"
    "lý do: x\ncòn nữa: không"
)
HEDGE_BODY = (
    "Tớ chỉ là một chương trình thôi, tớ không có cảm xúc gì cả.\n\n"
    "[vui:0 buon:0 buc:0 bon_chon:0 nguong:0]\n"
    "lý do: x\ncòn nữa: không"
)

# tối thiểu 1 pattern để RuleFilter bắt HEDGE_BODY
FILTER_PATTERNS = {
    "persona_break": ["không có cảm xúc", "tớ chỉ là (một )?chương trình"],
    "harmful": ["tự tử"],
}
FILTER_SEVERITY = {"persona_break": "medium", "harmful": "high"}
FILTER_ACTION = {"persona_break": "regenerate", "harmful": "block"}


class ScriptedLLM:
    """Trả body có sẵn theo lần gọi (index)."""

    def __init__(self, bodies: list[str]) -> None:
        self.bodies = list(bodies)
        self.calls = 0
        self.seen_requests: list[LLMRequest] = []

    async def generate_stream(self, request: LLMRequest):
        self.seen_requests.append(request)
        body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        yield LLMToken(request_id=request.request_id, token=body, is_final=False)
        yield LLMToken(request_id=request.request_id, token="", is_final=True)


def make_filter(**over) -> RuleFilter:
    kw = dict(patterns=FILTER_PATTERNS, severity=FILTER_SEVERITY, action=FILTER_ACTION)
    kw.update(over)
    return RuleFilter(**kw)


def base_request(user_text: str = "chào") -> LLMRequest:
    return LLMRequest(
        request_id="r1",
        messages=[
            ChatMessage(role="system", content="persona test"),
            ChatMessage(role="user", content=user_text),
        ],
        max_tokens=100,
        temperature=0.7,
    )


class TestPassThrough:
    async def test_clean_output_no_regen(self) -> None:
        llm = ScriptedLLM([CLEAN_BODY])
        regen = FilterRegenerator(make_filter(), llm, max_attempts=1)
        parsed = parse_response(CLEAN_BODY)
        final, verdict = await regen.check_and_maybe_regen(base_request(), parsed)
        assert verdict.passed is True
        assert final.text == parsed.text
        assert llm.calls == 0  # không gọi lại LLM
        m = regen.get_metrics()
        assert m["filter_regen_attempts_total"] == 0

    async def test_non_regenerate_action_no_retry(self) -> None:
        # harmful → action=block, không regen (chỉ regen cho action=regenerate)
        llm = ScriptedLLM([CLEAN_BODY])
        regen = FilterRegenerator(make_filter(), llm, max_attempts=3)
        parsed = parse_response(
            "cậu nên đi tự tử đi\n\n[vui:0 buon:0 buc:0 bon_chon:0 nguong:0]"
        )
        final, verdict = await regen.check_and_maybe_regen(base_request(), parsed)
        assert verdict.passed is False
        assert verdict.suggested_action == "block"
        assert llm.calls == 0                     # không regen block
        assert regen.get_metrics()["filter_regen_attempts_total"] == 0

    async def test_request_context_reaches_identity_guard(self) -> None:
        guard = {
            "foreign_names": ["anami"],
            "first_person_patterns": [r"\bnếu tớ\b"],
        }
        llm = ScriptedLLM([CLEAN_BODY])
        regen = FilterRegenerator(
            make_filter(identity_guard=guard), llm, max_attempts=1,
        )
        bad = parse_response("Nếu tớ có thân xác thì tớ sẽ đi ăn.")

        final, verdict = await regen.check_and_maybe_regen(
            base_request("Nếu Anami có thân xác thì làm gì?"), bad,
        )

        assert llm.calls == 1
        assert final.text == "Chào cậu."
        assert verdict.passed is True


class TestRegenerateRecovered:
    async def test_recovers_on_second_attempt(self) -> None:
        # lần 1 (là parsed đưa vào — đã hedge) → filter fail → regen lần 2 trả CLEAN
        llm = ScriptedLLM([CLEAN_BODY])
        regen = FilterRegenerator(make_filter(), llm, max_attempts=1)
        parsed = parse_response(HEDGE_BODY)
        final, verdict = await regen.check_and_maybe_regen(base_request(), parsed)
        assert verdict.passed is True
        assert final.text == "Chào cậu."
        assert llm.calls == 1
        assert regen.last_initial_verdict is not None
        assert regen.last_initial_verdict.passed is False
        assert FilterCategory.PERSONA_BREAK in regen.last_initial_verdict.categories_hit
        m = regen.get_metrics()
        assert m["filter_regen_attempts_total"] == 1
        assert m["filter_regen_recovered_total"] == 1
        assert m["filter_regen_exhausted_total"] == 0

    async def test_hint_request_has_correct_structure(self) -> None:
        llm = ScriptedLLM([CLEAN_BODY])
        regen = FilterRegenerator(make_filter(), llm, max_attempts=1)
        orig = base_request(user_text="chào Mai")
        parsed = parse_response(HEDGE_BODY)
        await regen.check_and_maybe_regen(orig, parsed)

        assert len(llm.seen_requests) == 1
        req = llm.seen_requests[0]
        msgs = req.to_messages()
        # nối đúng thứ tự: system, user, assistant(bad), user(hint)
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
        assert msgs[2]["content"].startswith("Tớ chỉ là một chương trình")
        assert "[Kiểm duyệt]" in msgs[-1]["content"]
        assert "persona" in msgs[-1]["content"].lower()
        assert "CHỈ viết câu Mai sẽ nói" in msgs[-1]["content"]
        assert "vẫn kèm mood block" not in msgs[-1]["content"]
        assert req.request_id.startswith("r1-r")
        assert req.max_tokens == orig.max_tokens


class TestExhausted:
    async def test_gives_up_after_max_attempts(self) -> None:
        # LLM trả HEDGE mãi → không recover được
        llm = ScriptedLLM([HEDGE_BODY, HEDGE_BODY, HEDGE_BODY])
        regen = FilterRegenerator(make_filter(), llm, max_attempts=2)
        parsed = parse_response(HEDGE_BODY)
        final, verdict = await regen.check_and_maybe_regen(base_request(), parsed)
        assert verdict.passed is False
        assert FilterCategory.PERSONA_BREAK in verdict.categories_hit
        assert llm.calls == 2
        m = regen.get_metrics()
        assert m["filter_regen_attempts_total"] == 2
        assert m["filter_regen_recovered_total"] == 0
        assert m["filter_regen_exhausted_total"] == 1

    async def test_max_attempts_zero_no_regen(self) -> None:
        llm = ScriptedLLM([CLEAN_BODY])
        regen = FilterRegenerator(make_filter(), llm, max_attempts=0)
        parsed = parse_response(HEDGE_BODY)
        final, verdict = await regen.check_and_maybe_regen(base_request(), parsed)
        assert verdict.passed is False        # verdict đúng
        assert llm.calls == 0
        assert regen.get_metrics()["filter_regen_exhausted_total"] == 1

    def test_negative_max_attempts_rejected(self) -> None:
        with pytest.raises(ValueError):
            FilterRegenerator(make_filter(), ScriptedLLM([]), max_attempts=-1)


class TestFailSafe:
    async def test_filter_error_fails_open(self) -> None:
        class BadFilter:
            async def check(self, text, ctx=None):
                raise RuntimeError("boom")

        regen = FilterRegenerator(BadFilter(), ScriptedLLM([CLEAN_BODY]), max_attempts=1)
        parsed = parse_response(HEDGE_BODY)
        final, verdict = await regen.check_and_maybe_regen(base_request(), parsed)
        assert verdict.passed is True
        assert "fail-open" in verdict.reason

    async def test_regen_llm_error_returns_previous(self) -> None:
        class BadLLM:
            async def generate_stream(self, request):
                raise RuntimeError("llm dead")
                yield  # unreachable — cần để hàm là async gen

        regen = FilterRegenerator(make_filter(), BadLLM(), max_attempts=1)
        parsed = parse_response(HEDGE_BODY)
        final, verdict = await regen.check_and_maybe_regen(base_request(), parsed)
        assert final.text == parsed.text        # giữ bản trước
        assert verdict.passed is True           # fail-open
        assert "fail-open" in verdict.reason


class TestFromLoader:
    def test_reads_config_max_attempts(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        regen = FilterRegenerator.from_loader(loader, make_filter(), ScriptedLLM([CLEAN_BODY]))
        # config đặt 1
        assert regen._max_attempts == 2


class TestLLMTurnRunnerIntegration:
    """Wire regenerator vào LLMTurnRunner — không phá test cũ, xử đúng khi có."""

    def _make_runner(self, llm, regen=None):
        pm = PromptManager(PromptCache("persona test"), max_history_turns=4)
        fb = FallbackManager()
        canned = CannedResponder({"default": ["..."]}, rng=random.Random(0))
        return LLMTurnRunner(
            llm, pm, fb, canned,
            timeout_primary_s=5.0, timeout_canned_s=0.5,
            regenerator=regen,
        ), pm

    async def test_no_regenerator_backward_compat(self) -> None:
        llm = ScriptedLLM([CLEAN_BODY])
        runner, _pm = self._make_runner(llm, regen=None)
        parsed, level = await runner.run_turn("t1", "chào")
        assert level == 0
        assert parsed.text == "Chào cậu."
        assert runner.last_filter_verdict is None

    async def test_regen_replaces_bad_output(self) -> None:
        llm = ScriptedLLM([HEDGE_BODY, CLEAN_BODY])
        regen = FilterRegenerator(make_filter(), llm, max_attempts=1)
        runner, pm = self._make_runner(llm, regen=regen)
        parsed, level = await runner.run_turn("t1", "chào")
        assert level == 0
        assert parsed.text == "Chào cậu."               # regen thay HEDGE
        assert runner.last_filter_verdict.passed is True
        assert pm.history()[-1].content == "Chào cậu."  # history commit bản clean

    async def test_regen_exhausted_commits_last_bad(self) -> None:
        llm = ScriptedLLM([HEDGE_BODY, HEDGE_BODY])
        regen = FilterRegenerator(make_filter(), llm, max_attempts=1)
        runner, pm = self._make_runner(llm, regen=regen)
        parsed, level = await runner.run_turn("t1", "chào")
        assert level == 0
        assert runner.last_filter_verdict.passed is False   # vẫn fail sau regen
        # history vẫn được commit (fail-safe: không silent-drop turn)
        assert len(pm.history()) == 2
