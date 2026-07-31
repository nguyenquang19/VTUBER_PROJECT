"""Test sentence splitter VN (Phase 4, 4.C)."""
from __future__ import annotations

from services.tts.sentence_splitter import split_vn


class TestBasics:
    def test_empty(self) -> None:
        assert split_vn("") == []
        assert split_vn("   \n\t  ") == []

    def test_single_sentence_no_punct(self) -> None:
        # câu không dấu kết vẫn giữ nguyên (đuôi chưa hoàn chỉnh)
        assert split_vn("chào cậu") == ["chào cậu"]

    def test_single_with_period(self) -> None:
        assert split_vn("chào cậu.") == ["chào cậu."]

    def test_two_sentences(self) -> None:
        out = split_vn("Chào cậu. Khoẻ không?")
        assert out == ["Chào cậu.", "Khoẻ không?"]

    def test_ellipsis_and_bang(self) -> None:
        out = split_vn("Ơ... thế à! Sao mày biết?")
        assert out == ["Ơ...", "thế à!", "Sao mày biết?"]


class TestNumbers:
    def test_decimal_not_split(self) -> None:
        out = split_vn("Pi khoảng 3.14. Chấp nhận được.")
        assert out == ["Pi khoảng 3.14.", "Chấp nhận được."]

    def test_dotted_thousand_not_split(self) -> None:
        out = split_vn("Giá là 1.250.000. Đắt phết.")
        assert out == ["Giá là 1.250.000.", "Đắt phết."]


class TestEdges:
    def test_trailing_whitespace(self) -> None:
        assert split_vn("Hi.   \n  ") == ["Hi."]

    def test_multiple_end_punct(self) -> None:
        # "!!?" gộp vào 1 câu
        assert split_vn("Cái gì!!? Nói lại đi.") == ["Cái gì!!?", "Nói lại đi."]

    def test_min_len_filters_tiny(self) -> None:
        # ".." không có chữ → bị loại (min_len=1 mặc định = 1 ký tự non-space)
        assert split_vn(". . .") == []

    def test_incomplete_tail_kept(self) -> None:
        out = split_vn("Câu đầu. Câu chưa xong")
        assert out == ["Câu đầu.", "Câu chưa xong"]
