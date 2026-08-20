"""Test MemoryExtractor — Phase 7.F.1."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from interfaces.memory import MemoryTier
from services.memory.extractor import MemoryExtractor, TurnData


def make_turn(user="hello world dài đủ chữ", mai="chào cậu xin chào cậu ơi", **over):
    kw = dict(
        user_input=user, mai_output=mai, delivery_verified=True,
        outcome_id="delivery-1",
    )
    kw.update(over)
    return TurnData(**kw)


class TestSkipTrivial:
    def test_both_short_returns_none(self) -> None:
        ex = MemoryExtractor()
        assert ex.extract(make_turn(user="hi", mai="ok")) is None

    def test_one_side_long_ok(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(user="hi", mai="chào cậu đây là câu Mai nói khá dài"))
        assert e is not None


class TestTierAndImportance:
    def test_default_session_tier(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn())
        assert e.tier == MemoryTier.SESSION
        assert e.importance == 0.5

    def test_preference_becomes_persistent(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(user="tớ thích cà phê sữa đá lắm"))
        assert e.tier == MemoryTier.PERSISTENT
        assert e.importance == 0.85
        assert "preference" in e.tags

    def test_name_declaration_persistent(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(user="tôi tên là Nguyễn Văn A nhé"))
        assert e.tier == MemoryTier.PERSISTENT

    def test_birthday_declaration_persistent(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(user="sinh nhật của mình là 15 tháng 8 mai nhớ nhé"))
        assert e.tier == MemoryTier.PERSISTENT

    def test_high_intensity_boost_importance(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(mood_intensity=8, mood_dominant="vui"))
        assert e.importance == 0.7
        assert "high_intensity" in e.tags

    def test_low_intensity_no_boost(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(mood_intensity=3, mood_dominant="buon"))
        assert e.importance == 0.5


class TestTagsAndMetadata:
    def test_mood_tag(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(mood_dominant="bực", mood_intensity=5))
        assert "mood:bực" in e.tags
        assert e.metadata["mood_dominant"] == "bực"
        assert e.metadata["mood_intensity"] == 5

    def test_viewer_and_session_metadata(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(viewer_id="v_1", session_id="s_1"))
        assert e.metadata["viewer_id"] == "v_1"
        assert e.metadata["session_id"] == "s_1"

    def test_trigger_type_tag(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(trigger_type="chat_normal"))
        assert "trigger:chat_normal" in e.tags

    def test_no_extras_when_absent(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn())
        assert "viewer_id" not in e.metadata
        assert "session_id" not in e.metadata


class TestContentComposition:
    def test_both_sides(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(user="cậu tên gì?", mai="tớ là Mai đây cậu ơi"))
        assert "User: cậu tên gì?" in e.content
        assert "Mai: tớ là Mai đây cậu ơi" in e.content

    def test_user_only(self) -> None:
        ex = MemoryExtractor()
        e = ex.extract(make_turn(user="tớ tên An nhớ nhé mai ơi", mai=""))
        assert e.content.startswith("User:")
        assert "Mai:" not in e.content

    def test_timestamp_from_turn(self) -> None:
        ex = MemoryExtractor()
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        e = ex.extract(make_turn(timestamp=ts))
        assert e.timestamp == ts


class TestEntryId:
    def test_generates_unique_ids(self) -> None:
        ex = MemoryExtractor()
        ids = {ex.extract(make_turn()).entry_id for _ in range(20)}
        assert len(ids) == 20  # tất cả unique

    def test_unverified_delivery_is_not_extracted(self) -> None:
        ex = MemoryExtractor()
        assert ex.extract(make_turn(delivery_verified=False)) is None
