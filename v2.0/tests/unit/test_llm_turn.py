"""Test LLMTurnRunner — turn qua fallback chain (ARCHITECTURE 8.7.1, 1.E)."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

from interfaces.llm import LLMToken
from interfaces.memory import MemoryEntry
from orchestrator.config_loader import ConfigLoader
from orchestrator.fallback_manager import FallbackManager
from services.llm.canned_response import CannedResponder
from services.llm.llm_turn import LLMTurnRunner
from services.llm.parser import parse_response
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager
from services.memory.extractor import MemoryExtractor
from services.memory.config import MemoryRuntimeConfig
from services.memory.recall_gate import RecallGate
from services.cognition.compatibility_context import ConversationContextComposer

VALID = ["Chào cậu.", "\n\n[vui:5 buon:0 buc:0 bon_chon:0 nguong:0]", "\nlý do: x\ncòn nữa: không"]
ROOT = Path(__file__).resolve().parents[2]


class FakeLLM:
    def __init__(self, tokens=None, raise_exc=None, delay=0.0):
        self._tokens = tokens or []
        self._raise = raise_exc
        self._delay = delay
        self.requests = []

    async def generate_stream(self, request):
        self.requests.append(request)
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


class TestDirectedTurn:
    async def test_action_context_is_system_only_and_not_committed(self) -> None:
        fake = FakeLLM(VALID)
        runner, pm, *_ = make_runner(fake)
        parsed = await runner.run_directed_turn("g1", "grounded director action")
        assert parsed.ok is True
        assert pm.history() == []
        request = fake.requests[0]
        assert request.messages[-1].role == "system"
        assert request.messages[-1].content == "grounded director action"
        assert not any(
            message.role == "user" and "grounded director action" in message.content
            for message in request.messages
        )

    async def test_conversation_context_is_injected_as_system_message(self) -> None:
        class State:
            def snapshot(self):
                from interfaces.state import AgentStateSnapshot
                return AgentStateSnapshot()

        class Composer:
            def render(self, _state, query):
                return f"[Conversation continuity] query={query}"

        fake = FakeLLM(VALID)
        runner, _pm, *_ = make_runner(fake)
        runner._agent_state = State()
        runner.set_conversation_context_renderer(Composer())
        await runner.run_turn("c1", "nãy cậu bảo gì")
        request = fake.requests[0]
        assert any(
            message.role == "system" and "[Conversation continuity]" in message.content
            for message in request.messages
        )
        assert request.messages[-1].role == "user"

    async def test_recall_gate_keeps_verbatim_memory_out_of_llm_request_and_output(self) -> None:
        class State:
            def snapshot(self):
                from interfaces.state import AgentStateSnapshot
                return AgentStateSnapshot()

        class Memory:
            async def query(self, _query, *, top_k, viewer_id):
                del top_k, viewer_id
                return [MemoryEntry(
                    entry_id="private-memory",
                    content=raw,
                    timestamp=datetime(2026, 8, 27, tzinfo=timezone.utc),
                    importance=0.9,
                    metadata={"cognitive_kind": "EPISODIC"},
                )]

        raw = "Chuỗi memory nguyên văn tuyệt đối không được lặp lại."
        loader = ConfigLoader(ROOT / "config")
        loader.load_all()
        gate = RecallGate(MemoryRuntimeConfig.from_loader(loader))
        composer = ConversationContextComposer.from_loader(
            loader,
            memory_provider=Memory,
            recall_gate=gate,
            selector_enabled=True,
            clock=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
        fake = FakeLLM(VALID)
        runner, _pm, *_ = make_runner(fake)
        runner._agent_state = State()
        runner.set_conversation_context_renderer(composer)
        parsed, _level = await runner.run_turn("c2", "mình từng nói gì?")
        request_text = "\n".join(message.content for message in fake.requests[0].messages)
        assert "Latent memory hint" in request_text
        assert raw not in request_text
        assert raw not in parsed.text


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

    async def test_deferred_delivery_exposes_context_without_committing_state(self) -> None:
        runner, pm, *_ = make_runner(FakeLLM(VALID))
        await runner.run_turn("deferred", "chào", defer_delivery_commit=True)
        assert pm.history() == []
        pending = runner.pending_delivery_context("deferred")
        assert pending.history_user_text == "chào"
        assert pending.parsed.text == "Chào cậu."
        assert runner.finalize_delivery("deferred", True) is True
        assert pm.history() == []
        assert runner.finalize_delivery("deferred", True) is False

    def test_external_delivery_staging_clears_stale_filter_state(self) -> None:
        fake = FakeLLM(VALID)
        runner, pm, *_ = make_runner(fake)
        runner.last_filter_verdict = type(
            "Rejected", (), {"passed": False},
        )()
        parsed = parse_response("Câu Brain đã grounded.")

        runner.stage_external_delivery(
            request_id="brain-public",
            parsed=parsed,
            user_text="Mai trả lời nhé",
            viewer_id="raw-viewer",
            trigger_type="cognitive_brain_public",
        )

        assert runner.last_filter_verdict is None
        assert fake.requests == []
        pending = runner.pending_delivery_context("brain-public")
        assert pending.parsed.text == "Câu Brain đã grounded."
        assert pending.history_user_text == "Mai trả lời nhé"
        assert pending.viewer_id == "raw-viewer"
        assert pm.history() == []

    async def test_failed_deferred_delivery_discards_history(self) -> None:
        runner, pm, *_ = make_runner(FakeLLM(VALID))
        await runner.run_turn("failed", "chào", defer_delivery_commit=True)
        assert runner.finalize_delivery("failed", False) is True
        assert pm.history() == []

    async def test_runner_never_writes_memory_for_deferred_delivery(self) -> None:
        class Memory:
            def __init__(self) -> None:
                self.entries = []

            async def write(self, entry) -> None:
                self.entries.append(entry)

        memory = Memory()
        runner, _pm, *_ = make_runner(FakeLLM(VALID))
        runner._memory = memory
        runner._memory_extractor = MemoryExtractor()
        await runner.run_turn("verified", "câu đủ dài để ghi nhớ", defer_delivery_commit=True)
        assert memory.entries == []
        assert runner.finalize_delivery("verified", True) is True
        await asyncio.sleep(0)
        assert memory.entries == []
        assert runner.memory_write_metrics()["memory_background_writes_pending"] == 0

    async def test_failed_delivery_never_schedules_success_memory(self) -> None:
        class Memory:
            async def write(self, entry) -> None:
                raise AssertionError("failed delivery must not write memory")

        runner, _pm, *_ = make_runner(FakeLLM(VALID))
        runner._memory = Memory()
        runner._memory_extractor = MemoryExtractor()
        await runner.run_turn("failed-memory", "câu đủ dài để ghi nhớ", defer_delivery_commit=True)
        assert runner.finalize_delivery("failed-memory", False) is True
        await asyncio.sleep(0)
        assert runner.memory_write_metrics()["memory_background_writes_scheduled"] == 0

    async def test_runner_has_no_deferred_memory_task_to_close(self) -> None:
        blocker = asyncio.Event()

        class SlowMemory:
            async def write(self, entry) -> None:
                await blocker.wait()

        runner, _pm, *_ = make_runner(FakeLLM(VALID))
        runner._memory = SlowMemory()
        runner._memory_extractor = MemoryExtractor()
        runner._pending_memory_writes_max = 1
        await runner.run_turn("slow", "câu đủ dài để ghi nhớ", defer_delivery_commit=True)
        assert runner.finalize_delivery("slow", True) is True
        await asyncio.sleep(0)
        assert runner.memory_write_metrics()["memory_background_writes_pending"] == 0
        await runner.close_memory_writes()
        assert runner.memory_write_metrics()["memory_background_writes_pending"] == 0


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
        assert fb._chains["llm"].timeouts == [20.0, 0.1]
        assert pm._history_max_chars == 1200

    async def test_generation_caps_are_applied_per_turn_kind(self) -> None:
        fake = FakeLLM(VALID)
        pm = PromptManager(PromptCache("persona test"), max_history_turns=2)
        runner = LLMTurnRunner(
            fake,
            pm,
            FallbackManager(),
            CannedResponder({"default": ["fallback"]}),
            chat_max_tokens=160,
            ambient_max_tokens=96,
            directed_max_tokens=128,
        )

        await runner.run_turn("chat", "xin chào")
        await runner.run_ambient_turn("ambient", "tự mở lời")
        await runner.run_directed_turn("directed", "tiếp tục chủ đề")

        assert [request.max_tokens for request in fake.requests] == [160, 96, 128]
