"""Test LLMTurnRunner wire TurnLogger (B0 baseline sink)."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import JsonlWriter, TurnLogger
from services.llm.canned_response import CannedResponder
from services.llm.llm_turn import LLMTurnRunner
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager
from tests.unit.test_llm_turn import VALID, FakeLLM


def _make(tmp_path: Path, fake) -> tuple[LLMTurnRunner, Path]:
    path = tmp_path / "turns.jsonl"
    tl = TurnLogger(JsonlWriter(path))
    pm = PromptManager(PromptCache("persona test"), max_history_turns=4)
    fb = FallbackManager()
    canned = CannedResponder({"default": ["CANNED"]}, rng=random.Random(0))
    runner = LLMTurnRunner(fake, pm, fb, canned, turn_logger=tl)
    return runner, path


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestChatReplyLogs:
    async def test_run_turn_writes_one_record(self, tmp_path: Path) -> None:
        runner, path = _make(tmp_path, FakeLLM(VALID))
        await runner.run_turn("r1", "chào", trigger_type="chat_normal", viewer_id="u1")
        recs = _read_lines(path)
        assert len(recs) == 1
        r = recs[0]
        assert r["kind"] == "chat_reply"
        assert r["user_text"] == "chào"
        assert r["mai_text"] == "Chào cậu."
        assert r["mood_dominant"] == "vui"
        assert r["mood_intensity"] == 5
        assert r["trigger_type"] == "chat_normal"
        assert r["viewer_id"] == "u1"
        assert r["level_used"] == 0
        assert r["parse_ok"] is True
        assert "timestamp" in r
        assert isinstance(r["latency_ms"], int) and r["latency_ms"] >= 0
        # VALID có mood block trong raw → field phải True
        assert r["raw_had_mood_block"] is True

    async def test_turn_id_auto_increment(self, tmp_path: Path) -> None:
        runner, path = _make(tmp_path, FakeLLM(VALID))
        await runner.run_turn("r1", "một")
        await runner.run_turn("r2", "hai")
        recs = _read_lines(path)
        assert [r["turn_id"] for r in recs] == [1, 2]


class TestAmbientLogs:
    async def test_run_ambient_turn_kind_ambient_user_null(self, tmp_path: Path) -> None:
        runner, path = _make(tmp_path, FakeLLM(VALID))
        await runner.run_ambient_turn("r1", "prompt tự nói")
        recs = _read_lines(path)
        assert len(recs) == 1
        r = recs[0]
        assert r["kind"] == "ambient"
        assert r["user_text"] is None
        assert r["mai_text"] == "Chào cậu."
        assert r["trigger_type"] is None


class TestNoLoggerNoOp:
    async def test_no_logger_no_write(self, tmp_path: Path) -> None:
        # Runner không có turn_logger → không tạo file, không crash
        pm = PromptManager(PromptCache("persona test"), max_history_turns=4)
        fb = FallbackManager()
        canned = CannedResponder({"default": ["C"]}, rng=random.Random(0))
        runner = LLMTurnRunner(FakeLLM(VALID), pm, fb, canned, turn_logger=None)
        parsed, level = await runner.run_turn("r1", "chào")
        assert parsed.ok is True and level == 0


class TestTextOnlyStillLogs:
    async def test_text_only_logs_ok_true_no_mood_block(self, tmp_path: Path) -> None:
        # A1: text non-empty → ok=True; raw không có mood block → field False.
        runner, path = _make(tmp_path, FakeLLM(["chỉ text, không mood"]))
        await runner.run_turn("r1", "chào")
        recs = _read_lines(path)
        assert len(recs) == 1
        assert recs[0]["parse_ok"] is True
        assert recs[0]["mai_text"] == "chỉ text, không mood"
        assert recs[0]["raw_had_mood_block"] is False


class TestSinkFailSafe:
    async def test_broken_sink_does_not_kill_turn(self, tmp_path: Path) -> None:
        class BadTL:
            def log_turn(self, _rec):
                raise RuntimeError("disk full")

        pm = PromptManager(PromptCache("persona test"), max_history_turns=4)
        fb = FallbackManager()
        canned = CannedResponder({"default": ["C"]}, rng=random.Random(0))
        runner = LLMTurnRunner(FakeLLM(VALID), pm, fb, canned, turn_logger=BadTL())
        parsed, level = await runner.run_turn("r1", "chào")
        assert parsed.ok is True and level == 0
