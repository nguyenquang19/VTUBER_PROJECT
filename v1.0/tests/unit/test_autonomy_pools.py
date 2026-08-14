"""Test RoundRobinPool + OpenerTracker + Dedup — Aut.B."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from services.autonomy.dedup import DedupBuffer, is_too_similar
from services.autonomy.opener_tracker import OpenerTracker
from services.autonomy.pools import RoundRobinPool

REPO_ROOT = Path(__file__).resolve().parents[2]


# ═══════════════════════ RoundRobinPool ═══════════════════════


class TestPool:
    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            RoundRobinPool([])

    def test_next_returns_item(self) -> None:
        p = RoundRobinPool(["a", "b", "c"], no_repeat_last_n=1, rng=random.Random(0))
        pick = p.next()
        assert pick in {"a", "b", "c"}

    def test_no_repeat_last_n(self) -> None:
        p = RoundRobinPool(["a", "b", "c", "d"], no_repeat_last_n=2, rng=random.Random(0))
        picks = [p.next() for _ in range(4)]
        # 2 pick gần nhất không được lặp
        for i in range(2, 4):
            assert picks[i] not in picks[i - 2 : i], (
                f"pick[{i}]={picks[i]} lặp trong window {picks[i-2:i]}"
            )

    def test_reshuffle_when_exhausted(self) -> None:
        """Pool 3 item + no_repeat=3 → sau 3 pick recent full → reshuffle clear."""
        p = RoundRobinPool(["a", "b", "c"], no_repeat_last_n=3,
                           reshuffle_when_exhausted=True, rng=random.Random(0))
        picks = [p.next() for _ in range(6)]
        # 6 pick, không None (reshuffle hoạt động)
        assert None not in picks
        # Phân bổ tương đối đều — mỗi item xuất hiện ~2 lần
        counts = {x: picks.count(x) for x in ("a", "b", "c")}
        assert all(v >= 1 for v in counts.values())

    def test_no_reshuffle_returns_none(self) -> None:
        p = RoundRobinPool(["a"], no_repeat_last_n=1, reshuffle_when_exhausted=False)
        assert p.next() == "a"
        assert p.next() is None   # a trong recent, không còn candidate

    def test_snapshot(self) -> None:
        p = RoundRobinPool(["a", "b"], no_repeat_last_n=2)
        s = p.snapshot()
        assert s["size"] == 2
        assert s["recent"] == []
        assert s["available"] == 2


# ═══════════════════════ OpenerTracker ═══════════════════════


class TestOpenerTracker:
    def test_empty_forbidden_list(self) -> None:
        t = OpenerTracker()
        assert t.forbidden_list() == "(không có)"

    def test_records_3_words(self) -> None:
        t = OpenerTracker(window=5, words_per_opener=3)
        t.record("Ơ chào cậu ơi cậu đang làm gì đấy")
        assert t.recent() == ["ơ chào cậu"]

    def test_forbidden_list_formats(self) -> None:
        t = OpenerTracker(window=3, words_per_opener=2)
        t.record("Hôm nay trời đẹp")
        t.record("Cậu ơi tớ đói")
        s = t.forbidden_list()
        assert '"hôm nay..."' in s
        assert '"cậu ơi..."' in s

    def test_window_limits(self) -> None:
        t = OpenerTracker(window=2, words_per_opener=2)
        t.record("a b x")
        t.record("c d y")
        t.record("e f z")
        # deque maxlen=2 → chỉ giữ 2 gần nhất
        assert len(t.recent()) == 2
        assert "a b" not in t.recent()

    def test_ignores_empty(self) -> None:
        t = OpenerTracker()
        t.record("")
        t.record("   ")
        assert t.recent() == []

    def test_strips_mood_block(self) -> None:
        """Mood block cuối câu không tính là opener text."""
        t = OpenerTracker(words_per_opener=3)
        t.record("Chào cậu ơi [vui:5 buc:2]")
        assert t.recent() == ["chào cậu ơi"]

    def test_reset(self) -> None:
        t = OpenerTracker()
        t.record("hello world foo")
        t.reset()
        assert t.recent() == []


# ═══════════════════════ Dedup ═══════════════════════


class TestDedupFunction:
    def test_identical_flagged(self) -> None:
        assert is_too_similar("chào cậu ơi", ["chào cậu ơi"])

    def test_unrelated_not_flagged(self) -> None:
        assert not is_too_similar(
            "hôm nay trời đẹp",
            ["cà phê đắng quá"],
        )

    def test_partial_overlap_at_threshold(self) -> None:
        # Overlap ~50% < 0.6 threshold
        assert not is_too_similar(
            "một hai ba bốn",
            ["một hai năm sáu"],
            threshold=0.6,
        )

    def test_partial_overlap_above_threshold(self) -> None:
        # 4/5 = 0.8 > 0.6
        assert is_too_similar(
            "một hai ba bốn năm",
            ["một hai ba bốn bảy"],
            threshold=0.6,
        )

    def test_empty_new_text_not_similar(self) -> None:
        assert not is_too_similar("", ["hi"])

    def test_empty_recent_list(self) -> None:
        assert not is_too_similar("hi", [])

    def test_mood_block_stripped_before_compare(self) -> None:
        """Mood block khác nhau nhưng câu giống → vẫn dedup."""
        assert is_too_similar(
            "chào cậu ơi [vui:5]",
            ["chào cậu ơi [vui:8 buc:0]"],
        )

    def test_leading_bracket_tag_does_not_bypass_dedup(self) -> None:
        assert is_too_similar(
            "[MÔ PHỎNG] Chat chạy nhanh ghê.",
            ["[MÔ PHỎNG] Chat chạy nhanh ghê."],
        )

    def test_reversing_sentence_order_is_still_duplicate(self) -> None:
        assert is_too_similar(
            "Lôi tớ ra trêu cũng vui thật đấy. Béo ở đâu chứ?",
            ["Béo ở đâu chứ? Lôi tớ ra trêu cũng vui thật đấy."],
            threshold=0.72,
        )


class TestDedupBuffer:
    def test_check_uses_recent(self) -> None:
        d = DedupBuffer(window=3, threshold=0.6)
        d.record("chào cậu ơi cậu ơi cậu ơi")
        assert d.check("chào cậu ơi cậu ơi cậu ơi") is True
        assert d.check("hôm nay trời đẹp và mát") is False

    def test_record_ignores_empty(self) -> None:
        d = DedupBuffer()
        d.record("")
        d.record("   ")
        assert d.recent() == []

    def test_window_evicts_old(self) -> None:
        d = DedupBuffer(window=2, threshold=0.5)
        d.record("một hai ba")
        d.record("bốn năm sáu")
        d.record("bảy tám chín")
        # "một hai ba" đã bị evict → check "một hai ba" không match
        assert d.check("một hai ba") is False
