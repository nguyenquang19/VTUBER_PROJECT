"""Test sentence splitter VN (Phase 4, 4.C)."""
from __future__ import annotations

from services.tts.sentence_splitter import LiveSentenceStreamer, split_vn


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


# ---------- LiveSentenceStreamer ----------

def stream_tokens(streamer: LiveSentenceStreamer, text: str, chunk_size: int = 3) -> list[str]:
    """Push text theo chunk nhỏ như LLM streaming; trả list câu đã emit."""
    seen: list[str] = []
    streamer._on_sentence = seen.append  # type: ignore[assignment]
    for i in range(0, len(text), chunk_size):
        streamer.push(text[i:i + chunk_size])
    streamer.close()
    return seen


class TestLiveStreamerBasics:
    def test_single_sentence(self) -> None:
        s = LiveSentenceStreamer(lambda _s: None)
        seen = stream_tokens(s, "Chào cậu.")
        assert seen == ["Chào cậu."]

    def test_multi_sentence_emitted_incrementally(self) -> None:
        emitted_order: list[str] = []
        s = LiveSentenceStreamer(emitted_order.append)
        # Push từng ký tự để verify emit ngay khi thấy dấu kết
        for ch in "Câu 1. Câu 2! Câu 3?":
            s.push(ch)
        s.close()
        assert emitted_order == ["Câu 1.", "Câu 2!", "Câu 3?"]

    def test_incomplete_tail_flushed_on_close(self) -> None:
        s = LiveSentenceStreamer(lambda _s: None)
        seen = stream_tokens(s, "Câu đủ. Câu chưa hoàn")
        assert seen == ["Câu đủ.", "Câu chưa hoàn"]


class TestLiveStreamerMoodBlock:
    def test_stops_at_mood_block(self) -> None:
        s = LiveSentenceStreamer(lambda _s: None)
        # Persona format: text \n\n [vui:...] \n lý do: ...
        text = "Cái này tự tra đi.\n\n[vui:3 buon:0 buc:5 bon_chon:0 nguong:0]\nlý do: x"
        seen = stream_tokens(s, text, chunk_size=4)
        assert seen == ["Cái này tự tra đi."]
        assert s.blocked is True

    def test_mood_block_arrives_after_incomplete_sentence(self) -> None:
        s = LiveSentenceStreamer(lambda _s: None)
        text = "Ngắn quá\n\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]"
        seen = stream_tokens(s, text)
        # câu không có dấu kết vẫn được flush khi cắt tại mood block
        assert seen == ["Ngắn quá"]
        assert s.blocked is True

    def test_no_emit_after_blocked(self) -> None:
        s = LiveSentenceStreamer(lambda _s: None)
        text = "Câu.\n\n[vui:1] extra stuff. Ignored."
        seen = stream_tokens(s, text)
        assert seen == ["Câu."]


class TestLiveStreamerEdges:
    def test_empty_input(self) -> None:
        s = LiveSentenceStreamer(lambda _s: None)
        seen = stream_tokens(s, "")
        assert seen == []

    def test_only_whitespace_after_close_no_emit(self) -> None:
        s = LiveSentenceStreamer(lambda _s: None)
        seen = stream_tokens(s, "   \n\n   ")
        assert seen == []

    def test_min_len_filter(self) -> None:
        # 2 dấu chấm không có chữ → không emit
        seen: list[str] = []
        s = LiveSentenceStreamer(seen.append, min_len=1)
        s.push("..")
        s.push(" .")
        s.close()
        assert seen == []
