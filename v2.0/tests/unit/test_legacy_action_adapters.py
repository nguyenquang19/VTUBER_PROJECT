"""Regression coverage for the Phase 8 typed legacy action adapters."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from interfaces.compatibility import ActionRequest
from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult
from services.action.legacy_adapters import (
    AvatarGestureExecutor,
    AvatarGestureVerifier,
    SpeechDeliveryExecutor,
    SpeechDeliveryVerifier,
)
from services.animation.vts_service import VTSAnimationService


def _request(action_type: str, **arguments: object) -> ActionRequest:
    return ActionRequest(
        schema_version=1, action_id=f"action:{action_type}", capability_id=action_type,
        action_type=action_type, target=None, arguments=arguments, intention_id=None,
        evidence_refs=(), idempotency_key=f"key:{action_type}", priority=0.0,
        requested_at=datetime(2026, 8, 15, tzinfo=timezone.utc), transaction_policy="verified",
    )


def test_speech_adapter_accepts_complete_subtitle_delivery_once() -> None:
    calls: list[tuple[str, str]] = []

    async def speak(request_id: str, text: str) -> TTSDeliveryResult:
        calls.append((request_id, text))
        return TTSDeliveryResult(
            request_id=request_id, delivered=True, mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1, sentences_delivered=1, subtitle_sentences=1,
        )

    executor = SpeechDeliveryExecutor(speak, enabled=True)
    verifier = SpeechDeliveryVerifier(enabled=True)
    request = _request("SPEAK", text="xin chào")
    result = asyncio.run(executor.execute(request))
    verification = asyncio.run(verifier.verify(request, result))

    assert calls == [("action:SPEAK", "xin chào")]
    assert verification.verified is True
    assert result.verified is False  # executor claim is never authoritative


def test_speech_adapter_rejects_partial_or_missing_delivery() -> None:
    async def partial(_request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id="partial", delivered=False, mode=TTSDeliveryMode.MIXED,
            sentences_total=2, sentences_delivered=1, subtitle_sentences=1, failed_sentences=1,
        )

    request = _request("SPEAK", text="một. hai.")
    result = asyncio.run(SpeechDeliveryExecutor(partial, enabled=True).execute(request))
    assert result.error_code == "delivery_not_confirmed"
    assert asyncio.run(SpeechDeliveryVerifier(enabled=True).verify(request, result)).verified is False
    assert asyncio.run(SpeechDeliveryExecutor(None, enabled=True).execute(request)).error_code == "delivery_callback_missing"


class _Animation:
    def __init__(self, acknowledged: bool) -> None:
        self.acknowledged = acknowledged
        self.calls: list[str] = []

    async def trigger_intentional_gesture(self, gesture_id: str) -> bool:
        self.calls.append(gesture_id)
        return self.acknowledged


def test_avatar_adapter_requires_acknowledgement_and_respects_disable() -> None:
    request = _request("AVATAR_GESTURE", gesture_id="wave")
    animation = _Animation(True)
    executor = AvatarGestureExecutor(animation, enabled=True)
    result = asyncio.run(executor.execute(request))
    assert animation.calls == ["wave"]
    assert asyncio.run(AvatarGestureVerifier(enabled=True).verify(request, result)).verified is True
    executor.set_enabled(False)
    assert asyncio.run(executor.execute(request)).error_code == "adapter_disabled"


class _Transport:
    connected = True
    hotkeys = ("Wave",)

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def trigger(self, hotkey: str) -> bool:
        return hotkey == "Wave"


def test_vts_intentional_gesture_is_allowlisted_and_fail_safe() -> None:
    service = VTSAnimationService(
        _Transport(), mood_hotkeys={}, intentional_gesture_hotkeys={"wave": "Wave"},
    )
    asyncio.run(service.start())
    assert asyncio.run(service.trigger_intentional_gesture("wave")) is True
    assert asyncio.run(service.trigger_intentional_gesture("unknown")) is False
    asyncio.run(service.stop())
    assert asyncio.run(service.trigger_intentional_gesture("wave")) is False