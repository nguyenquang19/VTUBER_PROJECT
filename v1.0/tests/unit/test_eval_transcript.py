"""Test scripts/eval_transcript.py — B0 baseline eval."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.eval_transcript import (
    Report,
    _has_mood_block,
    _opener3,
    _parse_iso,
    evaluate,
    iter_records,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


class TestOpener3:
    def test_three_words_lower(self) -> None:
        assert _opener3("Chào cậu hôm nay thế nào?") == "chào cậu hôm"

    def test_strips_punctuation_tail(self) -> None:
        assert _opener3("Ừ, cũng được đấy!") == "ừ, cũng được"

    def test_none_if_short(self) -> None:
        assert _opener3("Ừ ha") is None
        assert _opener3("") is None


class TestMoodBlock:
    def test_detects_full_block(self) -> None:
        assert _has_mood_block("hi [vui:5 buon:0 buc:0 bon_chon:0 nguong:0]")

    def test_detects_partial(self) -> None:
        assert _has_mood_block("[bực:8 vui:0]")
        assert _has_mood_block("nói xong [ngượng:3 vui:1]")

    def test_no_block(self) -> None:
        assert not _has_mood_block("Chào cậu.")
        assert not _has_mood_block("[chú thích 42]")  # không phải mood key


class TestParseIso:
    def test_z_suffix(self) -> None:
        dt = _parse_iso("2026-08-06T18:00:00Z")
        assert dt is not None and dt.tzinfo is not None

    def test_offset(self) -> None:
        dt = _parse_iso("2026-08-06T18:00:00+00:00")
        assert dt is not None

    def test_bad(self) -> None:
        assert _parse_iso("not a date") is None
        assert _parse_iso(None) is None


class TestEvaluate:
    def test_counts_and_kinds(self, tmp_path: Path) -> None:
        base = datetime(2026, 8, 6, 18, 0, 0, tzinfo=timezone.utc)
        recs = [
            {"turn_id": 1, "kind": "chat_reply", "mai_text": "Chào cậu nhé.",
             "timestamp": base.isoformat()},
            {"turn_id": 2, "kind": "ambient", "mai_text": "Ừ hôm nay chán quá.",
             "timestamp": (base + timedelta(seconds=5)).isoformat()},
        ]
        p = tmp_path / "turns.jsonl"
        _write_jsonl(p, recs)
        rep = evaluate(iter_records(p))
        assert rep.total == 2
        assert rep.by_kind["chat_reply"] == 1
        assert rep.by_kind["ambient"] == 1

    def test_opener_repeat_ratio(self, tmp_path: Path) -> None:
        recs = [
            {"kind": "chat_reply", "mai_text": "Ừ cũng được đấy."},
            {"kind": "chat_reply", "mai_text": "Ừ cũng được thôi mà."},
            {"kind": "chat_reply", "mai_text": "Ừ cũng được nhé."},
            {"kind": "ambient", "mai_text": "Khác hẳn hoàn toàn nhé."},
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(p, recs)
        rep = evaluate(iter_records(p))
        # 3 câu "ừ cũng được" trùng opener → repeat 3/4
        assert rep.opener_repeat_count == 3
        assert rep.opener_repeat_ratio == pytest.approx(0.75)

    def test_dead_air_gaps(self, tmp_path: Path) -> None:
        base = datetime(2026, 8, 6, 18, 0, 0, tzinfo=timezone.utc)
        recs = [
            {"kind": "chat_reply", "mai_text": "A a a.",
             "timestamp": base.isoformat()},
            {"kind": "chat_reply", "mai_text": "B b b.",
             "timestamp": (base + timedelta(seconds=3)).isoformat()},   # 3s: ok
            {"kind": "chat_reply", "mai_text": "C c c.",
             "timestamp": (base + timedelta(seconds=18)).isoformat()},  # 15s gap > 10
            {"kind": "ambient", "mai_text": "D d d.",
             "timestamp": (base + timedelta(seconds=48)).isoformat()},  # 30s gap > 10
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(p, recs)
        rep = evaluate(iter_records(p), dead_air_threshold_s=10.0)
        assert len(rep.dead_air_gaps_s) == 2
        assert rep.dead_air_gaps_s[0] == pytest.approx(15.0, abs=0.1)
        assert rep.dead_air_gaps_s[1] == pytest.approx(30.0, abs=0.1)

    def test_mood_exposition_legacy_mai_text(self, tmp_path: Path) -> None:
        # Log CŨ (trước A1.1) không có raw_had_mood_block → fallback về regex mai_text.
        recs = [
            {"kind": "chat_reply", "mai_text": "Chào cậu."},
            {"kind": "chat_reply", "mai_text": "Ừ. [vui:5 buon:0 buc:0 bon_chon:0 nguong:0]"},
            {"kind": "chat_reply", "mai_text": "Không có mood block ở đây."},
            {"kind": "ambient", "mai_text": "Nghĩ vu vơ [bực:7]"},
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(p, recs)
        rep = evaluate(iter_records(p))
        assert rep.mood_exposition_count == 2

    def test_mood_exposition_uses_raw_field_when_present(self, tmp_path: Path) -> None:
        # Log MỚI (A1.1) — mai_text đã strip block, phải đọc raw_had_mood_block.
        recs = [
            {"kind": "chat_reply", "mai_text": "Chào cậu.", "raw_had_mood_block": True},
            {"kind": "chat_reply", "mai_text": "Chào cậu.", "raw_had_mood_block": False},
            # mai_text chứa block nhưng raw_had_mood_block=False → tin field (LLM ổn,
            # ai đó chèn tay vào text hiển thị).
            {"kind": "chat_reply", "mai_text": "hi [vui:5]", "raw_had_mood_block": False},
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(p, recs)
        rep = evaluate(iter_records(p))
        assert rep.mood_exposition_count == 1

    def test_since_filter(self, tmp_path: Path) -> None:
        base = datetime(2026, 8, 6, 18, 0, 0, tzinfo=timezone.utc)
        recs = [
            {"kind": "chat_reply", "mai_text": "cũ 1 2 3.",
             "timestamp": base.isoformat()},
            {"kind": "chat_reply", "mai_text": "mới 4 5 6.",
             "timestamp": (base + timedelta(minutes=10)).isoformat()},
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(p, recs)
        rep = evaluate(iter_records(p, since=base + timedelta(minutes=5)))
        assert rep.total == 1

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        rep = evaluate(iter_records(p))
        assert rep.total == 0
        assert rep.opener_repeat_ratio == 0.0

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        p.write_text(
            "{\"kind\":\"chat_reply\",\"mai_text\":\"ok ok ok.\"}\n"
            "not json at all\n"
            "{\"kind\":\"ambient\",\"mai_text\":\"hai ba bốn.\"}\n",
            encoding="utf-8",
        )
        rep = evaluate(iter_records(p))
        assert rep.total == 2
