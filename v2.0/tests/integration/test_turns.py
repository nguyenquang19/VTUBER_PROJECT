"""Integration Phase 1 (ARCHITECTURE 11.2 DoD, milestone 1.F).

Mock LLM (không cần server) để kiểm:
- 100 turn không crash (DoD)
- parse mood block >95% (DoD)
- fallback triggered khi force timeout (DoD)
- metrics ghi đúng, history trim ổn định

Live parse-rate trên model thật ở test_llm_live.py (marker llm) + user chạy cli.py
đủ 20+ turn duyệt persona (subjective) ở CHECKPOINT P1.
"""
from __future__ import annotations

import asyncio
import random

from interfaces.llm import LLMToken
from orchestrator.fallback_manager import FallbackManager
from services.operations.metrics import MetricsCollector
from services.llm.canned_response import CannedResponder
from services.llm.llm_turn import LLMTurnRunner
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager


class ScriptedLLM:
    """Trả response theo format persona; malformed_every>0 để chèn câu sai format."""

    def __init__(self, malformed_every: int = 0, delay: float = 0.0):
        self._n = 0
        self._malformed_every = malformed_every
        self._delay = delay

    async def generate_stream(self, request):
        self._n += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._malformed_every and self._n % self._malformed_every == 0:
            body = "Câu này không có mood block."
        else:
            v = self._n % 11
            body = (
                f"Câu trả lời số {self._n}.\n\n"
                f"[vui:{v} buồn:0 bực:1 bồn_chồn:0 ngượng:0]\n"
                f"lý do: test\ncòn nữa: không"
            )
        for ch in (body[:20], body[20:]):
            if ch:
                yield LLMToken(request_id=request.request_id, token=ch, is_final=False)
        yield LLMToken(request_id=request.request_id, token="", is_final=True)

    def get_metrics(self):
        return {"llm_last_ttft_ms": 80.0, "llm_last_decode_tps": 40.0}


def build_runner(llm, timeout_primary=5.0):
    loader_pm = PromptManager(PromptCache("persona test"), max_history_turns=12)
    fb = FallbackManager()
    canned = CannedResponder({"default": ["..."]}, rng=random.Random(0))
    metrics = MetricsCollector()
    runner = LLMTurnRunner(
        llm, loader_pm, fb, canned,
        timeout_primary_s=timeout_primary, timeout_canned_s=0.5, metrics=metrics,
    )
    return runner, loader_pm, metrics


class TestHundredTurns:
    async def test_100_turns_no_crash_all_parse(self) -> None:
        runner, pm, metrics = build_runner(ScriptedLLM())
        for i in range(100):
            parsed, level = await runner.run_turn(f"t{i}", f"câu hỏi {i}")
            assert level == 0
            assert parsed.ok is True
        s = metrics.llm_snapshot()
        assert s["requests_total"] == 100
        assert s["parse_rate_percent"] == 100.0
        assert s["last_ttft_ms"] == 80.0
        # history trim: 12 cặp = 24 message
        assert len(pm.history()) == 24

    async def test_parse_rate_above_95(self) -> None:
        # malformed mỗi 25 lượt → 4/100 sai = 96% ok (>95%)
        runner, _pm, metrics = build_runner(ScriptedLLM(malformed_every=25))
        for i in range(100):
            await runner.run_turn(f"t{i}", "x")
        rate = metrics.llm_snapshot()["parse_rate_percent"]
        assert rate >= 95.0, f"parse rate {rate}% < 95%"


class TestForceTimeoutFallback:
    async def test_timeout_falls_to_canned(self) -> None:
        slow = ScriptedLLM(delay=0.2)
        runner, _pm, metrics = build_runner(slow, timeout_primary=0.02)
        parsed, level = await runner.run_turn("t", "x")
        assert level == 1
        assert parsed.text == "..."
        assert metrics.llm_snapshot()["fallback_total"] == 1

    async def test_many_timeouts_no_crash(self) -> None:
        slow = ScriptedLLM(delay=0.05)
        runner, _pm, metrics = build_runner(slow, timeout_primary=0.01)
        for i in range(20):
            _parsed, level = await runner.run_turn(f"t{i}", "x")
            assert level == 1
        assert metrics.llm_snapshot()["fallback_total"] == 20
