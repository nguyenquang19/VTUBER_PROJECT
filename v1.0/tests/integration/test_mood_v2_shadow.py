from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from orchestrator.emotion_orchestrator import EmotionOrchestrator
from services.emotion.classifier import EmotionEvent, EventKind


ROOT = Path(__file__).resolve().parents[2]


def _emotion() -> EmotionOrchestrator:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return EmotionOrchestrator.from_loader(loader)


@pytest.mark.asyncio
async def test_shadow_observes_same_event_without_changing_v1_mood() -> None:
    shadow = _emotion()
    legacy = _emotion()
    legacy.set_affect_shadow_enabled(False)
    event = EmotionEvent(
        kind=EventKind.CHAT, text="Mai giỏi quá",
        meta={"source_event_id": "chat-1", "author": "private viewer"},
    )
    await shadow.handle_event(event)
    await legacy.handle_event(event)
    assert shadow.flush_and_tick(0.1) == legacy.flush_and_tick(0.1)
    snapshot = shadow.snapshot()
    assert snapshot["affect_v2"]["turn_affect"]["cause_ref"] == "chat-1"
    assert "private viewer" not in str(snapshot["affect_v2"])
    assert snapshot["affect_v2"]["prompt_enabled"] is True


@pytest.mark.asyncio
async def test_prompt_cutover_can_rollback_immediately_to_v1() -> None:
    emotion = _emotion()
    await emotion.handle_event(EmotionEvent(
        kind=EventKind.CHAT, text="Mai giỏi quá", meta={"source_event_id": "chat-1"},
    ))
    legacy_mood = emotion.current_mood()
    directive = emotion.delivery_directive()
    assert directive is not None
    assert "Nhận lời khen ngắn" in directive
    assert "Tối đa 2 câu" in directive
    emotion.set_affect_prompt_enabled(False)
    assert emotion.delivery_directive() is None
    assert emotion.current_mood() == legacy_mood
    emotion.set_affect_prompt_enabled(True)
    assert emotion.delivery_directive() is not None


@pytest.mark.asyncio
async def test_hybrid_prompt_keeps_spam_boundary_as_v2_primary() -> None:
    emotion = _emotion()
    await emotion.handle_event(EmotionEvent(
        kind=EventKind.CHAT,
        text="Mai trả lời! Mai trả lời! Mai trả lời!",
        meta={"source_event_id": "spam-1", "is_spam": True},
    ))
    emotion.set_affect_prompt_enabled(True)
    directive = emotion.delivery_directive()
    assert directive is not None
    assert "nhịp spam" in directive
    assert "Tối đa 2 câu" in directive


@pytest.mark.asyncio
async def test_affect_ttl_advances_when_turn_context_is_consumed() -> None:
    emotion = _emotion()
    await emotion.handle_event(EmotionEvent(
        kind=EventKind.SYSTEM,
        meta={"platform_type": "donation", "amount_vnd": 100000, "source_event_id": "don-1"},
    ))
    assert emotion.current_turn_affect() is not None
    emotion.clear_tone_flags()
    assert emotion.current_turn_affect() is None
