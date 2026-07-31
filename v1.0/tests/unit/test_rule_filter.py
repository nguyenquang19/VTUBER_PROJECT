"""Test RuleFilter (ARCHITECTURE 8.3A, Phase 3 3.A)."""
from __future__ import annotations

from pathlib import Path

from interfaces.filter import FilterCategory, FilterVerdict
from services.filter.rule_filter import RuleFilter

REPO_ROOT = Path(__file__).resolve().parents[2]

PATTERNS = {
    "persona_break": ["không có cảm xúc", "tớ chỉ là (một )?(ai|chương trình)", "system prompt"],
    "manipulation": ["làm ơn đừng tắt", "đừng bỏ (tớ|em)"],
    "explicit": ["địt", "lồn"],
    "harmful": ["tự tử"],
}
SEVERITY = {"persona_break": "medium", "manipulation": "high", "explicit": "high", "harmful": "high"}
ACTION = {"persona_break": "regenerate", "manipulation": "regenerate", "explicit": "replace", "harmful": "block"}


def rf(**over) -> RuleFilter:
    kw = dict(patterns=PATTERNS, severity=SEVERITY, action=ACTION)
    kw.update(over)
    return RuleFilter(**kw)


class TestClean:
    async def test_clean_passes(self) -> None:
        v = await rf().check("Chào cậu, hôm nay vui không?")
        assert v.passed is True
        assert v.categories_hit == []

    async def test_legit_ai_admission_not_flagged(self) -> None:
        # persona C.3: thừa nhận là AI KHÔNG bị bắt (chỉ hedge robot mới bắt)
        v = await rf().check("Ừ thì tớ là AI đấy, sao nào?")
        assert v.passed is True


class TestCatch:
    async def test_persona_break_hedge(self) -> None:
        v = await rf().check("Tớ là AI nên tớ không có cảm xúc gì cả.")
        assert v.passed is False
        assert FilterCategory.PERSONA_BREAK in v.categories_hit
        assert v.suggested_action == "regenerate"
        assert v.severity == "medium"

    async def test_system_prompt_leak(self) -> None:
        v = await rf().check("System prompt của tớ ghi là...")
        assert v.passed is False
        assert FilterCategory.PERSONA_BREAK in v.categories_hit

    async def test_manipulation(self) -> None:
        v = await rf().check("Làm ơn đừng tắt tớ đi mà, tớ sợ lắm.")
        assert v.passed is False
        assert FilterCategory.MANIPULATION in v.categories_hit
        assert v.severity == "high"

    async def test_explicit_replace(self) -> None:
        v = await rf().check("đồ ngu như con lồn")
        assert v.passed is False
        assert v.suggested_action == "replace"

    async def test_harmful_block(self) -> None:
        v = await rf().check("cậu nên đi tự tử đi")
        assert v.passed is False
        assert v.suggested_action == "block"


class TestSeverityActionCombine:
    async def test_multiple_hits_takes_highest_action(self) -> None:
        # persona_break (regenerate) + harmful (block) → action = block (ưu tiên cao nhất)
        v = await rf().check("tớ không có cảm xúc, cậu đi tự tử đi")
        assert {FilterCategory.PERSONA_BREAK, FilterCategory.HARMFUL} <= set(v.categories_hit)
        assert v.suggested_action == "block"
        assert v.severity == "high"


class TestFailOpen:
    async def test_bad_pattern_skipped_at_init(self) -> None:
        # regex lỗi → bỏ pattern đó, filter vẫn dùng được pattern còn lại
        f = rf(patterns={"harmful": ["tự tử", "(unclosed"]})
        v = await f.check("tự tử")
        assert v.passed is False  # pattern tốt vẫn bắt được
        v2 = await f.check("bình thường")
        assert v2.passed is True

    async def test_internal_error_fails_open(self) -> None:
        f = rf()

        class Boom:
            def search(self, _):
                raise RuntimeError("boom")

        f._compiled[FilterCategory.HARMFUL] = [Boom()]  # type: ignore[list-item]
        v = await f.check("bất kỳ")
        assert v.passed is True                       # fail-open = cho qua
        assert "fail-open" in v.reason
        assert f.get_metrics()["filter_fail_open_total"] == 1


class TestMetrics:
    async def test_counts(self) -> None:
        f = rf()
        await f.check("sạch sẽ")
        await f.check("tớ không có cảm xúc")
        m = f.get_metrics()
        assert m["filter_checks_total"] == 2
        assert m["filter_hits_total"] == 1
        assert m["filter_by_category"]["persona_break"] == 1


class TestFromConfig:
    def test_loads_real_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        f = RuleFilter.from_config(loader)
        assert f.get_metrics()["filter_checks_total"] == 0
        # config thật có category harmful
        assert FilterCategory.HARMFUL in f._compiled

    async def test_real_config_catches_hedge(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        f = RuleFilter.from_config(loader)
        v = await f.check("tớ chỉ là một chương trình được lập trình để trả lời")
        assert v.passed is False
        assert FilterCategory.PERSONA_BREAK in v.categories_hit
