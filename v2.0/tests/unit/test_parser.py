"""Test parser output LLM (ARCHITECTURE 7.4/8.2, config/prompts/persona_system.txt B, milestone 1.D)."""
from __future__ import annotations

from services.llm.parser import ParsedResponse, parse_response

FULL = """Ừ thì chào cậu.

[vui:4 buồn:1 bực:3 bồn_chồn:2 ngượng:0]
lý do: thấy câu hỏi hơi nhạt
còn nữa: không"""


class TestHappyPath:
    def test_extracts_text_without_mood_block(self) -> None:
        r = parse_response(FULL)
        assert r.text == "Ừ thì chào cậu."
        assert "vui:" not in r.text
        assert "lý do" not in r.text

    def test_mood_values(self) -> None:
        r = parse_response(FULL)
        assert (r.mood.vui, r.mood.buon, r.mood.buc, r.mood.bon_chon, r.mood.nguong) == (4, 1, 3, 2, 0)

    def test_reason_and_continuation(self) -> None:
        r = parse_response(FULL)
        assert r.reason == "thấy câu hỏi hơi nhạt"
        assert r.continuation is False

    def test_ok_true_when_all_five(self) -> None:
        assert parse_response(FULL).ok is True

    def test_returns_parsedresponse(self) -> None:
        assert isinstance(parse_response(FULL), ParsedResponse)
        assert parse_response(FULL).raw == FULL


class TestAccentTolerance:
    def test_non_accented_keys(self) -> None:
        raw = "hi\n\n[vui:5 buon:2 buc:1 bon_chon:0 nguong:3]\nlý do: x\ncòn nữa: có"
        r = parse_response(raw)
        assert r.ok is True
        assert (r.mood.vui, r.mood.buon, r.mood.buc, r.mood.bon_chon, r.mood.nguong) == (5, 2, 1, 0, 3)

    def test_space_instead_of_underscore(self) -> None:
        raw = "hi\n\n[vui:1 buồn:1 bực:1 bồn chồn:7 ngượng:1]\nlý do: y\ncòn nữa: không"
        r = parse_response(raw)
        assert r.mood.bon_chon == 7
        assert r.ok is True

    def test_mixed_accents(self) -> None:
        raw = "hi\n\n[vui:2 buon:3 bực:4 bon_chon:5 ngượng:6]"
        r = parse_response(raw)
        assert (r.mood.buon, r.mood.buc, r.mood.bon_chon, r.mood.nguong) == (3, 4, 5, 6)


class TestContinuation:
    def test_co_true(self) -> None:
        assert parse_response("a\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]\ncòn nữa: có").continuation is True

    def test_khong_false(self) -> None:
        assert parse_response("a\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]\ncòn nữa: không").continuation is False

    def test_no_accent_co(self) -> None:
        assert parse_response("a\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]\ncon nua: co").continuation is True

    def test_no_accent_khong(self) -> None:
        assert parse_response("a\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]\ncon nua: khong").continuation is False

    def test_missing_defaults_false(self) -> None:
        assert parse_response("a\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]").continuation is False


class TestReason:
    def test_ly_do_no_accent(self) -> None:
        r = parse_response("a\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]\nly do: vì thế")
        assert r.reason == "vì thế"

    def test_reason_missing(self) -> None:
        r = parse_response("a\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]")
        assert r.reason == ""


class TestFailSafe:
    def test_no_mood_block_returns_text(self) -> None:
        # A1: ok = True miễn text non-empty. Mood block không còn bắt buộc.
        r = parse_response("Chào cậu, tớ đây.")
        assert r.text == "Chào cậu, tớ đây."
        assert r.ok is True
        assert r.mood.dominant() == "neutral"

    def test_empty_input(self) -> None:
        r = parse_response("")
        assert r.text == ""
        assert r.ok is False

    def test_partial_mood_kept_ok_true_when_text(self) -> None:
        # A1: dù partial mood, có text → ok True (mood vẫn parse defensive).
        r = parse_response("hi\n[vui:5 buon:2]")
        assert r.ok is True
        assert r.mood.vui == 5
        assert r.mood.buon == 2

    def test_malformed_value_ignored(self) -> None:
        r = parse_response("hi\n[vui:1 buồn:2 bực:abc bồn_chồn:3 ngượng:4]")
        assert r.ok is True  # A1
        assert r.mood.buc == 0  # không parse được → default
        assert r.mood.bon_chon == 3

    def test_meta_lines_stripped_when_no_block(self) -> None:
        r = parse_response("Câu nói.\nlý do: abc\ncòn nữa: có")
        assert r.text == "Câu nói."
        assert r.ok is True  # A1: có text


class TestClampAndEdges:
    def test_value_over_ten_clamped(self) -> None:
        r = parse_response("hi\n[vui:99 buon:0 buc:0 bon_chon:0 nguong:0]")
        assert r.mood.vui == 10

    def test_random_brackets_in_text_ignored(self) -> None:
        raw = "Đây là [một cái ngoặc] trong câu.\n\n[vui:3 buon:0 buc:0 bon_chon:0 nguong:0]"
        r = parse_response(raw)
        assert r.text == "Đây là [một cái ngoặc] trong câu."
        assert r.mood.vui == 3
        assert r.ok is True

    def test_reasoning_tag_stripped(self) -> None:
        raw = "<think>tớ nên trả lời gì nhỉ</think>Chào cậu.\n[vui:2 buon:0 buc:0 bon_chon:0 nguong:0]"
        r = parse_response(raw)
        assert "think" not in r.text
        assert r.text == "Chào cậu."

    def test_special_token_stripped(self) -> None:
        raw = "<|channel|>Chào.\n[vui:1 buon:0 buc:0 bon_chon:0 nguong:0]"
        r = parse_response(raw)
        assert "channel" not in r.text
        assert r.text == "Chào."
