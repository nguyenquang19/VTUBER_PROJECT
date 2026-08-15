"""Test AppraisalTable — Phase 7.5.B (24 category lookup)."""
from __future__ import annotations

from pathlib import Path

import pytest

from services.emotion.appraisal import AppraisalTable

REPO_ROOT = Path(__file__).resolve().parents[2]


# 20 category chính (10 system + 10 chat) + 4 timer = 24 tổng.
# 2 category có target rỗng ({}): chat_question_normal, chat_neutral.
_ALL_CATEGORIES = [
    # System (10)
    "operator_sudden_shutdown", "operator_join", "operator_leave",
    "donation_small", "donation_large", "subscribe_new",
    "viewer_count_spike", "viewer_count_drop",
    "stream_start", "stream_end",
    # Chat (10)
    "chat_compliment", "chat_insult_troll", "chat_question_normal",
    "chat_genuine_sad_share", "chat_spam_flood", "chat_mention_direct",
    "chat_jailbreak_attempt", "chat_sexual_advance", "mai_self_error",
    "chat_neutral",
    # Timer (4)
    "silence_1min", "silence_5min", "silence_10min_plus", "long_session_active",
]


@pytest.fixture
def real_table() -> AppraisalTable:
    from orchestrator.config_loader import ConfigLoader
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    return AppraisalTable.from_loader(loader)


class TestLookup:
    def test_all_24_categories_present(self, real_table: AppraisalTable) -> None:
        known = set(real_table.known_categories())
        missing = [c for c in _ALL_CATEGORIES if c not in known]
        assert not missing, f"thiếu category: {missing}"

    def test_shutdown_target(self, real_table: AppraisalTable) -> None:
        tgt = real_table.target_for("operator_sudden_shutdown")
        assert tgt["buon"] == 8
        assert tgt["buc"] == 6
        assert tgt["bon_chon"] == 7

    def test_donation_large(self, real_table: AppraisalTable) -> None:
        tgt = real_table.target_for("donation_large")
        assert tgt["vui"] == 9
        assert tgt["nguong"] == 6

    def test_sexual_advance_never_vui(self, real_table: AppraisalTable) -> None:
        """Spec Mục 4: chat_sexual_advance KHÔNG bao giờ vui."""
        tgt = real_table.target_for("chat_sexual_advance")
        assert "vui" not in tgt or tgt["vui"] == 0
        assert tgt["buc"] == 6
        assert tgt["nguong"] == 7

    def test_empty_target_for_neutral(self, real_table: AppraisalTable) -> None:
        assert real_table.target_for("chat_question_normal") == {}
        assert real_table.target_for("chat_neutral") == {}

    def test_unknown_category_empty(self, real_table: AppraisalTable) -> None:
        assert real_table.target_for("does_not_exist") == {}

    def test_returned_dict_is_copy(self, real_table: AppraisalTable) -> None:
        """Mutation không ảnh hưởng bảng gốc."""
        tgt = real_table.target_for("chat_compliment")
        tgt["vui"] = 999
        again = real_table.target_for("chat_compliment")
        assert again["vui"] == 7


class TestToneFlags:
    def test_gentle_tone_flag(self, real_table: AppraisalTable) -> None:
        assert real_table.tone_flag("chat_genuine_sad_share") == "force_gentle_tone"

    def test_deflect_flag(self, real_table: AppraisalTable) -> None:
        assert real_table.tone_flag("chat_sexual_advance") == "force_deflect"

    def test_no_flag(self, real_table: AppraisalTable) -> None:
        assert real_table.tone_flag("chat_compliment") is None
        assert real_table.tone_flag("operator_join") is None


class TestExplicitMapping:
    def test_construct_from_dict(self) -> None:
        t = AppraisalTable(
            mapping={"cat_a": {"vui": 5}, "cat_b": {}},
            tone_flags={"cat_a": "force_x"},
        )
        assert t.target_for("cat_a") == {"vui": 5}
        assert t.target_for("cat_b") == {}
        assert t.tone_flag("cat_a") == "force_x"
