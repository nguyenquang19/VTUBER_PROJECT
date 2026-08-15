"""Test T3 — MoodStyleTable (PLAN Mood→Style)."""
from __future__ import annotations

from pathlib import Path

from interfaces.animation import MoodState
from services.emotion.mood_style import MoodStyleTable

REPO_ROOT = Path(__file__).resolve().parents[2]


def _table() -> MoodStyleTable:
    from orchestrator.config_loader import ConfigLoader
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    t = MoodStyleTable.from_loader(loader)
    assert t is not None
    return t


class TestDirective:
    def test_buc_high_gives_coc_gat_no_raw_mood(self) -> None:
        t = _table()
        d = t.directive_for(MoodState(buc=9))
        assert d is not None
        assert "cộc" in d or "gắt" in d
        # KHÔNG chứa số THÔ mood / nhãn snake_case (mô tả "1-2 câu" thì OK)
        for tok in ("buc=", "vui=", "buon=", "bon_chon=", "nguong=", "=9", "=8"):
            assert tok not in d

    def test_baseline_returns_none(self) -> None:
        # mood toàn ≤5 → vùng chết → None (persona default nói)
        t = _table()
        assert t.directive_for(MoodState(vui=5, buon=4, buc=3)) is None

    def test_tie_prefers_declaration_order(self) -> None:
        # vui=8 buc=8 → chọn vui (thứ tự _DIMS: vui trước buc)
        t = _table()
        d = t.directive_for(MoodState(vui=8, buc=8))
        assert d is not None
        assert "vui" in d and "bực" not in d

    def test_gentle_flag_overrides(self) -> None:
        t = _table()
        assert t.directive_for(MoodState(buc=9), tone_flags={"force_gentle_tone"}) is None

    def test_deflect_flag_overrides(self) -> None:
        t = _table()
        assert t.directive_for(MoodState(nguong=9), tone_flags={"force_deflect"}) is None

    def test_band_boundaries(self) -> None:
        t = _table()
        # 7 → mid (không prefix), 8 → high ("khá "), 10 → peak ("cực kỳ ")
        assert "khá" not in t.directive_for(MoodState(vui=7))
        assert "khá" in t.directive_for(MoodState(vui=8))
        assert "cực kỳ" in t.directive_for(MoodState(vui=10))

    def test_below_floor_none(self) -> None:
        t = _table()
        assert t.directive_for(MoodState(buc=5)) is None   # 5 < floor 6
        assert t.directive_for(MoodState(buc=6)) is not None


class TestFailSafe:
    def test_from_loader_none_when_styles_missing(self) -> None:
        # Fake loader: mọi section rỗng → styles rỗng → None (fail-safe không bơm)
        class FakeLoader:
            def get(self, section, key, default=None):
                return default
        assert MoodStyleTable.from_loader(FakeLoader()) is None

    def test_directive_never_raises_on_bad_mood(self) -> None:
        t = _table()

        class Bad:
            pass
        # mood object thiếu attr → không raise, trả None
        assert t.directive_for(Bad()) is None
