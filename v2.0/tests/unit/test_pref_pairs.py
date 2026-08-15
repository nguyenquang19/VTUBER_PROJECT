"""Test T2 — pref pairs (DPO) khi regen (Phase 8 data pipeline)."""
from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import JsonlWriter
from services.llm.canned_response import CannedResponder
from services.llm.llm_turn import LLMTurnRunner
from services.llm.parser import parse_response
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager
from tests.unit.test_llm_turn import VALID, FakeLLM


def _runner(tmp_path: Path, fake, regen=None):
    pref = JsonlWriter(tmp_path / "pref.jsonl")
    pm = PromptManager(PromptCache("persona"), max_history_turns=4)
    r = LLMTurnRunner(fake, pm, FallbackManager(),
                      CannedResponder({"default": ["C"]}, rng=random.Random(0)),
                      pref_logger=pref, regenerator=regen)
    return r, tmp_path / "pref.jsonl"


def _read(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestLogPrefPair:
    def test_writes_pair(self, tmp_path: Path) -> None:
        r, path = _runner(tmp_path, FakeLLM(VALID))
        r.log_pref_pair("câu dở", "câu hay", "filter:persona_break", user_text="hi")
        recs = _read(path)
        assert len(recs) == 1
        assert recs[0]["rejected"] == "câu dở"
        assert recs[0]["chosen"] == "câu hay"
        assert recs[0]["reason"] == "filter:persona_break"
        assert recs[0]["schema_version"] == 1
        assert recs[0]["session_id"] == r.session_id
        assert recs[0]["source"] == "filter"
        assert recs[0]["timestamp"].endswith("+00:00")

    def test_identical_not_written(self, tmp_path: Path) -> None:
        r, path = _runner(tmp_path, FakeLLM(VALID))
        r.log_pref_pair("x", "x", "dedup")
        assert not path.exists() or _read(path) == []

    def test_no_logger_no_crash(self, tmp_path: Path) -> None:
        pm = PromptManager(PromptCache("p"), max_history_turns=2)
        r = LLMTurnRunner(FakeLLM(VALID), pm, FallbackManager(),
                          CannedResponder({"default": ["C"]}, rng=random.Random(0)))
        r.log_pref_pair("a", "b", "x")   # pref_logger None → no-op, không raise

    def test_known_display_name_is_removed_from_pair(self, tmp_path: Path) -> None:
        r, path = _runner(tmp_path, FakeLLM(VALID))
        r._log_cause = SimpleNamespace(viewer_alias="RealViewer")  # noqa: SLF001
        r.log_pref_pair(
            "RealViewer nói câu dở", "trả lời RealViewer", "filter:privacy",
            user_text="chào RealViewer",
        )
        encoded = json.dumps(_read(path), ensure_ascii=False)
        assert "RealViewer" not in encoded
        assert "[PII]" in encoded


class FakeRegen:
    """Regen giả: luôn đổi text (mô phỏng filter chặn → regen)."""
    async def check_and_maybe_regen(self, request, parsed, on_token=None):
        from types import SimpleNamespace
        new = parse_response("Câu đã sửa cho lịch sự.")
        verdict = SimpleNamespace(passed=True,
                                  categories_hit=[SimpleNamespace(value="persona_break")])
        return new, verdict


class TestFilterRegenAutoPair:
    async def test_filter_regen_creates_pair(self, tmp_path: Path) -> None:
        r, path = _runner(tmp_path, FakeLLM(VALID), regen=FakeRegen())
        await r.run_turn("r1", "chọc phá", session_id="s1")
        recs = _read(path)
        assert len(recs) == 1
        assert recs[0]["rejected"] == "Chào cậu."           # bản đầu (VALID)
        assert recs[0]["chosen"] == "Câu đã sửa cho lịch sự."
        assert recs[0]["reason"] == "filter:persona_break"
        assert recs[0]["session_id"] == "s1"

    async def test_no_regen_no_pair(self, tmp_path: Path) -> None:
        # không regenerator → không cặp
        r, path = _runner(tmp_path, FakeLLM(VALID))
        await r.run_turn("r1", "bình thường")
        assert not path.exists() or _read(path) == []
